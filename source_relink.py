"""
source_relink.py — Preradenie článkov z presmerovacích domén (google.com/goto?url=...) na skutočný zdroj.

SerpAPI pri niektorých výsledkoch vracia len Google presmerovanie, takže ingest zaradí článok pod
`google.com`. Skutočná URL sa zistí až pri fetchi (`final_url`) — vtedy sa článok preradí na jej doménu.

Duplikáty podľa URL (mark_url_duplicate): ten istý článok môže prísť raz ako google.com/goto a inokedy
priamo — url_hash sa líši, zhodu odhalí až final_url_hash. Pri zhode sa novší článok soft-deletne
s poznámkou `auto: duplikát #<id>` (ak má ručné hodnotenie len novší, zmaže sa starší; ak oba, nič).

Volá sa z extract_bulk.py a refetch_article.py. Ako CLI dorovná existujúce články:
    python source_relink.py           # dry-run: zobrazí čo by sa zmenilo
    python source_relink.py --apply   # skutočne preradí a vyradí duplikáty
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import bindparam, text

from config.config import get_db_engine, init_cli, init_context

s, paths = init_context()
logger = logging.getLogger("source_relink")

REDIRECT_DOMAINS = {"google.com", "news.google.com"}

# final_url = titulná stránka webu (zmazaný článok presmeruje na homepage) → nepovažuje sa za zhodu
_NOT_ROOT = "{col} NOT REGEXP '^https?://[^/]+/?$'"

# URL v texte chyby requests: "403 Client Error: Forbidden for url: https://..."
_ERROR_URL_RE = re.compile(r"for url: (\S+)")


@dataclass
class DuplicateResult:
    keep_id: int
    delete_id: int
    deleted: bool  # False = oba články majú ručné hodnotenie, nič sa nezmazalo


def domain_of(url: str | None) -> str:
    host = urlparse(url or "").netloc.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0]


def url_from_fetch_error(fetch_error: str | None) -> str | None:
    """Skutočná URL z textu HTTPError (pre články, kde fetch zlyhal až na cieľovom webe)."""
    m = _ERROR_URL_RE.search(fetch_error or "")
    return m.group(1) if m else None


def is_redirect_domain(domain: str) -> bool:
    return domain in REDIRECT_DOMAINS


def relink_source(conn, article_id: int, final_url: str | None) -> str | None:
    """Ak je článok pod presmerovacou doménou a final_url vedie inam, preradí ho na skutočnú doménu.

    Vracia novú doménu, alebo None ak sa nič nemenilo.
    """
    new_domain = domain_of(final_url)
    if not new_domain or is_redirect_domain(new_domain):
        return None

    row = conn.execute(text("""
        SELECT s.domain FROM articles a JOIN sources s ON s.id = a.source_id WHERE a.id = :id
    """), {"id": article_id}).mappings().fetchone()
    if not row or not is_redirect_domain(row["domain"]):
        return None

    conn.execute(text("""
        INSERT INTO sources (domain, is_preferred)
        VALUES (:domain, :is_preferred)
        ON DUPLICATE KEY UPDATE is_preferred = GREATEST(is_preferred, VALUES(is_preferred))
    """), {"domain": new_domain, "is_preferred": 1 if new_domain in s.preferred_domains else 0})
    conn.execute(text("""
        UPDATE articles SET source_id = (SELECT id FROM sources WHERE domain = :domain) WHERE id = :id
    """), {"domain": new_domain, "id": article_id})

    logger.info("id=%s: %s → %s", article_id, row["domain"], new_domain)
    return new_domain


def mark_url_duplicate(conn, article_id: int) -> DuplicateResult | None:
    """Nájde iný aktívny článok s rovnakou URL (url_hash / final_url_hash v ľubovoľnej kombinácii).

    Zmaže sa novší z dvojice; ak má ručné hodnotenie len novší, zmaže sa starší; ak oba, nič.
    Vracia None ak duplikát neexistuje.
    """
    row = conn.execute(text("""
        SELECT a.relevance AS a_rel, o.id AS o_id, o.relevance AS o_rel
        FROM articles a
        JOIN articles o
          ON o.id <> a.id AND o.deleted_at IS NULL
         AND ((o.url_hash = a.final_url_hash AND {a_not_root})
              OR (o.final_url_hash = a.url_hash AND {o_not_root})
              OR (o.final_url_hash = a.final_url_hash AND {a_not_root}))
        WHERE a.id = :id AND a.deleted_at IS NULL
        ORDER BY o.id
        LIMIT 1
    """.format(a_not_root=_NOT_ROOT.format(col="a.final_url_canonical"),
               o_not_root=_NOT_ROOT.format(col="o.final_url_canonical"))),
        {"id": article_id}).mappings().fetchone()
    if not row:
        return None

    rel = {article_id: row["a_rel"], row["o_id"]: row["o_rel"]}
    older, newer = sorted(rel)
    if rel[newer] is None:
        keep_id, delete_id = older, newer
    elif rel[older] is None:
        keep_id, delete_id = newer, older
    else:
        logger.info("id=%s: duplikát #%s, oba ručne hodnotené — ponechávam", newer, older)
        return DuplicateResult(keep_id=older, delete_id=newer, deleted=False)

    conn.execute(text("""
        UPDATE articles
        SET deleted_at = NOW(),
            relevance_note = COALESCE(relevance_note, :note)
        WHERE id = :id
    """), {"id": delete_id, "note": f"auto: duplikát #{keep_id}"})
    logger.info("id=%s: vyradený ako duplikát #%s", delete_id, keep_id)
    return DuplicateResult(keep_id=keep_id, delete_id=delete_id, deleted=True)


def backfill(apply: bool) -> dict:
    """Preradí existujúce články pod presmerovacími doménami podľa final_url (alebo URL z fetch_error)."""
    from extract_articles_reworked import canonicalize_url

    engine = get_db_engine()
    counts = {"relinked": 0, "unresolved": 0}
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT a.id, a.final_url, a.final_url_hash, a.fetch_error
            FROM articles a JOIN sources s ON s.id = a.source_id
            WHERE s.domain IN :domains
            ORDER BY a.id
        """).bindparams(bindparam("domains", expanding=True)), {"domains": list(REDIRECT_DOMAINS)}).mappings().fetchall()

        for r in rows:
            final_url, final_hash = r["final_url"], r["final_url_hash"]
            from_error = False
            if not final_url:
                final_url = url_from_fetch_error(r["fetch_error"])
                from_error = bool(final_url)
            if not domain_of(final_url) or is_redirect_domain(domain_of(final_url)):
                counts["unresolved"] += 1
                continue
            if from_error:
                canon = canonicalize_url(final_url)
                final_hash = hashlib.sha256(canon.encode("utf-8")).hexdigest() if canon else None
                if apply:
                    # uložená final_url → ďalší pokus o fetch pôjde priamo na cieľový web
                    conn.execute(text("""
                        UPDATE articles SET final_url = :u, final_url_canonical = :c, final_url_hash = :h
                        WHERE id = :id
                    """), {"u": final_url, "c": canon, "h": final_hash, "id": r["id"]})

            if apply:
                if relink_source(conn, r["id"], final_url):
                    counts["relinked"] += 1
            else:
                logger.info("[DRY-RUN] id=%s → %s", r["id"], domain_of(final_url))
                counts["relinked"] += 1
    return counts


