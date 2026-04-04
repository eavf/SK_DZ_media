import json
import re
import time
from urllib.parse import urlparse

from serpapi import GoogleSearch
from datetime import datetime, timedelta
import argparse
from pathlib import Path
from collections import Counter

import logging
from config.config import init_context, init_cli, require
from search_mfa_gov import scrape_mfa

# ----------------------------
# CONFIG
# ----------------------------

s, paths = init_context()
logger = logging.getLogger("search_flow_news")

PREFERRED_DOMAINS = s.preferred_domains
SOURCE_RANK = s.source_rank
TOPIC_KEYWORDS = s.topic_keywords
SLOVAKIA_TERMS = s.search_terms.get("slovakia", [])
ALGERIA_TERMS = s.search_terms.get("algeria", [])
BLOCKLIST_DOMAINS = s.blocklist_domains
ECOM_PATH_MARKERS = s.ecom_path_markers
BAD_PATH_MARKERS = s.bad_path_markers
TITLE_BAD_WORDS = s.title_bad_words
api_key = s.serpapi_key

logger.info(
    f"Config loaded: {len(PREFERRED_DOMAINS)} preferred domains, "
    f"{len(SOURCE_RANK)} ranked sources, "
    f"{len(TOPIC_KEYWORDS)} topic groups, "
    f"{len(SLOVAKIA_TERMS)} slovakia terms, {len(ALGERIA_TERMS)} algeria terms, "
    f"{len(BLOCKLIST_DOMAINS)} blocklist domains"
)

def parse_when(when: str) -> str:
    """Prevedie 'Nd'/'Nh'/'Nw'/'Nm' na SerpAPI qdr hodnotu (napr. '70d' → 'd70').
    Fallback na 'w' (týždeň) pri neplatnom vstupe."""
    import re
    m = re.fullmatch(r"(\d+)([dhwm])", when.strip().lower())
    if m:
        n, unit = m.group(1), m.group(2)
        if unit == "w":
            return f"w" if n == "1" else f"d{int(n) * 7}"
        return f"{unit}{n}" if int(n) != 1 else unit
    return "w"


# ----------------------------
# QUERY BUILDING
# ----------------------------

def slovakia_terms() -> str:
    return "(" + " OR ".join(SLOVAKIA_TERMS) + ")"

def mentions_slovakia(item: dict) -> bool:
    text = f"{item.get('title','')} {item.get('snippet','')}".lower()
    return any(term.lower() in text for term in SLOVAKIA_TERMS)

def algeria_terms() -> str:
    return "(" + " OR ".join(f'"{t}"' if " " in t else t for t in ALGERIA_TERMS) + ")"

def unwanted_url_filters() -> str:
    return "-inurl:recherche -inurl:search -inurl:tag -inurl:tags -inurl:page"

def build_site_or(domains: set[str]) -> str:
    if not domains:
        raise ValueError("PREFERRED_DOMAINS is empty")
    return "(" + " OR ".join(f"site:{d}" for d in sorted(domains)) + ")"

def build_query(domains: set[str] | None = None) -> str:
    """Postaví SerpAPI query. S domains = preferred whitelist, bez = broad site:.dz."""
    sites = build_site_or(domains) if domains else "site:.dz"
    return f"{sites} {slovakia_terms()} {unwanted_url_filters()}"

def build_query_global() -> str:
    """Q3 fallback: globálne hľadanie článkov spomínajúcich oba štáty (bez site: obmedzenia)."""
    return f"{slovakia_terms()} {algeria_terms()}"

