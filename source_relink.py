"""
source_relink.py — Preradenie článkov z presmerovacích domén (google.com/goto?url=...) na skutočný zdroj.

SerpAPI pri niektorých výsledkoch vracia len Google presmerovanie, takže ingest zaradí článok pod
`google.com`. Skutočná URL sa zistí až pri fetchi (`final_url`) — vtedy sa článok preradí na jej doménu.
Ak už v DB existuje iný aktívny článok s rovnakou URL, presmerovaný článok sa označí ako duplikát (soft delete).

Volá sa z extract_bulk.py a refetch_article.py. Ako CLI dorovná existujúce články:
    python source_relink.py           # dry-run: zobrazí čo by sa zmenilo
    python source_relink.py --apply   # skutočne preradí
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

# URL v texte chyby requests: "403 Client Error: Forbidden for url: https://..."
_ERROR_URL_RE = re.compile(r"for url: (\S+)")


@dataclass
class RelinkResult:
    domain: str
    duplicate_of: int | None = None
    deleted: bool = False  # duplikát bol soft-deletnutý (nemal ručné hodnotenie)


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


def relink_source(conn, article_id: int, final_url: str | None, final_url_hash: str | None = None) -> RelinkResult | None:
    """Ak je článok pod presmerovacou doménou a final_url vedie inam, preradí ho na skutočnú doménu.

    Vracia None ak sa nič nemenilo. Duplikát sa označí soft delete len ak nemá ručné hodnotenie.
    """
    new_domain = domain_of(final_url)
    if not new_domain or is_redirect_domain(new_domain):
        return None

    row = conn.execute(text("""
        SELECT s.domain, a.relevance FROM articles a JOIN sources s ON s.id = a.source_id WHERE a.id = :id
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
    result = RelinkResult(domain=new_domain)

    if final_url_hash:
        dup = conn.execute(text("""
            SELECT id FROM articles
            WHERE id <> :id AND deleted_at IS NULL
              AND (url_hash = :h OR final_url_hash = :h)
            ORDER BY id LIMIT 1
        """), {"id": article_id, "h": final_url_hash}).fetchone()
        if dup:
            result.duplicate_of = dup[0]
            if row["relevance"] is None:
                conn.execute(text("""
                    UPDATE articles
                    SET deleted_at = NOW(),
                        relevance_note = COALESCE(relevance_note, :note)
                    WHERE id = :id
                """), {"id": article_id, "note": f"auto: duplikát #{dup[0]}"})
                result.deleted = True

    logger.info("id=%s: google.com → %s%s", article_id, new_domain,
                f" (duplikát #{result.duplicate_of})" if result.duplicate_of else "")
    return result


def backfill(apply: bool) -> dict:
    """Preradí existujúce články pod presmerovacími doménami podľa final_url (alebo URL z fetch_error)."""
    from extract_articles_reworked import canonicalize_url

    engine = get_db_engine()
    counts = {"relinked": 0, "duplicates": 0, "unresolved": 0}
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
                res = relink_source(conn, r["id"], final_url, final_hash)
            else:
                res = RelinkResult(domain=domain_of(final_url))
                logger.info("[DRY-RUN] id=%s → %s", r["id"], res.domain)
            if res:
                counts["relinked"] += 1
                counts["duplicates"] += 1 if res.duplicate_of else 0
    return counts


def main() -> int:
    init_cli("source_relink")
    p = argparse.ArgumentParser(description="Preradí články z google.com na skutočný zdroj podľa final_url.")
    p.add_argument("--apply", action="store_true", help="Skutočne zapíše zmeny (inak dry-run)")
    args = p.parse_args()

    counts = backfill(args.apply)
    logger.info("%sHotovo: preradených=%s, z toho duplikátov=%s, bez známej cieľovej URL=%s",
                "" if args.apply else "[DRY-RUN] ",
                counts["relinked"], counts["duplicates"], counts["unresolved"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