def backfill_duplicates(apply: bool) -> int:
    """Vyradí existujúce duplikáty podľa URL (napr. google.com/goto + priamy odkaz na ten istý článok)."""
    engine = get_db_engine()
    with engine.begin() as conn:
        # novší článok z každej dvojice; tri samostatné JOINy (namiesto OR) sú rýchlejšie
        ids = sorted({r[0] for r in conn.execute(text("""
            SELECT GREATEST(a.id, o.id) FROM articles a
            JOIN articles o ON o.id <> a.id AND (
                 o.url_hash = a.final_url_hash
              OR o.final_url_hash = a.final_url_hash)
            WHERE a.deleted_at IS NULL AND o.deleted_at IS NULL AND {a_not_root}
        """.format(a_not_root=_NOT_ROOT.format(col="a.final_url_canonical")))).fetchall()})

        count = 0
        for article_id in ids:
            if apply:
                res = mark_url_duplicate(conn, article_id)
                count += 1 if res and res.deleted else 0
            else:
                logger.info("[DRY-RUN] id=%s: duplikát podľa URL", article_id)
                count += 1
    return count


def main() -> int:
    init_cli("source_relink")
    p = argparse.ArgumentParser(description="Preradí články z google.com na skutočný zdroj a vyradí duplikáty podľa URL.")
    p.add_argument("--apply", action="store_true", help="Skutočne zapíše zmeny (inak dry-run)")
    args = p.parse_args()

    counts = backfill(args.apply)
    duplicates = backfill_duplicates(args.apply)
    logger.info("%sHotovo: preradených=%s, bez známej cieľovej URL=%s, vyradených duplikátov=%s",
                "" if args.apply else "[DRY-RUN] ",
                counts["relinked"], counts["unresolved"], duplicates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
