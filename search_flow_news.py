import json
import time
from urllib.parse import urlparse

from serpapi import GoogleSearch
from datetime import datetime, timedelta
import argparse
from pathlib import Path
from collections import Counter

import logging
from config.config import init_context, init_cli, require

# ----------------------------
# CONFIG
# ----------------------------

s, paths = init_context()
logger = logging.getLogger("search_flow_news")

PREFERRED_DOMAINS = s.preferred_domains
SOURCE_RANK = s.source_rank
TOPIC_KEYWORDS = s.topic_keywords
api_key = s.serpapi_key

logger.info(
    f"Config loaded: {len(PREFERRED_DOMAINS)} preferred domains, "
    f"{len(SOURCE_RANK)} ranked sources, "
    f"{len(TOPIC_KEYWORDS)} topic groups"
)

SLOVAKIA_TERMS = (
    "Slovaquie",
    "slovaque",
    "Slovakia",
    "Slovak",
    "Bratislava",
    "سلوفاكيا",
    "سلوفاكي",
)

# Domény, ktoré často vracajú "ne-news" výsledky
BLOCKLIST_DOMAINS = {
    "mobilis.dz",
    "stepmode.dz",
    "translate.google.dz",
    "ems.dz",
    "elhanaa.cnas.dz",
}

# URL path markery typické pre e-commerce/ponuky
ECOM_PATH_MARKERS = (
    "/produit/", "/product/", "/shop/", "/cart/", "/panier/",
    "/offers", "/offre", "/pass", "/promo",
)

# URL path markery typické pre search/tag/archive/pagination stránky
BAD_PATH_MARKERS = (
    "/recherche",
    "/search",
    "/tag/",
    "/tags/",
    "/page/",
    "/opac/",
    "/company-location/",
    "/embassys-locations-list",
    "/?m=",
)

# Slová v titulkoch, ktoré často signalizujú "obchodné" výsledky
TITLE_BAD_WORDS = (
    "promo", "promotion", "prix", "offre", "pass", "roaming",
    "سعر", "عرض", "تخفيضات", "شراء",
)

WHEN_MAP = {
    "1d": "d",
    "7d": "w",
    "30d": "m"
}



# ----------------------------
# QUERY BUILDING
# ----------------------------

def slovakia_terms() -> str:
    return "(" + " OR ".join(SLOVAKIA_TERMS) + ")"

def mentions_slovakia(item: dict) -> bool:
    text = f"{item.get('title','')} {item.get('snippet','')}".lower()
    return any(term.lower() in text for term in SLOVAKIA_TERMS)

def algeria_terms() -> str:
    return '("Algérie" OR "Algeria" OR Alger OR "Algiers" OR الجزائر OR "الجزائر العاصمة")'

def unwanted_url_filters() -> str:
    return "-inurl:recherche -inurl:search -inurl:tag -inurl:tags -inurl:page"

def build_site_or(domains: set[str]) -> str:
    if not domains:
        raise ValueError("PREFERRED_DOMAINS is empty")
    return "(" + " OR ".join(f"site:{d}" for d in sorted(domains)) + ")"

def build_query_dz() -> str:
    """
    Q1 = preferred/media whitelist query.
    """
    sites = build_site_or(PREFERRED_DOMAINS)
    return f"{sites} {slovakia_terms()} {unwanted_url_filters()}"

def build_query_non_dz() -> str:
    """
    Q2 = broad Algerian web scan.
    """
    return f"site:.dz {slovakia_terms()} {unwanted_url_filters()}"

def build_query_preferred_fallback() -> str:
    preferred = build_site_or(PREFERRED_DOMAINS)
    return f"{preferred} {slovakia_terms()} {unwanted_url_filters()}"

def build_query_dz_broad_fallback() -> str:
    """
    Q3 = broad .dz fallback query.
    """
    return f"site:.dz {slovakia_terms()} {unwanted_url_filters()}"

def compute_window_7d() -> tuple[str, str]:
    """
    Calendar-based 7-day window:
    - end: today 23:59:59 local time (or choose UTC)
    - start: 6 days ago 00:00:00
    Returns ISO strings.
    """
    now = datetime.now()  # local time; OK if you run always in same TZ
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    start = (end - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), end.isoformat()


