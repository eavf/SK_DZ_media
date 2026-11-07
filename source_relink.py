"""
source_relink.py — Preradenie článkov z presmerovacích domén (google.com/goto?url=...) na skutočný zdroj.

SerpAPI pri niektorých výsledkoch vracia len Google presmerovanie, takže ingest zaradí článok pod
`google.com`. Skutočná URL sa zistí až pri fetchi (`final_url`) — vtedy sa článok preradí na jej doménu.

Duplikáty podľa URL (mark_url_duplicate): ten istý článok môže prísť raz ako google.com/goto a inokedy
priamo — url_hash sa líši, zhodu odhalí až final_url_hash. Pri zhode sa novší článok soft-deletne
s poznámkou `auto: duplikát #<id>` (ak má ručné hodnotenie len novší, zmaže sa starší; ak oba, nič).

Volá sa z extract_bulk.py a refetch_article.py. Ako CLI dorovná existujúce články:
  1. aktívnym článkom pod google.com bez final_url zistí cieľ presmerovania (len hlavička Location
     od Google, cieľový web sa neotvára) — staré články stiahnuté pred 13.10.2026 final_url neuložili
  2. preradí články pod google.com podľa final_url (alebo URL z fetch_error)
  3. vyradí duplikáty podľa URL
  4. vyradí duplikáty podľa textu (rovnaký content_hash na tej istej doméne, text ≥ MIN_DUP_TEXT_CHARS,
     zhodný začiatok titulku — TITLE_PREFIX_CHARS)

    python source_relink.py           # dry-run: všetko prebehne v transakcii a na konci sa vráti (rollback)
    python source_relink.py --apply   # skutočne zapíše
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from sqlalchemy import bindparam, text

from config.config import get_db_engine, init_cli, init_context

s, paths = init_context()
logger = logging.getLogger("source_relink")

REDIRECT_DOMAINS = {"google.com", "news.google.com"}

# final_url = titulná / jazyková stránka webu alebo výpis (kategória, tag, rubrika, archív, stránkovanie):
# zmazaný článok často presmeruje sem → dva rôzne články by mali rovnakú final_url, nepovažuje sa za zhodu.
# Rovnaký regex beží v MariaDB (REGEXP, bez ohľadu na veľkosť písmen) aj v Pythone (is_listing_url).
LISTING_RE = (
    r"^https?://[^/]+(/(fr|ar|en))?/?$"
    r"|/(category|categorie|categories|tag|tags|rubrique|rubriques|section|author|auteur"
    r"|archive|archives|page)(/|$)"
)
_NOT_LISTING = "{col} NOT REGEXP '" + LISTING_RE + "'"
_LISTING_PY = re.compile(LISTING_RE, re.IGNORECASE)


def is_listing_url(canon: str | None) -> bool:
    """True pre titulnú/jazykovú stránku alebo výpis — taká final_url nie je dôkaz duplikátu."""
    return bool(canon and _LISTING_PY.search(canon))

# kratší „text“ býva zlá extrakcia (cookie lišta, pätička) — rovnaký u rôznych článkov
MIN_DUP_TEXT_CHARS = 500
# rovnaký text nestačí: zle extrahovaná stránka (menu webu) je rovnaká pre rôzne články (alaraby.co.uk) →
# musí sedieť aj začiatok normalizovaného titulku (prípona / koniec titulku sa môže líšiť)
TITLE_PREFIX_CHARS = 40

RESOLVE_DELAY = 1.0     # s medzi požiadavkami na Google (nech nezačne blokovať)
RESOLVE_TIMEOUT = (10, 20)

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


def mark_url_duplicate(conn, article_id: int, *, apply: bool = True) -> DuplicateResult | None:
    """Nájde iný aktívny článok s rovnakou URL (url_hash / final_url_hash v ľubovoľnej kombinácii).

    Zmaže sa novší z dvojice; ak má ručné hodnotenie len novší, zmaže sa starší; ak oba, nič.
    apply=False: len rozhodne a zaloguje, nič nezapíše (deleted = čo by sa zmazalo).
    Vracia None ak duplikát neexistuje.
    """
    row = conn.execute(text("""
        SELECT a.relevance AS a_rel, o.id AS o_id, o.relevance AS o_rel
        FROM articles a
        JOIN articles o
          ON o.id <> a.id AND o.deleted_at IS NULL
         AND ((o.url_hash = a.final_url_hash AND {a_not_listing})
              OR (o.final_url_hash = a.url_hash AND {o_not_listing})
              OR (o.final_url_hash = a.final_url_hash AND {a_not_listing}))
        WHERE a.id = :id AND a.deleted_at IS NULL
        ORDER BY o.id
        LIMIT 1
    """.format(a_not_listing=_NOT_LISTING.format(col="a.final_url_canonical"),
               o_not_listing=_NOT_LISTING.format(col="o.final_url_canonical"))),
        {"id": article_id}).mappings().fetchone()
    if not row:
        return None

    return _resolve_pair(conn, article_id, row, apply=apply, note_suffix="")


def titles_match(t1: str | None, t2: str | None) -> bool:
    """Prvých TITLE_PREFIX_CHARS znakov normalizovaného titulku je zhodných (prázdny titulok → False)."""
    from ingest_to_dz_news_reworked import normalize_title
    n1, n2 = normalize_title(t1), normalize_title(t2)
    return bool(n1 and n2) and n1[:TITLE_PREFIX_CHARS] == n2[:TITLE_PREFIX_CHARS]


def mark_content_duplicate(conn, article_id: int, *, apply: bool = True) -> DuplicateResult | None:
    """Nájde iný aktívny článok z tej istej domény s rovnakým textom (content_hash) a podobným titulkom.

    Zachytí ten istý článok pod inou URL (AMP, iná kategória, iný slug). Text kratší ako
    MIN_DUP_TEXT_CHARS sa neporovnáva; titulky musia mať zhodný začiatok (titles_match).
    Pravidlá mazania ako pri mark_url_duplicate.
    """
    rows = conn.execute(text("""
        SELECT a.relevance AS a_rel, a.title AS a_title, o.id AS o_id, o.relevance AS o_rel, o.title AS o_title
        FROM articles a
        JOIN articles o
          ON o.id <> a.id AND o.deleted_at IS NULL
         AND o.source_id = a.source_id AND o.content_hash = a.content_hash
        WHERE a.id = :id AND a.deleted_at IS NULL AND a.content_hash IS NOT NULL
          AND CHAR_LENGTH(a.content_text) >= :min_chars
        ORDER BY o.id
    """), {"id": article_id, "min_chars": MIN_DUP_TEXT_CHARS}).mappings().fetchall()
    row = next((r for r in rows if titles_match(r["a_title"], r["o_title"])), None)
    if not row:
        return None
    return _resolve_pair(conn, article_id, row, apply=apply, note_suffix=" (text)")


def _resolve_pair(conn, article_id: int, row, *, apply: bool, note_suffix: str) -> DuplicateResult:
    """Rozhodne, ktorý z dvojice sa zmaže (novší; ak má ručné hodnotenie len novší, tak starší; ak oba, nič)."""
    rel = {article_id: row["a_rel"], row["o_id"]: row["o_rel"]}
    older, newer = sorted(rel)
    if rel[newer] is None:
        keep_id, delete_id = older, newer
    elif rel[older] is None:
        keep_id, delete_id = newer, older
    else:
        logger.info("%sid=%s: duplikát #%s%s, oba ručne hodnotené — ponechávam",
                    "" if apply else "[DRY-RUN] ", newer, older, note_suffix)
        return DuplicateResult(keep_id=older, delete_id=newer, deleted=False)

    if apply:
        conn.execute(text("""
            UPDATE articles
            SET deleted_at = NOW(),
                relevance_note = COALESCE(relevance_note, :note)
            WHERE id = :id
        """), {"id": delete_id, "note": f"auto: duplikát #{keep_id}{note_suffix}"})
    logger.info("%sid=%s: vyradený ako duplikát #%s%s", "" if apply else "[DRY-RUN] ",
                delete_id, keep_id, note_suffix)
    return DuplicateResult(keep_id=keep_id, delete_id=delete_id, deleted=True)


DUP_REASON_LABELS = {"url": "rovnaká URL", "text": "rovnaký text", "title": "podobný titulok"}


def find_possible_duplicates(conn, article_id: int, limit: int = 10) -> list[dict]:
    """Možné duplikáty článku na zobrazenie (nič nemaže) — aj zmazané články.

    Dôvody: 'url' (url_hash / final_url_hash, bez titulky a výpisov), 'text' (rovnaký content_hash
    na tej istej doméne, text ≥ MIN_DUP_TEXT_CHARS), 'title' (titles_match na tej istej doméne).
    Vracia [{id, title, domain, relevance, deleted_at, reasons: [...]}] zoradené od najsilnejšej zhody.
    """
    a = conn.execute(text("""
        SELECT id, source_id, title, url_hash, final_url_hash, final_url_canonical, content_hash,
               CHAR_LENGTH(content_text) AS text_len
        FROM articles WHERE id = :id
    """), {"id": article_id}).mappings().fetchone()
    if not a:
        return []

    found: dict[int, set[str]] = {}

    final_ok = a["final_url_hash"] and not is_listing_url(a["final_url_canonical"])
    for r in conn.execute(text("""
        SELECT o.id, o.final_url_canonical, o.url_hash = :fh AS by_url_final,
               o.final_url_hash = :fh AS by_final_final, o.final_url_hash = :uh AS by_final_url
        FROM articles o
        WHERE o.id <> :id AND (o.url_hash = :fh OR o.final_url_hash = :fh OR o.final_url_hash = :uh)
    """), {"id": article_id, "uh": a["url_hash"], "fh": a["final_url_hash"] if final_ok else "-"}).mappings():
        if r["by_final_url"] and is_listing_url(r["final_url_canonical"]):
            if not (r["by_url_final"] or r["by_final_final"]):
                continue
        found.setdefault(r["id"], set()).add("url")

    if a["content_hash"] and (a["text_len"] or 0) >= MIN_DUP_TEXT_CHARS:
        for r in conn.execute(text("""
            SELECT id FROM articles
            WHERE id <> :id AND source_id = :sid AND content_hash = :ch
        """), {"id": article_id, "sid": a["source_id"], "ch": a["content_hash"]}):
            found.setdefault(r[0], set()).add("text")

    if a["title"]:
        for r in conn.execute(text("""
            SELECT id, title FROM articles WHERE id <> :id AND source_id = :sid AND title IS NOT NULL
        """), {"id": article_id, "sid": a["source_id"]}).mappings():
            if titles_match(a["title"], r["title"]):
                found.setdefault(r["id"], set()).add("title")

    if not found:
        return []
    order = {"url": 0, "text": 1, "title": 2}
    ids = sorted(found, key=lambda i: (min(order[x] for x in found[i]), -len(found[i]), i))[:limit]
    rows = {r["id"]: dict(r) for r in conn.execute(text("""
        SELECT a.id, a.title, s.domain, a.relevance, a.deleted_at, a.relevance_note
        FROM articles a JOIN sources s ON s.id = a.source_id WHERE a.id IN :ids
    """).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).mappings()}
    out = []
    for i in ids:
        if i in rows:
            row = rows[i]
            row["reasons"] = sorted(found[i], key=order.get)
            out.append(row)
    return out


def format_duplicates_flash(dups: list[dict]) -> str | None:
    """Krátke upozornenie pre flash po ručnej akcii (len aktívne články)."""
    active = [d for d in dups if not d["deleted_at"]]
    if not active:
        return None
    parts = [f"#{d['id']} ({', '.join(DUP_REASON_LABELS[x] for x in d['reasons'])})" for d in active[:5]]
    return "⚠ Možný duplikát: " + "; ".join(parts) + " — pozri sekciu Možné duplikáty."


def resolve_redirect(url: str) -> str | None:
    """Cieľ presmerovania z hlavičky Location (jedna požiadavka, cieľový web sa neotvára)."""
    from extract_articles_reworked import REQUEST_HEADERS
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, allow_redirects=False, timeout=RESOLVE_TIMEOUT)
    except requests.RequestException as e:
        logger.warning("Presmerovanie zlyhalo (%s): %s", type(e).__name__, url)
        return None
    loc = resp.headers.get("Location") if resp.is_redirect else None
    return urljoin(url, loc) if loc else None


def resolve_missing(conn) -> dict:
    """Aktívnym článkom pod presmerovacou doménou bez final_url doplní final_url z presmerovania."""
    from extract_articles_reworked import canonicalize_url, sha256_hex

    rows = conn.execute(text("""
        SELECT a.id, a.url
        FROM articles a JOIN sources s ON s.id = a.source_id
        WHERE s.domain IN :domains AND a.final_url IS NULL AND a.deleted_at IS NULL
        ORDER BY a.id
    """).bindparams(bindparam("domains", expanding=True)), {"domains": list(REDIRECT_DOMAINS)}).mappings().fetchall()

    counts = {"resolved": 0, "failed": 0}
    for i, r in enumerate(rows):
        if i:
            time.sleep(RESOLVE_DELAY)
        final_url = resolve_redirect(r["url"])
        if not final_url or is_redirect_domain(domain_of(final_url)):
            logger.warning("id=%s: presmerovanie sa nepodarilo zistiť", r["id"])
            counts["failed"] += 1
            continue
        canon = canonicalize_url(final_url)
        conn.execute(text("""
            UPDATE articles SET final_url = :u, final_url_canonical = :c, final_url_hash = :h WHERE id = :id
        """), {"u": final_url, "c": canon, "h": sha256_hex(canon), "id": r["id"]})
        logger.info("id=%s: final_url = %s", r["id"], final_url)
        counts["resolved"] += 1
    return counts


def backfill(conn) -> dict:
    """Preradí existujúce články pod presmerovacími doménami podľa final_url (alebo URL z fetch_error)."""
    from extract_articles_reworked import canonicalize_url

    counts = {"relinked": 0, "unresolved": 0}
    rows = conn.execute(text("""
        SELECT a.id, a.final_url, a.final_url_hash, a.fetch_error
        FROM articles a JOIN sources s ON s.id = a.source_id
        WHERE s.domain IN :domains
        ORDER BY a.id
    """).bindparams(bindparam("domains", expanding=True)), {"domains": list(REDIRECT_DOMAINS)}).mappings().fetchall()

    for r in rows:
        final_url = r["final_url"]
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
            # uložená final_url → ďalší pokus o fetch pôjde priamo na cieľový web
            conn.execute(text("""
                UPDATE articles SET final_url = :u, final_url_canonical = :c, final_url_hash = :h
                WHERE id = :id
            """), {"u": final_url, "c": canon, "h": final_hash, "id": r["id"]})

        if relink_source(conn, r["id"], final_url):
            counts["relinked"] += 1
    return counts


def backfill_duplicates(conn) -> int:
    """Vyradí existujúce duplikáty podľa URL (napr. google.com/goto + priamy odkaz na ten istý článok)."""
    # novší článok z každej dvojice (symetrický self-join pokrýva aj o.final_url_hash = a.url_hash)
    ids = sorted({r[0] for r in conn.execute(text("""
        SELECT GREATEST(a.id, o.id) FROM articles a
        JOIN articles o ON o.id <> a.id AND (
             o.url_hash = a.final_url_hash
          OR o.final_url_hash = a.final_url_hash)
        WHERE a.deleted_at IS NULL AND o.deleted_at IS NULL AND {a_not_listing}
    """.format(a_not_listing=_NOT_LISTING.format(col="a.final_url_canonical")))).fetchall()})

    deleted: set[int] = set()
    for article_id in ids:
        res = mark_url_duplicate(conn, article_id)
        if res and res.deleted:
            deleted.add(res.delete_id)
    return len(deleted)


def backfill_content_duplicates(conn) -> int:
    """Vyradí existujúce duplikáty podľa textu (rovnaký content_hash na tej istej doméne)."""
    ids = sorted({r[0] for r in conn.execute(text("""
        SELECT GREATEST(a.id, o.id) FROM articles a
        JOIN articles o ON o.id <> a.id AND o.source_id = a.source_id AND o.content_hash = a.content_hash
        WHERE a.deleted_at IS NULL AND o.deleted_at IS NULL
          AND CHAR_LENGTH(a.content_text) >= :min_chars
    """), {"min_chars": MIN_DUP_TEXT_CHARS}).fetchall()})

    deleted: set[int] = set()
    for article_id in ids:
        res = mark_content_duplicate(conn, article_id)
        if res and res.deleted:
            deleted.add(res.delete_id)
    return len(deleted)


def main() -> int:
    init_cli("source_relink")
    p = argparse.ArgumentParser(description="Preradí články z google.com na skutočný zdroj a vyradí duplikáty podľa URL.")
    p.add_argument("--apply", action="store_true", help="Skutočne zapíše zmeny (inak dry-run s rollbackom)")
    args = p.parse_args()
    prefix = "" if args.apply else "[DRY-RUN] "
    if not args.apply:
        logger.info("[DRY-RUN] zmeny sa na konci vrátia (rollback) — výpis ukazuje, čo urobí --apply")

    with get_db_engine().connect() as conn:
        tx = conn.begin()
        try:
            resolved = resolve_missing(conn)
            counts = backfill(conn)
            duplicates = backfill_duplicates(conn)
            content_duplicates = backfill_content_duplicates(conn)
        except Exception:
            tx.rollback()
            raise
        if args.apply:
            tx.commit()
        else:
            tx.rollback()

    logger.info("%sHotovo: zistených presmerovaní=%s (nezistených=%s), preradených=%s, "
                "bez známej cieľovej URL=%s, vyradených duplikátov podľa URL=%s, podľa textu=%s",
                prefix, resolved["resolved"], resolved["failed"], counts["relinked"],
                counts["unresolved"], duplicates, content_duplicates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
