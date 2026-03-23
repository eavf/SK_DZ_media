from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
import argparse

import requests
import trafilatura
from sqlalchemy import text

from config.config import get_db_engine, init_context, init_cli


# ---------------------------------------------------------------------------
# Context / logging
# ---------------------------------------------------------------------------

s, paths = init_context()
logger = logging.getLogger("extract_articles")

max_articles = s.extract_max
only_missing = s.extract_only_missing
DROP_QUERY_KEYS = set(s.drop_query_keys or set())

DEBUG_HTML_DIR = paths.project_root / "debug_html"
DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)

AGGREGATOR_DOMAINS = {
    "msn.com",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def domain_root(netloc: str) -> str:
    n = (netloc or "").lower()
    if n.startswith("www."):
        n = n[4:]
    parts = n.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return n


def canonicalize_url(url: str) -> str:
    if not url:
        return ""

    p = urlparse(url.strip())
    scheme = (p.scheme or "https").lower()

    netloc = (p.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    q = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if k in DROP_QUERY_KEYS:
            continue
        q.append((k, v))
    q.sort()
    query = urlencode(q, doseq=True)

    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    return urlunparse((scheme, netloc, path, "", query, ""))


def sha256_hex(k: str) -> str:
    return hashlib.sha256(k.encode("utf-8")).hexdigest()


def clamp(k: str | None, max_len: int = 1000) -> str | None:
    if k is None:
        return None
    k = str(k)
    return k if len(k) <= max_len else (k[: max_len - 3] + "...")


def is_js_shell(html: str) -> bool:
    h = (html or "").lower()
    if '<div id="root"' in h and 'bundles/v1/views/latest' in h:
        return True
    if 'data-ssr-entry' in h and 'ssr-service-entry' in h:
        return True
    return False


def resolve_and_fetch(url: str, timeout=(10, 25)) -> tuple[str, int, str]:
    """
    Returns (final_url, status_code, html_text).
    Raises requests exceptions on network errors.
    """
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "fr,en;q=0.8,ar;q=0.7",
    })

    resp = sess.get(url, allow_redirects=True, timeout=timeout)
    resp.raise_for_status()
    return resp.url, resp.status_code, resp.text


def extract_text_and_metadata(html: str) -> tuple[str | None, datetime | None, str | None]:
    """
    Returns (text, published_at_real, lang_detected).
    """
    text_out = trafilatura.extract(html, include_comments=False, include_tables=False)
    if not text_out or not text_out.strip():
        return None, None, None

    meta = trafilatura.metadata.extract_metadata(html)
    published_dt = None
    lang = None

    if meta:
        lang = getattr(meta, "language", None)
        d = getattr(meta, "date", None)
        if d:
            if isinstance(d, datetime):
                published_dt = d
            else:
                try:
                    published_dt = datetime.fromisoformat(str(d))
                except Exception:
                    published_dt = None

    return text_out, published_dt, lang