# ----------------------------
# SERPAPI CALL
# ----------------------------


def serpapi_google_news_search(query: str, api_key: str, num: int, hl: str, gl: str, when: str) -> dict:
    all_results = []
    start = 0
    qdr = WHEN_MAP.get(when, "w")
    data = {}

    while len(all_results) < num:
        page_size = min(10, max(1, num - len(all_results)))

        params = {
            "engine": "google",
            "tbm": "nws",
            "q": query,
            "api_key": api_key,
            "hl": hl,
            "gl": gl,
            "tbs": f"qdr:{qdr}",
            "num": page_size,
            "start": start,
            "so": 1,
        }

        data = GoogleSearch(params).get_dict()

        if "error" in data:
            err = str(data["error"])
            if "hasn't returned any results" in err.lower():
                break
            logger.warning(f"SerpAPI error for query={query!r}: {err}")
            break

        items = data.get("news_results", [])
        if not items:
            break

        all_results.extend(items)
        start += page_size
        time.sleep(0.5)

    data["news_results"] = all_results[:num]
    return data


# ----------------------------
# JSON HELPERS
# ----------------------------

def save_json(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def extract_news_items(results: dict) -> list[dict]:
    return results.get("news_results") or []


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def looks_unwanted_news_item(item: dict) -> bool:
    link = item.get("link") or ""
    if not link:
        return True

    d = domain_of(link)
    if d in BLOCKLIST_DOMAINS:
        return True

    low_link = link.lower()

    # e-commerce / offers
    if any(m in low_link for m in ECOM_PATH_MARKERS):
        return True

    # search / tag / archive / technical pages
    if any(m in low_link for m in BAD_PATH_MARKERS):
        return True

    # selected technical / directory patterns that showed up in results
    if low_link.endswith("/opac_css/"):
        return True

    title = (item.get("title") or "").lower()
    if any(w in title for w in TITLE_BAD_WORDS):
        return True

    return False


def clean_news_results(results: dict, limit: int) -> dict:
    """
    Returns a copy of results with news_results filtered and limited.
    """
    items = extract_news_items(results)
    cleaned = [it for it in items if not looks_unwanted_news_item(it)]
    cleaned = cleaned[:limit]

    out = dict(results)
    out["news_results"] = cleaned
    out["cleaning_info"] = {
        "before": len(items),
        "after": len(cleaned),
        "limit": limit,
        "blocklist_domains": sorted(BLOCKLIST_DOMAINS),
    }
    return out


def extract_links_from_news(results: dict) -> list[str]:
    links = []
    for item in extract_news_items(results):
        link = item.get("link")
        if link:
            links.append(link)
    return links


def deduplicate_news(items: list[dict]) -> list[dict]:
    seen = set()
    unique = []

    for item in items:
        link = item.get("link")
        if not link:
            continue

        if link in seen:
            continue

        seen.add(link)
        unique.append(item)

    return unique


def contains_preferred_links(links: list[str]) -> bool:
    for url in links:
        if domain_of(url) in PREFERRED_DOMAINS:
            return True
    return False


def is_full_news_page_raw(results: dict, num: int) -> bool:
    """
    Your rule: fallback only if Q1 and Q2 return a FULL page (== at least num)
    We evaluate this on RAW results (pre-cleaning).
    """
    return len(extract_news_items(results)) >= num


def should_run_preferred_fallback(
    q1_raw: dict,
    q2_raw: dict,
    combined: list[dict],
    *,
    num: int,
    min_unique_ratio: float = 0.70,
    min_preferred: int = 1,
) -> bool:
    """
    Spusti fallback, ak:
    - combined má málo výsledkov
    - alebo v ňom nie sú preferred zdroje
    """

    unique_count = len(combined)
    min_unique = int(num * min_unique_ratio)

    preferred_count = sum(
        1 for it in combined
        if domain_of(it.get("link", "")) in PREFERRED_DOMAINS
    )

    too_few_unique = unique_count < min_unique
    too_few_preferred = preferred_count < min_preferred

    return too_few_unique or too_few_preferred


def count_preferred_in_results(results: dict) -> int:
    links = extract_links_from_news(results)
    return sum(1 for u in links if domain_of(u) in PREFERRED_DOMAINS)


def unique_domain_count(results: dict) -> int:
    domains = [domain_of(u) for u in extract_links_from_news(results)]
    return len({d for d in domains if d})


def domain_stats(items: list[dict]) -> dict:
    domains = []

    for item in items:
        link = item.get("link")
        if not link:
            continue
        domains.append(domain_of(link))

    return dict(Counter(domains))


# ----------------------------
# Helpers
# ----------------------------

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hl", default="fr")
    ap.add_argument("--gl", default="dz")
    ap.add_argument("--num", type=int, default=20)
    ap.add_argument("--when", default="7d", help="napr. 1d, 7d, 30d (pôjde do query ako when:Xd)")
    ap.add_argument("--run-id", default=None, help="ak nezadané, vygeneruje sa timestamp")
    return ap.parse_args()


def update_latest_symlink(run_dir: Path) -> None:
    link = paths.latest_dir
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(run_dir, target_is_directory=True)


def ensure_run_dir(run_id: str) -> Path:
    """
    Ensure required directories exist.
    Safe to call multiple times.
    """
    run_dir = paths.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def source_score(url: str) -> int:
    domain = domain_of(url)
    return SOURCE_RANK.get(domain, 0)


def rank_news_results(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda it: (
            source_score(it.get("link", "")),
            it.get("date", "")
        ),
        reverse=True
    )


def fallback_reason(
    q1_raw: dict,
    q2_raw: dict,
    combined: list[dict],
    num: int,
    min_unique_ratio: float = 0.70,
    min_preferred: int = 1
) -> str:
    unique_count = len(combined)
    min_unique = int(num * min_unique_ratio)

    preferred_count = sum(
        1 for it in combined
        if domain_of(it.get("link", "")) in PREFERRED_DOMAINS
    )

    reasons = []
    if unique_count < min_unique:
        reasons.append(f"too_few_unique {unique_count}<{min_unique}")
    if preferred_count < min_preferred:
        reasons.append(f"too_few_preferred {preferred_count}<{min_preferred}")

    return "yes: " + ", ".join(reasons) if reasons else "no: ok"


# ----------------------------
# MAIN FLOW
# ----------------------------

def main() -> None:
    global s, paths, logger, PREFERRED_DOMAINS, SOURCE_RANK, TOPIC_KEYWORDS, api_key

    s, paths, logger = init_cli("search_flow_news")

    PREFERRED_DOMAINS = s.preferred_domains
    SOURCE_RANK = s.source_rank
    TOPIC_KEYWORDS = s.topic_keywords
    api_key = require(s.serpapi_key, "SERPAPI_KEY")

    logger.info(
        "Config loaded: %s preferred domains, %s ranked sources, %s topic groups",
        len(PREFERRED_DOMAINS),
        len(SOURCE_RANK),
        len(TOPIC_KEYWORDS),
    )

    args = parse_args()

    run_id = args.run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    run_dir = ensure_run_dir(run_id)

    hl = args.hl
    gl = args.gl
    num = args.num
    time_filter_query = f"when:{args.when}"

    ts = int(time.time())

    # Q1: preferred media whitelist
    q1 = build_query_dz()
    logger.info("[1/2] Q1 (PREFERRED): %s", q1)
    try:
        q1_raw = serpapi_google_news_search(
            query=q1,
            api_key=api_key,
            num=num,
            hl=hl,
            gl=gl,
            when=args.when
        )
        save_json(q1_raw, run_dir / "news_q1_raw.json")
    except Exception:
        logger.exception("SerpAPI call failed for Q1")
        raise

    time.sleep(1.0)

    # Q2: broad Algerian web/context query
    q2 = build_query_non_dz()
    logger.info("[2/2] Q2 (CONTEXT): %s", q2)
    try:
        q2_raw = serpapi_google_news_search(
            query=q2,
            api_key=api_key,
            num=num,
            hl=hl,
            gl=gl,
            when=args.when
        )
        save_json(q2_raw, run_dir / "news_q2_raw.json")
    except Exception:
        logger.exception("SerpAPI call failed for Q2")
        raise

    q1_clean = clean_news_results(q1_raw, limit=num)
    q2_clean = clean_news_results(q2_raw, limit=num)

    combined = deduplicate_news(
        extract_news_items(q1_clean) + extract_news_items(q2_clean)
    )
    combined = rank_news_results(combined)

    stats = domain_stats(combined)

    save_json(q1_clean, run_dir / "news_q1_clean.json")
    save_json(q2_clean, run_dir / "news_q2_clean.json")
    save_json({"news_results": combined}, run_dir / "news_combined.json")

    logger.info("\n--- STATS (RAW) ---")
    logger.info("Q1 raw news_results: %s (need >= %s for 'full')", len(extract_news_items(q1_raw)), num)
    logger.info("Q2 raw news_results: %s (need >= %s for 'full')", len(extract_news_items(q2_raw)), num)

    logger.info("\n--- STATS (CLEAN) ---")
    logger.info(
        "Q1 clean news_results: %s, preferred=%s, domains=%s",
        len(extract_news_items(q1_clean)),
        count_preferred_in_results(q1_clean),
        unique_domain_count(q1_clean),
    )
    logger.info(
        "Q2 clean news_results: %s, preferred=%s, domains=%s",
        len(extract_news_items(q2_clean)),
        count_preferred_in_results(q2_clean),
        unique_domain_count(q2_clean),
    )

    any_pref = contains_preferred_links(
        extract_links_from_news({"news_results": combined})
    )

    logger.info("Preferred present in Q1+Q2 (clean): %s", any_pref)
    logger.info("\n--- STATS (Deduplicated) ---")
    logger.info("Unique articles after dedup: %s", len(combined))
    logger.info("\n--- DOMAIN STATS ---")

    for domain, count in sorted(stats.items(), key=lambda x: x[1], reverse=True):
        logger.info("%s: %s", domain, count)

    logger.info("\n--- TOP ARTICLES ---")
    for item in combined[:5]:
        logger.info("%s | %s", domain_of(item.get("link")), item.get("title"))

    q3_raw = None
    q3_clean = None
    q3 = None

    logger.info("Fallback check: %s", fallback_reason(q1_raw, q2_raw, combined, num))

    if should_run_preferred_fallback(q1_raw, q2_raw, combined, num=num):
        time.sleep(1.0)
        q3 = build_query_dz_broad_fallback()
        logger.info("\n[+fallback] Q3 (DZ BROAD): %s", q3)
        try:
            q3_raw = serpapi_google_news_search(
                query=q3,
                api_key=api_key,
                num=num,
                hl=hl,
                gl=gl,
                when=args.when
            )
            save_json(q3_raw, run_dir / "news_q3_raw.json")
        except Exception:
            logger.exception("SerpAPI call failed for Q3")
            raise

        q3_clean = clean_news_results(q3_raw, limit=num)
        save_json(q3_clean, run_dir / "news_q3_clean.json")

        logger.info("\n--- STATS (FALLBACK CLEAN) ---")
        logger.info(
            "Q3 clean news_results: %s, preferred=%s, domains=%s",
            len(extract_news_items(q3_clean)),
            count_preferred_in_results(q3_clean),
            unique_domain_count(q3_clean),
        )
    else:
        logger.info("\n[no fallback] Conditions not met.")

    window_start, window_end = compute_window_7d()

    bundle = {
        "run": {
            "timestamp": ts,
            "engine": "google_news",
            "time_filter_query": time_filter_query,
            "window_start": window_start,
            "window_end": window_end,
            "window_type": "rolling_7x24h",
            "num": num,
            "hl": hl,
            "gl": gl,
            "sort": "date",
            "fallback_triggered": q3_raw is not None,
        },
        "queries": {"q1": q1, "q2": q2, "q3": q3},
        "responses_raw": {"q1": q1_raw, "q2": q2_raw, "q3": q3_raw},
        "responses_clean": {"q1": q1_clean, "q2": q2_clean, "q3": q3_clean},
    }

    save_json(bundle, run_dir / "news_bundle.json")
    update_latest_symlink(run_dir)

    logger.info("Saved bundle: %s", run_dir / "news_bundle.json")
    logger.info("Done.")



if __name__ == "__main__":
    main()