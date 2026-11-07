"""
backfill_run_bundles.py — Doplní priečinok runu do runs.bundle_filename pre staré ingesty.

Ingest do 11/2026 ukladal do runs.bundle_filename len 'news_bundle.json' (bez priečinka), takže
stránka Process nevedela, ktorý run z bundle/runs/ už bol ingestovaný. Skript pre každý starý záznam
v `runs` nájde bundle, z ktorého pochádzajú jeho články (url_hash článkov prepojených cez run_articles
⊆ url_hash kandidátov v bundli), a zapíše 'run_XXX/news_bundle.json'.

Opakované hľadanie v priebehu pár minút dáva bundle s rovnakými článkami — vtedy rozhodne čas:
posledný bundle vytvorený pred ingestom (run.timestamp v bundli vs. runs.started_at, oboje unix čas).

Ingesty bez prepojených článkov sa spárovať nedajú a ostanú bez zmeny.

Použitie (CLI):
    python backfill_run_bundles.py           # dry-run: zobrazí párovanie
    python backfill_run_bundles.py --apply   # zapíše
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

from config.config import get_db_engine, init_cli

# podiel článkov runu, ktoré musia byť v bundli (zvyšok: url_hash starších článkov počítaný podľa
# starších pravidiel canonicalize_url / url_cleanup.json)
MIN_CONTAINMENT = 0.75
# tolerancia hodín medzi kontajnerom a DB (s)
CLOCK_SLACK = 120


def load_bundle(bundle: Path) -> tuple[set[str], float]:
    """(url_hash všetkých kandidátov — rovnaký výpočet ako pri ingeste, čas vytvorenia bundlu)."""
    from ingest_to_dz_news_reworked import build_candidates
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    hashes = {c.url_hash for c in build_candidates(payload, preferred_domains=set(), default_query_id=None)}
    ts = (payload.get("run") or {}).get("timestamp") or bundle.stat().st_mtime
    return hashes, float(ts)


def best_bundle(run_hashes: set[str], run_ts: float,
                bundles: dict[str, tuple[set[str], float]]) -> tuple[str | None, float]:
    """Bundle s najväčším podielom článkov runu.

    Pri zhode posledný bundle vytvorený pred ingestom (run_ts); ak taký nie je, časovo najbližší.
    """
    scored = [(len(run_hashes & h) / len(run_hashes), label, ts) for label, (h, ts) in bundles.items()]
    if not scored:
        return None, 0.0
    top = max(sc for sc, _, _ in scored)
    if top == 0:
        return None, 0.0
    tied = [(label, ts) for sc, label, ts in scored if sc == top]
    before = [(label, ts) for label, ts in tied if ts <= run_ts + CLOCK_SLACK]
    if before:
        label = max(before, key=lambda x: x[1])[0]
    else:
        label = min(tied, key=lambda x: abs(x[1] - run_ts))[0]
    return label, top


def main() -> int:
    s, paths, logger = init_cli("backfill_run_bundles")
    p = argparse.ArgumentParser(description="Doplní priečinok runu do runs.bundle_filename pre staré ingesty.")
    p.add_argument("--apply", action="store_true", help="Skutočne zapíše (inak dry-run)")
    args = p.parse_args()
    prefix = "" if args.apply else "[DRY-RUN] "

    bundles: dict[str, tuple[set[str], float]] = {}
    for run_dir in sorted(paths.runs_dir.iterdir()):
        bundle = run_dir / "news_bundle.json"
        if bundle.exists():
            try:
                bundles[f"{run_dir.name}/{bundle.name}"] = load_bundle(bundle)
            except Exception as e:
                logger.warning("Bundle sa nedá načítať: %s (%s)", bundle, e)
    logger.info("Bundlov v %s: %s", paths.runs_dir, len(bundles))

    counts = {"matched": 0, "no_articles": 0, "no_match": 0}
    with get_db_engine().begin() as conn:
        rows = conn.execute(text("""
            SELECT r.id, r.started_at, UNIX_TIMESTAMP(r.started_at) AS started_ts, a.url_hash
            FROM runs r
            LEFT JOIN run_articles ra ON ra.run_id = r.id
            LEFT JOIN articles a ON a.id = ra.article_id
            WHERE r.bundle_filename IS NULL OR r.bundle_filename NOT LIKE '%/%'
        """)).mappings().fetchall()

        run_hashes: dict[int, set[str]] = defaultdict(set)
        started: dict[int, object] = {}
        started_ts: dict[int, float] = {}
        for r in rows:
            started[r["id"]] = r["started_at"]
            started_ts[r["id"]] = float(r["started_ts"])
            if r["url_hash"]:
                run_hashes[r["id"]].add(r["url_hash"])

        updates = []
        for run_id in sorted(started):
            hashes = run_hashes.get(run_id)
            if not hashes:
                counts["no_articles"] += 1
                continue
            label, score = best_bundle(hashes, started_ts[run_id], bundles)
            if not label or score < MIN_CONTAINMENT:
                logger.warning("%srun #%s (%s, %s článkov): bundle nenájdený (najlepšia zhoda %.0f %%)",
                               prefix, run_id, started[run_id], len(hashes), score * 100)
                counts["no_match"] += 1
                continue
            logger.info("%srun #%s (%s, %s článkov) → %s (%.0f %%)",
                        prefix, run_id, started[run_id], len(hashes), label, score * 100)
            updates.append({"id": run_id, "bf": label})
            counts["matched"] += 1

        if args.apply and updates:
            conn.execute(text("UPDATE runs SET bundle_filename = :bf WHERE id = :id"), updates)

    logger.info("%sHotovo: spárovaných=%s, bez článkov=%s, bez zhody=%s",
                prefix, counts["matched"], counts["no_articles"], counts["no_match"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