def compute_window(when: str) -> tuple[str, str]:
    """Vypočíta časové okno podľa when (napr. '7d', '70d', '2h').
    Returns ISO strings (start, end)."""
    import re
    now = datetime.now()
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    m = re.fullmatch(r"(\d+)([dhm])", when.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "h":
            start = now - timedelta(hours=n)
        elif unit == "m":
            start = (end - timedelta(days=n * 30)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:  # d
            start = (end - timedelta(days=n - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = (end - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), end.isoformat()


# ----------------------------
# SERPAPI CALL
# ----------------------------


def serpapi_google_news_search(query: str, api_key: str, num: int, hl: str, gl: str, when: str) -> dict:
    all_results = []
    start = 0
    qdr = parse_when(when)
    data = {}

    while len(all_results) < num:
        page_size = min(10, max(1, num - len(all_results)))

        params = {
            "engine": s.serp_engine,
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


_FR_REL = re.compile(
    r"il\s+y\s+a\s+(\d+)\s+(minute|minutes|heure|heures|jour|jours|semaine|semaines|mois)",
    re.IGNORECASE,
)
_EN_REL = re.compile(
    r"(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks)\s+ago",
    re.IGNORECASE,
)


def _parse_serp_date(date_str: str | None, fetched_at: datetime) -> tuple[str | None, bool]:
    """Convert SerpAPI date string to ISO date (YYYY-MM-DD).
    Returns (iso_date, is_absolute). is_absolute=True only for dates that were not
    computed from a relative string (e.g. "yesterday", "3 days ago").
    """
    if not date_str:
        return None, False
    s = date_str.strip()

    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10], True

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
        return (fetched_at - delta).date().isoformat(), False

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
        return (fetched_at - delta).date().isoformat(), False

    if re.match(r"(yesterday|hier)$", s, re.IGNORECASE):
        return (fetched_at - timedelta(days=1)).date().isoformat(), False

    return None, False


def _enrich_dates(items: list[dict], fetched_at: datetime) -> None:
    """Add published_at ISO field to items with an absolute (non-computed) date only.
    Relative dates ("yesterday", "3 days ago") are skipped — they are unreliable for
    articles that Google re-surfaces long after original publication.
    """
    for item in items:
        if "published_at" not in item:
            iso, is_absolute = _parse_serp_date(item.get("date"), fetched_at)
            if iso and is_absolute:
                item["published_at"] = iso


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
    """

    :return:
    """
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
    q1 = build_query(PREFERRED_DOMAINS)
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
    q2 = build_query()
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
        q3 = build_query_global()
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

    # MFA: scrape press releases priamo z mfa.gov.dz
    logger.info("\n[MFA] Scraping mfa.gov.dz press releases...")
    mfa_items: list[dict] = []
    try:
        mfa_items = scrape_mfa(args.when, SLOVAKIA_TERMS, logger)
        logger.info("[MFA] Found %s matching press releases", len(mfa_items))
    except Exception:
        logger.exception("[MFA] Scraping failed, pokračujeme bez MFA výsledkov")

    mfa_response = {"news_results": mfa_items} if mfa_items else None
    if mfa_items:
        combined = rank_news_results(combined + mfa_items)
        save_json(mfa_response, run_dir / "news_mfa.json")

    window_start, window_end = compute_window(args.when)

    fetched_at = datetime.fromtimestamp(ts)
    for results in [q1_clean, q2_clean, q3_clean]:
        if results:
            _enrich_dates(results.get("news_results") or [], fetched_at)

    bundle = {
        "run": {
            "timestamp": ts,
            "engine": s.serp_engine,
            "time_filter_query": time_filter_query,
            "window_start": window_start,
            "window_end": window_end,
            "window_type": "rolling_7x24h",
            "num": num,
            "hl": hl,
            "gl": gl,
            "sort": "date",
            "fallback_triggered": q3_raw is not None,
            "mfa_results": len(mfa_items),
        },
        "queries": {"q1": q1, "q2": q2, "q3": q3},
        "responses_raw": {"q1": q1_raw, "q2": q2_raw, "q3": q3_raw, "mfa": mfa_response},
        "responses_clean": {"q1": q1_clean, "q2": q2_clean, "q3": q3_clean, "mfa": mfa_response},
    }

    save_json(bundle, run_dir / "news_bundle.json")
    update_latest_symlink(run_dir)

    logger.info("Saved bundle: %s", run_dir / "news_bundle.json")
    logger.info("Done.")



if __name__ == "__main__":
    main()