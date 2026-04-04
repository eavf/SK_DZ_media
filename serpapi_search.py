"""
serpapi_search.py — Jednoduchý standalone SerpAPI search skript.

Používa config.py pre API kľúč, search terms a nastavenia.
Výsledky ukladá do bundle_dir.

Usage:
    python serpapi_search.py [--when 7d] [--num 20]
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

import requests

from config.config import init_cli, require

s, paths, log = init_cli("serpapi_search")

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"


def build_queries() -> list[str]:
    sk = s.search_terms.get("slovakia", [])
    dz = s.search_terms.get("algeria", [])
    preferred = s.preferred_domains

    sk_part = "(" + " OR ".join(sk) + ")" if sk else ""
    dz_part = "(" + " OR ".join(f'"{t}"' if " " in t else t for t in dz) + ")" if dz else ""
    sites = "(" + " OR ".join(f"site:{d}" for d in sorted(preferred)) + ")" if preferred else "site:.dz"
    url_f = "-inurl:recherche -inurl:search -inurl:tag -inurl:tags -inurl:page"

    queries = []
    if sk_part:
        queries.append(f"site:.dz {sk_part} {url_f}")
        if preferred:
            queries.append(f"{sites} {sk_part} {url_f}")
        if dz_part:
            queries.append(f"{sk_part} {dz_part}")
    return queries


def serpapi_search(query: str, num: int, when: str) -> dict:
    params = {
        "engine": s.serp_engine or "google",
        "tbm": "nws",
        "q": query,
        "hl": s.serp_hl,
        "gl": s.serp_gl,
        "num": min(num, 10),
        "tbs": f"qdr:{when}",
        "api_key": require(s.serpapi_key, "SERPAPI_KEY"),
    }
    resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    ap = argparse.ArgumentParser(description="Standalone SerpAPI search.")
    ap.add_argument("--when", default=s.serp_when, help="Časové okno (napr. 7d, 30d)")
    ap.add_argument("--num", type=int, default=s.serp_num, help="Počet výsledkov na query")
    args = ap.parse_args()

    queries = build_queries()
    if not queries:
        raise SystemExit("Žiadne search terms — skontroluj config/search_terms.json")

    ts = int(datetime.now(timezone.utc).timestamp())
    all_results = []

    for i, q in enumerate(queries, start=1):
        log.info("[%s/%s] Query: %s", i, len(queries), q)
        data = serpapi_search(q, num=args.num, when=args.when)
        all_results.append({"query": q, "response": data})

        out = paths.bundle_dir / f"serpapi_{i}_{ts}.json"
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Uložené: %s", out)

        time.sleep(1.0)

    bundle = paths.bundle_dir / f"serpapi_bundle_{ts}.json"
    bundle.write_text(
        json.dumps({"when": args.when, "num": args.num, "results": all_results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log.info("Bundle: %s", bundle)


if __name__ == "__main__":
    main()