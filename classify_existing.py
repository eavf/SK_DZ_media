"""
classify_existing.py — Spätná AI klasifikácia existujúcich článkov (šport áno/nie).

Prejde extrahované články, ktoré ešte neboli klasifikované (ai_checked_at IS NULL),
nie sú zmazané a nemajú ručne nastavenú relevanciu. Športové soft-deletne a označí
ako irelevantné — viď classify_ai.py.

Použitie (CLI):
    python classify_existing.py                    # dry-run: zavolá AI, vypíše výsledky, nič nezapíše
    python classify_existing.py --apply            # skutočne zapíše
    python classify_existing.py --apply --limit 200
"""
from __future__ import annotations

import argparse
import sys

from config.config import init_cli


def main() -> int:
    _, _, logger = init_cli("classify_existing")

    from classify_ai import classify_pending, is_enabled

    p = argparse.ArgumentParser(description="Spätná AI klasifikácia článkov (šport).")
    p.add_argument("--limit", type=int, default=50, help="Max počet článkov (default 50)")
    p.add_argument("--apply", action="store_true", help="Zapísať výsledky do DB (inak dry-run)")
    args = p.parse_args()

    if not is_enabled():
        logger.error("AI klasifikácia je vypnutá — nastav NVIDIA_API_KEY (a AI_CLASSIFY_ENABLED=1) v .env")
        return 1

    counts = classify_pending(limit=args.limit, apply=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] sport={counts['sport']}, other={counts['other']}, failed={counts['failed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
