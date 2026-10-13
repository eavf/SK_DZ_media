"""
translate_missing.py — Dodatočný preklad AR→FR pre články s textom, ale bez content_text_fr.

Preloží aktívne arabské články (content_text, chýbajúce title_fr/snippet_fr sa doplnia tiež)
cez backend zvolený v /admin/maintenance (config/translator_backend) alebo TRANSLATOR_BACKEND z .env.
Články označené ako irelevantné (relevance=0) sa preskakujú — šetrí sa kredit prekladača.

Použitie (CLI):
    python translate_missing.py                    # dry-run: vypíše články a počet znakov, nič neprekladá
    python translate_missing.py --apply            # skutočne preloží a zapíše
    python translate_missing.py --apply --limit 10
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from config.config import get_db_engine, init_cli

MAX_CONSECUTIVE_FAILURES = 3
# HTTP stavy, pri ktorých nemá zmysel pokračovať: DeepL 456 (kredit), Azure 403 (kvóta/kľúč)
_STOP_STATUSES = {403, 456}


def fetch_missing(conn, limit: int) -> list[dict]:
    rows = conn.execute(text("""
        SELECT id, title, snippet, title_fr, snippet_fr, content_text
        FROM articles
        WHERE deleted_at IS NULL
          AND content_text IS NOT NULL
          AND content_text_fr IS NULL
          AND COALESCE(language, lang_detected) = 'ar'
          AND COALESCE(relevance, 1) <> 0
        ORDER BY id DESC
        LIMIT :limit
    """), {"limit": limit}).mappings().fetchall()
    return [dict(r) for r in rows]


def main() -> int:
    s, _, logger = init_cli("translate_missing")

    from translate import get_translator_backend, translate_ar_fr

    p = argparse.ArgumentParser(description="Dodatočný preklad AR→FR článkov bez content_text_fr.")
    p.add_argument("--limit", type=int, default=100, help="Max počet článkov (default 100)")
    p.add_argument("--apply", action="store_true", help="Preložiť a zapísať do DB (inak dry-run)")
    args = p.parse_args()

    engine = get_db_engine()
    with engine.begin() as conn:
        rows = fetch_missing(conn, args.limit)

    backend = get_translator_backend()
    total_chars = sum(len(r["content_text"]) for r in rows)
    logger.info("Na preklad: %s článkov, %s znakov textu (backend: %s)", len(rows), total_chars, backend)

    if not args.apply:
        for r in rows:
            logger.info("[DRY-RUN] id=%s, %s znakov: %s", r["id"], len(r["content_text"]), (r["title"] or "")[:60])
        return 0

    counts = {"ok": 0, "failed": 0}
    consecutive_failures = 0
    for r in rows:
        texts, keys = [r["content_text"]], ["content_text_fr"]
        if not r["title_fr"] and r["title"]:
            texts.append(r["title"]); keys.append("title_fr")
        if not r["snippet_fr"] and r["snippet"]:
            texts.append(r["snippet"]); keys.append("snippet_fr")

        try:
            translated = translate_ar_fr(texts, api_key=s.deepl_api_key)
        except Exception as e:
            counts["failed"] += 1
            consecutive_failures += 1
            logger.warning("Preklad zlyhal pre id=%s: %s", r["id"], e)
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in _STOP_STATUSES:
                logger.error("Prekladač vrátil HTTP %s (kredit/kvóta/kľúč) — končím.", status)
                break
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error("%s zlyhaní za sebou — končím.", consecutive_failures)
                break
            continue

        updates = dict(zip(keys, translated))
        updates["id"] = r["id"]
        set_clause = ", ".join(f"{k} = :{k}" for k in keys)
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE articles SET {set_clause} WHERE id = :id"), updates)
        counts["ok"] += 1
        consecutive_failures = 0
        logger.info("Preložené id=%s, polia=%s", r["id"], keys)

    logger.info("Hotovo: preložených=%s, zlyhaní=%s (backend: %s)", counts["ok"], counts["failed"], backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
