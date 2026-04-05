"""
refetch_article.py — Manuálny re-fetch a extrakcia jedného článku podľa DB ID.

Použitie:
    python refetch_article.py --article-id 123

Exituje s kódom 0 (OK) alebo 1 (chyba).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import text

# extract_articles_reworked konfiguruje root logging pri importe
from extract_articles_reworked import CandidateArticle, extract_candidate
from config.config import get_db_engine, init_context
from translate import translate_ar_fr

s, paths = init_context()
logger = logging.getLogger("refetch_article")


def _to_mysql_dt(iso: str | None) -> str | None:
    """Konvertuje ISO 8601 string na MySQL DATETIME formát 'YYYY-MM-DD HH:MM:SS'."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def fetch_article_url(article_id: int) -> dict | None:
    engine = get_db_engine()
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT COALESCE(final_url, url) AS fetch_url, title, snippet,
                   title_fr, snippet_fr, language
            FROM articles
            WHERE id = :id
        """), {"id": article_id}).mappings().fetchone()
    return dict(row) if row else None


def save_extraction(article_id: int, result) -> None:
    engine = get_db_engine()
    with engine.begin() as conn:
        published_at_real = _to_mysql_dt(result.published_at_real)
        auto_irrelevant = result.extraction_ok and (result.is_commercial or result.no_slovak_context)
        conn.execute(text("""
            UPDATE articles
            SET final_url            = :final_url,
                final_url_canonical  = :final_url_canonical,
                final_url_hash       = :final_url_hash,
                fetched_at           = NOW(),
                http_status          = :http_status,
                fetch_error          = :fetch_error,
                content_text         = :content_text,
                content_hash         = :content_hash,
                published_at_real    = :published_at_real,
                published_conf       = :published_conf,
                lang_detected        = :lang_detected,
                extraction_ok        = :extraction_ok,
                relevance            = CASE WHEN relevance IS NULL AND :auto_irrelevant THEN 0 ELSE relevance END
            WHERE id = :id
        """), {
            "final_url":           result.final_url,
            "final_url_canonical": result.final_url_canonical,
            "final_url_hash":      result.final_url_hash,
            "http_status":         result.http_status,
            "fetch_error":         result.fetch_error,
            "content_text":        result.content_text,
            "content_hash":        result.content_hash,
            "published_at_real":   published_at_real,
            "published_conf":      "absolute" if published_at_real else None,
            "lang_detected":       result.lang_detected,
            "extraction_ok":       1 if result.extraction_ok else 0,
            "auto_irrelevant":     1 if auto_irrelevant else 0,
            "id":                  article_id,
        })


def main() -> int:
    p = argparse.ArgumentParser(description="Re-fetch a single article by DB ID.")
    p.add_argument("--article-id", type=int, required=True, help="articles.id to refetch")
    args = p.parse_args()

    article_id = args.article_id
    logger.info("refetch_article started: id=%s", article_id)

    row = fetch_article_url(article_id)
    if not row:
        logger.error("Article not found: id=%s", article_id)
        return 1

    url = row["fetch_url"]
    if not url:
        logger.error("Article has no URL: id=%s", article_id)
        return 1

    candidate = CandidateArticle(
        url=url,
        title=row.get("title"),
        snippet=row.get("snippet"),
    )

    logger.info("Fetching: %s", url)
    result = extract_candidate(candidate)

    save_extraction(article_id, result)

    if result.extraction_ok:
        _translate_if_arabic(article_id, result, row)
        logger.info("OK: id=%s, chars=%s", article_id, result.extracted_chars)
        return 0
    else:
        logger.error("FAIL: id=%s, error=%s", article_id, result.fetch_error)
        return 1


def _translate_if_arabic(article_id: int, result, row: dict) -> None:
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


if __name__ == "__main__":
    sys.exit(main())