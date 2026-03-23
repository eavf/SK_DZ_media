import os
import json
import time
from datetime import date, timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv
from serpapi import GoogleSearch


PREFERRED_DOMAINS = {
    "lalgerieaujourdhui.dz",
    "radioalgerie.dz",
    "horizons.dz",
}


def compute_after_date(days: int = 7) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def build_query_dz(after_date: str) -> str:
    slovakia = '("Slovaquie" OR slovaque OR "Slovakia" OR Slovak OR سلوفاكيا OR سلوفاكي)'
    return f"site:.dz {slovakia} after:{after_date}"


def build_query_non_dz(after_date: str) -> str:
    slovakia = '("Slovaquie" OR slovaque OR "Slovakia" OR Slovak OR سلوفاكيا OR سلوفاكي)'
    algeria = '("Algérie" OR "Algeria" OR Alger OR "Algiers" OR الجزائر OR "الجزائر العاصمة")'
    return f"{slovakia} {algeria} after:{after_date}"


def build_query_preferred_fallback(after_date: str) -> str:
    slovakia = '("Slovaquie" OR slovaque OR "Slovakia" OR Slovak OR سلوفاكيا OR سلوفاكي)'
    preferred = "(site:lalgerieaujourdhui.dz OR site:radioalgerie.dz OR site:horizons.dz)"
    return f"{preferred} {slovakia} after:{after_date}"


def serpapi_search(query: str, api_key: str, num: int, hl: str, gl: str, location: str | None) -> dict:
    params = {
        "engine": "google",
        "q": query,
        "num": num,
        "hl": hl,
        "gl": gl,
        "api_key": api_key,
    }
    if location:
        params["location"] = location

    search = GoogleSearch(params)
    return search.get_dict()


def save_json(data: dict, filename: str) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def extract_links(results: dict) -> list[str]:
    links = []
    for item in (results.get("organic_results") or []):
        link = item.get("link")
        if link:
            links.append(link)
    return links


def contains_preferred(links: list[str]) -> bool:
    for url in links:
        if get_domain(url) in PREFERRED_DOMAINS:
            return True
    return False


def is_full_page(results: dict, num: int) -> bool:
    return len(results.get("organic_results") or []) >= num


def should_run_preferred_fallback(dz_results: dict, non_dz_results: dict, num: int) -> bool:
    dz_full = is_full_page(dz_results, num)
    non_dz_full = is_full_page(non_dz_results, num)

    dz_links = extract_links(dz_results)
    non_dz_links = extract_links(non_dz_results)

    any_preferred = contains_preferred(dz_links + non_dz_links)

    # Fallback len ak: Q1 full AND Q2 full AND (no preferred in Q1+Q2)
    return dz_full and non_dz_full and (not any_preferred)


def brief_stats(label: str, results: dict, num: int) -> dict:
    links = extract_links(results)
    domains = [get_domain(u) for u in links]
    preferred_count = sum(1 for d in domains if d in PREFERRED_DOMAINS)
    return {
        "label": label,
        "organic_count": len(results.get("organic_results") or []),
        "requested_num": num,
        "is_full": is_full_page(results, num),
        "preferred_count": preferred_count,
        "unique_domains": len(set(d for d in domains if d)),
    }


def main() -> None:
    load_dotenv()
    api_key = os.getenv("SERPAPI_KEY")
    if not api_key:
        raise SystemExit("Missing SERPAPI_KEY in environment/.env")

    # Nastavenia (uprav podľa potreby)
    num = 20
    hl = "fr"          # jazyk SERP rozhrania; AR termy fungujú aj tak
    gl = "dz"          # geo bias krajina
    location = "Algeria"  # geo bias; môžeš skúsiť aj "Algiers, Algeria"

    after_date = compute_after_date(7)
    ts = int(time.time())

    q1 = build_query_dz(after_date)
    print(f"[1/2] Q1 (DZ)      : {q1}")
    r1 = serpapi_search(q1, api_key=api_key, num=num, hl=hl, gl=gl, location=location)
    save_json(r1, f"serpapi_q1_dz_{ts}.json")

    time.sleep(1.0)

    q2 = build_query_non_dz(after_date)
    print(f"[2/2] Q2 (NON-DZ)  : {q2}")
    r2 = serpapi_search(q2, api_key=api_key, num=num, hl=hl, gl=gl, location=location)
    save_json(r2, f"serpapi_q2_non_dz_{ts}.json")

    stats1 = brief_stats("Q1", r1, num)
    stats2 = brief_stats("Q2", r2, num)

    combined_links = extract_links(r1) + extract_links(r2)
    any_pref = contains_preferred(combined_links)

    print("\n--- STATS ---")
    print(f"Q1: organic={stats1['organic_count']}/{num}, full={stats1['is_full']}, preferred={stats1['preferred_count']}, domains={stats1['unique_domains']}")
    print(f"Q2: organic={stats2['organic_count']}/{num}, full={stats2['is_full']}, preferred={stats2['preferred_count']}, domains={stats2['unique_domains']}")
    print(f"Preferred present in Q1+Q2: {any_pref}")

    r3 = None
    q3 = None

    if should_run_preferred_fallback(r1, r2, num=num):
        time.sleep(1.0)
        q3 = build_query_preferred_fallback(after_date)
        print(f"\n[+fallback] Q3 (PREFERRED): {q3}")
        r3 = serpapi_search(q3, api_key=api_key, num=num, hl=hl, gl=gl, location=location)
        save_json(r3, f"serpapi_q3_preferred_{ts}.json")
    else:
        print("\n[no fallback] Conditions not met.")

    bundle = {
        "run": {
            "timestamp": ts,
            "after_date": after_date,
            "num": num,
            "hl": hl,
            "gl": gl,
            "location": location,
            "fallback_triggered": r3 is not None,
        },
        "queries": {"q1": q1, "q2": q2, "q3": q3},
        "responses": {"q1": r1, "q2": r2, "q3": r3},
        "stats": {"q1": stats1, "q2": stats2},
    }
    save_json(bundle, f"serpapi_bundle_{ts}.json")
    print(f"\nSaved bundle: serpapi_bundle_{ts}.json")
    print("Done.")


if __name__ == "__main__":
    main()
