"""
fix_serp_dates.py — Opravuje published_at_text a resetuje published_at_real
pre články kde bol dátum vypočítaný z relatívneho reťazca SerpAPI
(napr. "yesterday", "il y a 3 jours").

Logika:
  - published_at_text sa konvertuje na ISO dátum (YYYY-MM-DD) pomocou first_seen_at
    ako proxy za pôvodný čas fetchu
  - published_at_real sa resetuje na NULL, aby ho trafilatura pri ďalšom refetch
    doplnila zo správneho HTML meta tagu

Použitie (CLI):
    python fix_serp_dates.py           # dry-run: zobrazí čo by sa zmenilo
    python fix_serp_dates.py --apply   # skutočne opraví
"""
from __future__ import annotations

import argparse
import re
import sys
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import text

from config.config import get_db_engine, init_cli

# Vzory relatívnych dátumov zo SerpAPI
_RELATIVE_RE = re.compile(
    r"il y a\b"
    r"|ago\b"
    r"|\byer\b|yesterday"
    r"|\bhier\b",
    re.IGNORECASE,
)

_FR_REL = re.compile(
    r"il\s+y\s+a\s+(\d+)\s+(minute|minutes|heure|heures|jour|jours|semaine|semaines|mois)",
    re.IGNORECASE,
)
_EN_REL = re.compile(
    r"(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks)\s+ago",
    re.IGNORECASE,
)


def _resolve_for_fix(date_str: str, fetched_at: datetime) -> Optional[str]:
    """Konvertuje relatívny SerpAPI reťazec na ISO dátum pomocou fetched_at.
    Vracia ISO string alebo None ak sa nedá parsovať."""
    s = date_str.strip()

    m = _FR_REL.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("minute"):
            delta = timedelta(minutes=n)
        elif unit.startswith("heure"):
            delta = timedelta(hours=n)
        elif unit.startswith("jour"):
            delta = timedelta(days=n)
        elif unit.startswith("semaine"):
            delta = timedelta(weeks=n)
        else:  # mois
            delta = timedelta(days=n * 30)
        return (fetched_at - delta).date().isoformat()

    m = _EN_REL.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("minute"):
            delta = timedelta(minutes=n)
        elif unit.startswith("hour"):
            delta = timedelta(hours=n)
        elif unit.startswith("day"):
            delta = timedelta(days=n)
        else:  # week
            delta = timedelta(weeks=n)
        return (fetched_at - delta).date().isoformat()

    if re.match(r"(yesterday|hier)$", s, re.IGNORECASE):
        return (fetched_at - timedelta(days=1)).date().isoformat()

    return None


def fix_relative_dates(apply: bool = False, engine=None) -> dict:
    """Opraví published_at_text a resetuje published_at_real pre relatívne dátumy.

    Vracia dict so štatistikami:
      total_candidates  — počet článkov s published_conf='search' a IS NOT NULL real
      to_fix            — z toho s relatívnym published_at_text
      updated           — počet skutočne opravených (len ak apply=True)
      preview           — zoznam dict pre prvých 20 záznamov (pre UI náhľad)
    """
    if engine is None:
        engine = get_db_engine()

    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, published_at_text, published_at_real, published_conf, first_seen_at
            FROM articles
            WHERE (
                (published_conf = 'search' AND published_at_real IS NOT NULL)
                OR published_at_text REGEXP 'il y a|ago|yesterday|hier'
            )
            AND COALESCE(published_conf, '') <> 'absolute'
            AND deleted_at IS NULL
        """)).mappings().fetchall()
    rows = [dict(r) for r in rows]

    to_fix = []
    other_candidates = []
    for r in rows:
        raw = str(r["published_at_text"] or "")
        if not _RELATIVE_RE.search(raw):
            other_candidates.append({
                "id": r["id"],
                "published_at_text": raw,
                "published_at_real": r["published_at_real"],
                "published_conf": r.get("published_conf"),
                "first_seen_at": str(r["first_seen_at"]),
            })
            continue
        fetched_at = r["first_seen_at"]
        if isinstance(fetched_at, str):
            try:
                fetched_at = datetime.fromisoformat(fetched_at)
            except Exception:
                fetched_at = datetime.now()
        elif fetched_at is None:
            fetched_at = datetime.now()
        computed = _resolve_for_fix(raw, fetched_at)
        to_fix.append({
            "id": r["id"],
            "published_at_text_old": raw,
            "published_at_text_new": computed,
            "published_at_real": r["published_at_real"],
            "first_seen_at": str(r["first_seen_at"]),
        })

    updated = 0
    if apply and to_fix:
        batch_size = 500
        with engine.begin() as conn:
            for i in range(0, len(to_fix), batch_size):
                batch = to_fix[i:i + batch_size]
                for item in batch:
                    conn.execute(text("""
                        UPDATE articles
                        SET published_at_text = :new_text
                        WHERE id = :id
                    """), {"new_text": item["published_at_text_new"], "id": item["id"]})
                    updated += 1

    return {
        "total_candidates": len(rows),
        "to_fix": len(to_fix),
        "updated": updated,
        "preview": to_fix[:20],
        "other_candidates": other_candidates,
    }


def main():
    _, _, _ = init_cli("fix_serp_dates")
    logger = logging.getLogger("fix_serp_dates")
    p = argparse.ArgumentParser(description="Opraví published_at_text a resetuje published_at_real pre relatívne SerpAPI dátumy.")
    p.add_argument("--apply", action="store_true", help="Skutočne vykonaj UPDATE (bez toho je dry-run).")
    args = p.parse_args()

    stats = fix_relative_dates(apply=args.apply)

    logger.info("Kandidátov v DB: %d", stats["total_candidates"])
    logger.info("Na opravu (relatívny text): %d", stats["to_fix"])

    if stats["preview"]:
        print("\nPríklady (max 20):")
        for r in stats["preview"]:
            print(f"  id={r['id']:6d}  {r['published_at_text_old']!r:30s}  →  {r['published_at_text_new']!r}")

    if not args.apply:
        print(f"\nDRY-RUN: {stats['to_fix']} záznamov by sa opravilo. Spusti s --apply pre skutočnú opravu.")
    else:
        logger.info("Opravených riadkov: %d", stats["updated"])

    return 0


if __name__ == "__main__":
    sys.exit(main())