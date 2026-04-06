"""
extract_bulk.py — Hromadná extrakcia článkov bez content_text.

Pre každý článok kde content_text IS NULL a deleted_at IS NULL:
  - fetchuje URL a extrahuje text
  - ak text neobsahuje slovenský kontext → soft delete
  - ak text je komerčný → relevance=0
  - ak extrakcia zlyhala → zapíše chybu, článok ostáva v DB

Použitie:
    python extract_bulk.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import text

from extract_articles_reworked import CandidateArticle, extract_candidate
from config.config import get_db_engine, init_context
from translate import translate_ar_fr

s, paths = init_context()
logger = logging.getLogger("extract_bulk")


def _to_mysql_dt(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def fetch_pending(conn, limit: int) -> list[dict]:
    rows = conn.execute(text("""
        SELECT a.id, COALESCE(a.final_url, a.url) AS fetch_url, a.title, a.snippet,
               a.title_fr, a.snippet_fr, a.language,
               a.published_at_real
        FROM articles a
        JOIN sources s ON s.id = a.source_id
        WHERE a.content_text IS NULL
          AND a.deleted_at IS NULL
          AND s.is_avoided = 0
        ORDER BY a.id
        LIMIT :limit
    """), {"limit": limit}).mappings().fetchall()
    return [dict(r) for r in rows]


def translate_after_extraction(article_id: int, result, row: dict) -> None:
    """Preloží content_text (a title/snippet ak chýbajú) pre arabský článok."""
    api_key = s.deepl_api_key
    lang = row.get("language") or result.lang_detected
    if not api_key or lang != "ar":
        return

    texts, keys = [], []
    if result.content_text:
        texts.append(result.content_text); keys.append("content_text_fr")
    if not row.get("title_fr") and row.get("title"):
        texts.append(row["title"]); keys.append("title_fr")
    if not row.get("snippet_fr") and row.get("snippet"):
        texts.append(row["snippet"]); keys.append("snippet_fr")

    if not texts:
        return

    try:
        translated = translate_ar_fr(api_key, texts)
        updates = dict(zip(keys, translated))
        updates["id"] = article_id
        set_clause = ", ".join(f"{k} = :{k}" for k in keys)
        engine = get_db_engine()
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE articles SET {set_clause} WHERE id = :id"), updates)
        logger.info("Preložené AR→FR: id=%s, polia=%s", article_id, keys)
    except Exception as e:
        logger.warning("DeepL zlyhal pre id=%s: %s", article_id, e)


def save_result(conn, article_id: int, result, existing_published_at_real: str | None = None) -> str:
    """Uloží výsledok extrakcie. Vracia: 'ok', 'soft_deleted', 'commercial', 'failed'.

    existing_published_at_real: hodnota published_at_real z DB pred extrakciou (zo SerpAPI ingesta).
    Ak HTML dátum je novší ako SerpAPI dátum, SerpAPI dátum má prednosť — HTML môže obsahovať
    'last modified' namiesto 'published'.
    """
    html_date = _to_mysql_dt(result.published_at_real)

    if html_date and existing_published_at_real and html_date > str(existing_published_at_real):
        published_at_real = str(existing_published_at_real)
        published_conf_val = "search"
        logger.debug(
            "id=%s: HTML dátum (%s) je novší ako SerpAPI dátum (%s) — použijem SerpAPI dátum",
            article_id, html_date, existing_published_at_real,
        )
    else:
        published_at_real = html_date
        published_conf_val = "absolute" if html_date else None

    if result.extraction_ok and result.no_slovak_context:
        conn.execute(text("""
            UPDATE articles SET deleted_at = NOW() WHERE id = :id
        """), {"id": article_id})
        return "soft_deleted"

    if result.extraction_ok and result.is_commercial:
        conn.execute(text("""
            UPDATE articles
            SET fetched_at          = NOW(),
                http_status         = :http_status,
                fetch_error         = :fetch_error,
                content_text        = :content_text,
                content_hash        = :content_hash,
                published_at_real   = :published_at_real,
                published_conf      = :published_conf,
                lang_detected       = :lang_detected,
                extraction_ok       = 1,
                relevance           = CASE WHEN relevance IS NULL THEN 0 ELSE relevance END
            WHERE id = :id
        """), {
            "http_status": result.http_status,
            "fetch_error": result.fetch_error,
            "content_text": result.content_text,
            "content_hash": result.content_hash,
            "published_at_real": published_at_real,
            "published_conf": published_conf_val,
            "lang_detected": result.lang_detected,
            "id": article_id,
        })
        return "commercial"

    if result.extraction_ok:
        conn.execute(text("""
            UPDATE articles
            SET final_url           = :final_url,
                final_url_canonical = :final_url_canonical,
                final_url_hash      = :final_url_hash,
                fetched_at          = NOW(),
                http_status         = :http_status,
                fetch_error         = NULL,
                content_text        = :content_text,
                content_hash        = :content_hash,
                published_at_real   = :published_at_real,
                published_conf      = :published_conf,
                lang_detected       = :lang_detected,
                extraction_ok       = 1
            WHERE id = :id
        """), {
            "final_url": result.final_url,
            "final_url_canonical": result.final_url_canonical,
            "final_url_hash": result.final_url_hash,
            "http_status": result.http_status,
            "content_text": result.content_text,
            "content_hash": result.content_hash,
            "published_at_real": published_at_real,
            "published_conf": published_conf_val,
            "lang_detected": result.lang_detected,
            "id": article_id,
        })
        return "ok"

    # extrakcia zlyhala
    conn.execute(text("""
        UPDATE articles
        SET fetched_at  = NOW(),
            http_status = :http_status,
            fetch_error = :fetch_error,
            extraction_ok = 0
        WHERE id = :id
    """), {
        "http_status": result.http_status,
        "fetch_error": result.fetch_error,
        "id": article_id,
    })
    return "failed"


def translate_missing_titles(limit: int, dry_run: bool) -> int:
    """Preloží title/snippet pre arabské články s extraction_ok=1 a chýbajúcim title_fr."""
    api_key = s.deepl_api_key
    if not api_key:
        logger.info("translate_missing_titles: DeepL API key chýba, preskakujem.")
        return 0

    engine = get_db_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, title, snippet, title_fr, snippet_fr
            FROM articles
            WHERE (lang_detected = 'ar' OR language = 'ar')
              AND title_fr IS NULL
              AND deleted_at IS NULL
            ORDER BY id
            LIMIT :limit
        """), {"limit": limit}).mappings().fetchall()
        rows = [dict(r) for r in rows]

    if not rows:
        logger.info("translate_missing_titles: žiadne články na preklad.")
        return 0

    logger.info("translate_missing_titles: %s článkov na preklad nadpisov.", len(rows))
    count = 0
    for row in rows:
        texts, keys = [], []
        if row.get("title"):
            texts.append(row["title"]); keys.append("title_fr")
        if not row.get("snippet_fr") and row.get("snippet"):
            texts.append(row["snippet"]); keys.append("snippet_fr")
        if not texts:
            continue

        if dry_run:
            logger.info("[DRY-RUN] id=%s: preložilo by %s", row["id"], keys)
            count += 1
            continue

        try:
            translated = translate_ar_fr(api_key, texts)
            updates = dict(zip(keys, translated))
            updates["id"] = row["id"]
            set_clause = ", ".join(f"{k} = :{k}" for k in keys)
            with engine.begin() as conn:
                conn.execute(text(f"UPDATE articles SET {set_clause} WHERE id = :id"), updates)
            logger.info("Preložené AR→FR nadpis: id=%s, polia=%s", row["id"], keys)
            count += 1
        except Exception as e:
            logger.warning("DeepL zlyhal pre id=%s: %s", row["id"], e)

    return count


