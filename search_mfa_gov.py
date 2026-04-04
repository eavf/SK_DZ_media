"""
search_mfa_gov.py — Scrape MFA Algeria press releases for Slovakia mentions.

Uses the Next.js JSON data endpoint — no SerpAPI required.
Output bundle is compatible with extract_articles_reworked.py.

Usage:
    python search_mfa_gov.py [--when 30d] [--output path/to/output.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config.config import init_cli

MFA_BASE = "https://www.mfa.gov.dz"
MFA_FR_PATH = "/fr/press-and-information/news-and-press-releases"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
    ),
    "Accept-Language": "fr,en;q=0.8",
}


def fetch_build_id(session: requests.Session) -> str:
    resp = session.get(f"{MFA_BASE}{MFA_FR_PATH}", timeout=(10, 20))
    resp.raise_for_status()
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', resp.text)
    if not m:
        raise RuntimeError("buildId not found in MFA page — site may have been redeployed")
    return m.group(1)


def fetch_page(session: requests.Session, build_id: str, page: int) -> dict:
    url = f"{MFA_BASE}/_next/data/{build_id}{MFA_FR_PATH}.json?page={page}"
    resp = session.get(url, timeout=(10, 20))
    resp.raise_for_status()
    return resp.json()["pageProps"]["data"]


def cutoff_dt(when: str) -> datetime:
    m = re.fullmatch(r"(\d+)([dhwm])", when.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n),
                 "w": timedelta(weeks=n), "m": timedelta(days=n * 30)}[unit]
    else:
        delta = timedelta(days=30)
    return datetime.now(timezone.utc) - delta


def parse_creation_date(date_str: str) -> datetime | None:
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None


def item_to_news_result(item: dict) -> dict:
    link = item.get("link", "")
    if link and not link.startswith("http"):
        link = f"{MFA_BASE}/fr/{link.lstrip('/')}"
    date_str = (item.get("creation_date") or "")[:10]
    return {
        "link": link,
        "title": item.get("title") or item.get("meta") or "",
        "snippet": item.get("description") or "",
        "source": "mfa.gov.dz",
        "date": date_str,
        "published_at": date_str,
    }


def scrape_mfa(when: str, sk_terms: list[str], log: logging.Logger) -> list[dict]:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    session.verify = False  # mfa.gov.dz má neoveriteľný SSL certifikát
    cutoff = cutoff_dt(when)

    log.info("Fetching MFA buildId...")
    build_id = fetch_build_id(session)
    log.info("buildId: %s", build_id)

    results: list[dict] = []
    page = 1

    while True:
        log.info("Fetching page %s...", page)
        try:
            data = fetch_page(session, build_id, page)
        except Exception as e:
            log.warning("Failed to fetch page %s: %s", page, e)
            break

        total_pages = data.get("pages", 1)
        items = data.get("result", [])
        if not items:
            break

        stop = False
        for item in items:
            dt = parse_creation_date(item.get("creation_date", ""))
            if dt and dt < cutoff:
                stop = True
                break

            text = " ".join(filter(None, [
                item.get("title", ""),
                item.get("description", ""),
                item.get("meta", ""),
            ])).lower()
            if any(t.lower() in text for t in sk_terms):
                results.append(item_to_news_result(item))
                log.info("  ✓ [%s] %s", (item.get("creation_date") or "")[:10],
                         (item.get("title") or "")[:80])

        if stop or page >= total_pages:
            break

        page += 1
        time.sleep(0.5)

    return results


def main() -> None:
    s, paths, log = init_cli("search_mfa_gov")

    ap = argparse.ArgumentParser(description="Scrape MFA Algeria press releases for Slovakia mentions.")
    ap.add_argument("--when", default="30d",
                    help="Time window: 7d, 30d, 90d... (default: 30d)")
    ap.add_argument("--output", default=None, help="Output JSON file path (overrides default run dir)")
    args = ap.parse_args()

    sk_terms = s.search_terms.get("slovakia", [])
    if not sk_terms:
        raise SystemExit("No Slovakia search terms configured")

    results = scrape_mfa(args.when, sk_terms, log)
    log.info("Found %s matching press releases", len(results))

    now = datetime.now(timezone.utc)
    bundle = {
        "run": {
            "timestamp": int(now.timestamp()),
            "generated_at": now.isoformat(),
            "source": "mfa.gov.dz",
            "when": args.when,
            "sk_terms_count": len(sk_terms),
            "results_count": len(results),
        },
        "responses_clean": {"mfa": {"news_results": results}},
        "responses_raw":   {"mfa": {"news_results": results}},
    }

    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Saved: %s", output)
    else:
        # Uložíme do run adresára rovnako ako search_flow_news.py
        run_id = datetime.now().strftime("mfa_%Y%m%d_%H%M%S")
        run_dir = paths.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        bundle_path = run_dir / "news_bundle.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Saved bundle: %s", bundle_path)

        # Aktualizujeme latest symlink
        latest = paths.latest_dir
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(run_dir, target_is_directory=True)
        log.info("Updated latest → %s", run_dir)


if __name__ == "__main__":
    main()