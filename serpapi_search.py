import os
import json
import time
from datetime import date, timedelta
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("SERPAPI_KEY")
if not api_key:
    raise SystemExit("Missing SERPAPI_KEY environment variable.")


SERPAPI_ENDPOINT = "https://serpapi.com/search.json"


def compute_after_date(days: int = 7) -> str:
    """Return YYYY-MM-DD for 'days' ago (calendar days)."""
    d = date.today() - timedelta(days=days)
    return d.isoformat()


def build_queries(after_date: str) -> list[str]:
    slovakia = '("Slovaquie" OR slovaque OR "Slovakia" OR Slovak OR سلوفاكيا OR سلوفاكي)'
    algeria  = '("Algérie" OR "Algeria" OR Alger OR "Algiers" OR الجزائر OR "الجزائر العاصمة")'
    preferred = "(site:lalgerieaujourdhui.dz OR site:radioalgerie.dz OR site:horizons.dz)"

    # Tip: drž počet queries nízky; každá položka = 1 SerpAPI request.
    return [
        # 1) Hlavný zber – všetko na .dz
        f"site:.dz {slovakia} after:{after_date}",

        # 2) Preferované .dz zdroje – stabilné linky (nie ako jediný zdroj, len priorita)
        f"{preferred} {slovakia} after:{after_date}",

        # 3) Širšie než .dz – geo bias cez kľúčové slová (plus SerpAPI location/gl)
        f"{slovakia} {algeria} after:{after_date}",

        # 4) (Voliteľné) ak chceš chytať aj zmienky o Attafovi
        # f'("Ahmed Attaf" OR Attaf OR "أحمد عطاف") {slovakia} {algeria} after:{after_date}',
    ]


def serpapi_search(query: str, api_key: str, num: int = 20, hl: str = "fr") -> dict:
    """
    Call SerpAPI Google Search.
    Returns parsed JSON as dict.
    """
    params = {
        "engine": "google",
        "q": query,
        "hl": hl,
        "num": num,
        "api_key": api_key,
    }
    resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def save_json(data: dict, filename: str) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main() -> None:
    after_date = compute_after_date(7)
    queries = build_queries(after_date)

    all_results = []
    for q in queries:
        print(q)

    for i, q in enumerate(queries, start=1):
        print(f"[{i}/{len(queries)}] Query: {q}")
        data = serpapi_search(q, api_key=api_key, num=20, hl="fr")
        all_results.append({"query": q, "response": data})

        # Save each response separately for debugging / auditing
        ts = int(time.time())
        save_json(data, f"serpapi_{i}_{ts}.json")

        # Gentle throttle (avoid accidental bursts)
        time.sleep(1.0)

    # Save combined bundle too
    ts = int(time.time())
    save_json({"after_date": after_date, "results": all_results}, f"serpapi_bundle_{ts}.json")
    print("Done.")


if __name__ == "__main__":
    main()
