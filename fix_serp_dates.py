"""
fix_serp_dates.py — Resetuje published_at_real pre články, kde bol dátum
vypočítaný z relatívneho reťazca SerpAPI (napr. "yesterday", "il y a 3 jours").

Logika: ak published_conf = 'search' a published_at_text nie je absolútny ISO dátum
(YYYY-MM-DD), znamená to, že published_at_real bol vypočítaný od času searchu —
čo je pri re-surfovaných starých článkoch nesprávne.

Resetom na NULL necháme trafilaturu (pri ďalšom refetch) doplniť správny dátum z HTML.

Použitie:
    python fix_serp_dates.py           # dry-run: zobrazí čo by sa zmenilo
    python fix_serp_dates.py --apply   # skutočne resetuje
"""
from __future__ import annotations

import argparse
import re
import sys
import logging  # noqa: F401 — needed before init_cli sets up handlers

from sqlalchemy import text

from config.config import get_db_engine, init_cli

# Vzory, ktoré jednoznačne identifikujú relatívny dátum zo SerpAPI
_RELATIVE_RE = re.compile(
    r"il y a\b"           # "il y a X jours/heures/..."
    r"|ago\b"             # "X days ago"
    r"|\byer\b|yesterday" # "yesterday"
    r"|\bhier\b",         # "hier"
    re.IGNORECASE,
)


def main():
    _, _, logger_root = init_cli("fix_serp_dates")
    logger = logging.getLogger("fix_serp_dates")
    p = argparse.ArgumentParser(description="Reset published_at_real pre relatívne SerpAPI dátumy.")
    p.add_argument("--apply", action="store_true", help="Skutočne vykonaj UPDATE (bez toho je dry-run).")
    args = p.parse_args()

    engine = get_db_engine()

    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, published_at_text, published_at_real, last_seen_at
            FROM articles
            WHERE published_conf = 'search'
              AND published_at_real IS NOT NULL
              AND deleted_at IS NULL
        """)).mappings().fetchall()

    to_reset = [
        r for r in rows
        if r["published_at_text"] and _RELATIVE_RE.search(str(r["published_at_text"]))
    ]

    logger.info("Celkom záznamy s published_conf='search' a published_at_real IS NOT NULL: %d", len(rows))
    logger.info("Z toho na reset (relatívny alebo prázdny published_at_text): %d", len(to_reset))

    if not to_reset:
        logger.info("Nič na resetovanie.")
        return 0

    print("\nPríklady článkov na reset (max 20):")
    for r in to_reset[:20]:
        print(f"  id={r['id']:6d}  text={str(r['published_at_text'])!r:30s}  "
              f"real={r['published_at_real']}  last_seen={r['last_seen_at']}")

    if not args.apply:
        print(f"\nDRY-RUN: {len(to_reset)} záznamov by sa resetovalo. Spusti s --apply pre skutočný reset.")
        return 0

    ids = [r["id"] for r in to_reset]
    batch_size = 500
    total_updated = 0
    with engine.begin() as conn:
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            placeholders = ", ".join(f":id{j}" for j in range(len(batch)))
            params = {f"id{j}": v for j, v in enumerate(batch)}
            result = conn.execute(text(f"""
                UPDATE articles
                SET published_at_real = NULL,
                    published_conf    = NULL
                WHERE id IN ({placeholders})
            """), params)
            total_updated += result.rowcount

    logger.info("Resetovaných riadkov: %d", total_updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())