def main() -> int:
    p = argparse.ArgumentParser(description="Hromadná extrakcia článkov bez content_text.")
    p.add_argument("--limit", type=int, default=50, help="Max počet článkov (default 50)")
    p.add_argument("--dry-run", action="store_true", help="Len vypíše čo by urobil, nezapisuje")
    args = p.parse_args()

    engine = get_db_engine()

    with engine.begin() as conn:
        pending = fetch_pending(conn, args.limit)

    if not pending:
        logger.info("Žiadne články na extrakciu.")
        return 0

    logger.info("Pending: %s článkov (limit=%s)", len(pending), args.limit)

    counts = {"ok": 0, "soft_deleted": 0, "commercial": 0, "failed": 0}
    last_domain_fetch: dict[str, float] = {}
    DOMAIN_DELAY = 1.5  # min. sekúnd medzi requestmi na rovnakú doménu

    for row in pending:
        article_id = row["id"]
        url = row["fetch_url"]
        if not url:
            logger.warning("id=%s: žiadna URL, preskakujem", article_id)
            continue

        domain = urlparse(url).netloc
        since_last = time.time() - last_domain_fetch.get(domain, 0.0)
        if since_last < DOMAIN_DELAY:
            time.sleep(DOMAIN_DELAY - since_last)

        logger.info("Fetching id=%s: %s", article_id, url)
        candidate = CandidateArticle(
            url=url,
            title=row.get("title"),
            snippet=row.get("snippet"),
        )
        result = extract_candidate(candidate)
        last_domain_fetch[domain] = time.time()

        if args.dry_run:
            status = "ok" if result.extraction_ok else "failed"
            if result.extraction_ok and result.no_slovak_context:
                status = "soft_deleted"
            elif result.extraction_ok and result.is_commercial:
                status = "commercial"
            logger.info("[DRY-RUN] id=%s → %s", article_id, status)
            counts[status] += 1
            continue

        with engine.begin() as conn:
            status = save_result(conn, article_id, result, row.get("published_at_real"))

        translate_after_extraction(article_id, result, row)
        counts[status] += 1
        logger.info("id=%s → %s", article_id, status)
        time.sleep(0.3)

    logger.info(
        "Hotovo: ok=%s, soft_deleted=%s, commercial=%s, failed=%s",
        counts["ok"], counts["soft_deleted"], counts["commercial"], counts["failed"]
    )

    translated = translate_missing_titles(args.limit, args.dry_run)
    logger.info("Preložených nadpisov: %s", translated)
    return 0


if __name__ == "__main__":
    sys.exit(main())