def save_debug_html(article_id: int, html: str, url: str | None = None, final_url: str | None = None) -> None:
    try:
        ref_url = final_url or url or ""
        host = urlparse(ref_url).netloc.replace(":", "_") if ref_url else "unknown"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_file = DEBUG_HTML_DIR / f"fail_{article_id}_{host}_{ts}.html"
        debug_file.write_text(html or "", encoding="utf-8")
    except Exception:
        logger.exception("Failed to save debug HTML for article_id=%s", article_id)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_one(article_id: int, engine=None) -> dict:
    """
    Fetch + extract a single article by ID and store results in DB.
    Returns a small dict for UI/API use.
    """
    engine = engine or get_db_engine()

    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT id, COALESCE(final_url, url) AS fetch_url
            FROM articles
            WHERE id = :id
        """), {"id": article_id}).mappings().fetchone()

    if not row:
        return {"ok": False, "error": "NOT_FOUND"}

    url = row["fetch_url"]
    if not url:
        return {"ok": False, "error": "NO_URL"}

    ok = False
    http_status = None
    fetch_error = None
    final_url = None
    extracted_chars = 0

    with engine.begin() as conn:
        try:
            final_url, status, html = resolve_and_fetch(url)
            http_status = status

            final_canon = canonicalize_url(final_url)
            final_hash = sha256_hex(final_canon) if final_canon else None

            netloc = urlparse(final_url).netloc
            root = domain_root(netloc)

            if root in AGGREGATOR_DOMAINS:
                fetch_error = "AGGREGATOR"
                conn.execute(text("""
                    UPDATE articles
                    SET final_url=:final_url,
                        final_url_canonical=:final_canon,
                        final_url_hash=:final_hash,
                        fetched_at=NOW(),
                        http_status=:status,
                        fetch_error=:err,
                        extraction_ok=0
                    WHERE id=:id
                """), {
                    "final_url": final_url,
                    "final_canon": final_canon,
                    "final_hash": final_hash,
                    "status": status,
                    "err": fetch_error,
                    "id": article_id,
                })
                return {
                    "ok": False,
                    "http_status": status,
                    "fetch_error": fetch_error,
                    "final_url": final_url,
                    "extracted_chars": 0,
                }

            text_out, published_real, lang = extract_text_and_metadata(html)

            if not text_out:
                save_debug_html(article_id, html, url=url, final_url=final_url)
                fetch_error = "JS_RENDER_REQUIRED" if is_js_shell(html) else "EXTRACTION_EMPTY"

                conn.execute(text("""
                    UPDATE articles
                    SET final_url=:final_url,
                        final_url_canonical=:final_canon,
                        final_url_hash=:final_hash,
                        fetched_at=NOW(),
                        http_status=:status,
                        fetch_error=:err,
                        extraction_ok=0
                    WHERE id=:id
                """), {
                    "final_url": final_url,
                    "final_canon": final_canon,
                    "final_hash": final_hash,
                    "status": status,
                    "err": fetch_error,
                    "id": article_id,
                })
                return {
                    "ok": False,
                    "http_status": status,
                    "fetch_error": fetch_error,
                    "final_url": final_url,
                    "extracted_chars": 0,
                }

            content_hash = sha256_hex(text_out)
            extracted_chars = len(text_out)
            ok = True

            conn.execute(text("""
                UPDATE articles
                SET final_url=:final_url,
                    final_url_canonical=:final_canon,
                    final_url_hash=:final_hash,
                    fetched_at=NOW(),
                    http_status=:status,
                    fetch_error=NULL,
                    content_text=:content_text,
                    content_hash=:content_hash,
                    published_at_real=:published_at_real,
                    lang_detected=:lang_detected,
                    extraction_ok=1
                WHERE id=:id
            """), {
                "final_url": final_url,
                "final_canon": final_canon,
                "final_hash": final_hash,
                "status": status,
                "content_text": text_out,
                "content_hash": content_hash,
                "published_at_real": published_real,
                "lang_detected": lang,
                "id": article_id,
            })

        except requests.exceptions.ConnectionError:
            fetch_error = "NETWORK_UNREACHABLE"
            conn.execute(text("""
                UPDATE articles
                SET fetched_at=NOW(),
                    http_status=NULL,
                    fetch_error=:err,
                    extraction_ok=0
                WHERE id=:id
            """), {"err": fetch_error, "id": article_id})

        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            http_status = status
            fetch_error = clamp(f"HTTPError: {e}", 1000)
            conn.execute(text("""
                UPDATE articles
                SET fetched_at=NOW(),
                    http_status=:status,
                    fetch_error=:err,
                    extraction_ok=0
                WHERE id=:id
            """), {"status": status, "err": fetch_error, "id": article_id})

        except requests.exceptions.Timeout:
            fetch_error = "TIMEOUT"
            conn.execute(text("""
                UPDATE articles
                SET fetched_at=NOW(),
                    http_status=NULL,
                    fetch_error=:err,
                    extraction_ok=0
                WHERE id=:id
            """), {"err": fetch_error, "id": article_id})

        except Exception as e:
            fetch_error = clamp(f"{type(e).__name__}: {e}", 1000)
            conn.execute(text("""
                UPDATE articles
                SET fetched_at=NOW(),
                    http_status=:status,
                    fetch_error=:err,
                    extraction_ok=0
                WHERE id=:id
            """), {"status": http_status, "err": fetch_error, "id": article_id})

    return {
        "ok": ok,
        "http_status": http_status,
        "fetch_error": fetch_error,
        "final_url": final_url,
        "extracted_chars": extracted_chars,
    }


def run_extraction(
    engine=None,
    *,
    limit: int | None = None,
    only_missing_flag: bool | None = None,
    article_id: int | None = None,
) -> dict:
    """
    Shared entry point usable from Flask or CLI.
    """
    engine = engine or get_db_engine()
    lim = limit if limit is not None else max_articles
    only_missing_effective = only_missing_flag if only_missing_flag is not None else only_missing

    if article_id is not None:
        result = extract_one(article_id, engine=engine)
        return {
            "ok": 1 if result.get("ok") else 0,
            "fail": 0 if result.get("ok") else 1,
            "total": 1,
            "message": (
                f"Extraction done for article_id={article_id}. "
                f"ok={1 if result.get('ok') else 0}, "
                f"fail={0 if result.get('ok') else 1}"
            ),
            "results": [result],
        }

    with engine.begin() as conn:
        if only_missing_effective:
            rows = conn.execute(text("""
                SELECT id, url
                FROM articles
                WHERE extraction_ok = 0
                ORDER BY last_seen_at DESC
                LIMIT :lim
            """), {"lim": lim}).fetchall()
        else:
            rows = conn.execute(text("""
                SELECT id, url
                FROM articles
                ORDER BY last_seen_at DESC
                LIMIT :lim
            """), {"lim": lim}).fetchall()

    if not rows:
        return {"ok": 0, "fail": 0, "total": 0, "message": "Nothing to extract."}

    ok_count = 0
    fail_count = 0
    results = []

    for article_id_row, _url in rows:
        result = extract_one(article_id_row, engine=engine)
        results.append(result)
        if result.get("ok"):
            ok_count += 1
        else:
            fail_count += 1

    return {
        "ok": ok_count,
        "fail": fail_count,
        "total": len(rows),
        "message": f"Extraction done. ok={ok_count}, fail={fail_count}, total={len(rows)}",
        "results": results,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fetch and extract article content into dz_news DB."
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of articles to process. Default comes from config.extract_max.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Process recent articles regardless of extraction_ok flag.",
    )
    p.add_argument(
        "--article-id",
        type=int,
        default=None,
        help="Extract exactly one article by DB id.",
    )
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global s, paths, logger, max_articles, only_missing, DROP_QUERY_KEYS, DEBUG_HTML_DIR

    s, paths, logger = init_cli("extract_articles")

    max_articles = s.extract_max
    only_missing = s.extract_only_missing
    DROP_QUERY_KEYS = set(s.drop_query_keys or set())

    DEBUG_HTML_DIR = paths.project_root / "debug_html"
    DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)

    parser = build_arg_parser()
    args = parser.parse_args()

    effective_limit = args.limit if args.limit is not None else max_articles
    effective_only_missing = False if args.all else only_missing

    logger.info(
        "Config loaded: extract_max=%s, only_missing=%s, url_cleanup_keys=%s",
        max_articles,
        only_missing,
        len(DROP_QUERY_KEYS),
    )

    logger.info(
        "Runtime options: limit=%s, only_missing=%s, article_id=%s",
        effective_limit,
        effective_only_missing,
        args.article_id,
    )

    result = run_extraction(
        limit=effective_limit,
        only_missing_flag=effective_only_missing,
        article_id=args.article_id,
    )
    print(result["message"])


if __name__ == "__main__":
    main()