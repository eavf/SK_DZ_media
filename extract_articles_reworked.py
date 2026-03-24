from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import trafilatura

from config.config import get_settings, configure_root_logging

s = get_settings()
log = configure_root_logging(s, name="extract_articles")

DEBUG_HTML_DIR = s.paths.bundle_dir / "debug_html"
DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)

DROP_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "yclid", "mc_cid", "mc_eid", "ref", "ref_src",
}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "fr,en;q=0.8,ar;q=0.7",
}

AGGREGATOR_DOMAINS = {"msn.com"}


@dataclass
class CandidateArticle:
    url: str
    title: str | None = None
    source: str | None = None
    snippet: str | None = None
    published_at_search: str | None = None
    query_key: str | None = None
    query_text: str | None = None
    origin_file: str | None = None
    origin_path: str | None = None
    position: int | None = None
    raw: dict[str, Any] | None = None


@dataclass
class ExtractionResult:
    url: str
    url_canonical: str
    url_hash: str
    final_url: str | None
    final_url_canonical: str | None
    final_url_hash: str | None
    domain: str | None
    domain_root: str | None
    title_search: str | None
    source_search: str | None
    snippet_search: str | None
    published_at_search: str | None
    query_key: str | None
    query_text: str | None
    origin_file: str | None
    origin_path: str | None
    position: int | None
    extraction_ok: bool
    fetch_error: str | None
    http_status: int | None
    extracted_at: str
    content_text: str | None = None
    content_hash: str | None = None
    extracted_chars: int = 0
    published_at_real: str | None = None
    lang_detected: str | None = None
    preferred_source: bool = False
    source_rank: int = 0
    matched_topics: list[str] | None = None
    matched_keywords: dict[str, list[str]] | None = None
    raw_search_result: dict[str, Any] | None = None


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_hex(k: str) -> str:
    return hashlib.sha256(k.encode("utf-8")).hexdigest()


def domain_root(netloc: str) -> str:
    n = (netloc or "").lower()
    if n.startswith("www."):
        n = n[4:]
    parts = n.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return n


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url.strip())
    scheme = (p.scheme or "https").lower()

    netloc = (p.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    q = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if k in DROP_QUERY_KEYS:
            continue
        q.append((k, v))
    q.sort()
    query = urlencode(q, doseq=True)

    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    return urlunparse((scheme, netloc, path, "", query, ""))


def parse_dt(v: Any) -> str | None:
    if not v:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            return str(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def is_js_shell(html: str) -> bool:
    h = (html or "").lower()
    if '<div id="root"' in h and 'bundles/v1/views/latest' in h:
        return True
    if 'data-ssr-entry' in h and 'ssr-service-entry' in h:
        return True
    return False


def resolve_and_fetch(url: str, timeout: tuple[int, int] = (10, 25)) -> tuple[str, int, str]:
    sess = requests.Session()
    sess.headers.update(REQUEST_HEADERS)
    resp = sess.get(url, allow_redirects=True, timeout=timeout)
    resp.raise_for_status()
    return resp.url, resp.status_code, resp.text


def extract_text_and_metadata(html: str) -> tuple[str | None, str | None, str | None]:
    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    if not text or not text.strip():
        return None, None, None

    meta = trafilatura.metadata.extract_metadata(html)
    published_dt = None
    lang = None
    if meta:
        lang = getattr(meta, "language", None)
        published_dt = parse_dt(getattr(meta, "date", None))
    return text, published_dt, lang


def find_topic_matches(text: str, topic_keywords: dict[str, list[str]]) -> tuple[list[str], dict[str, list[str]]]:
    haystack = (text or "").lower()
    matched_topics: list[str] = []
    matched_keywords: dict[str, list[str]] = {}
    for topic, words in topic_keywords.items():
        hits: list[str] = []
        for word in words:
            w = str(word).strip()
            if not w:
                continue
            pattern = re.escape(w.lower())
            if re.search(pattern, haystack):
                hits.append(w)
        if hits:
            matched_topics.append(topic)
            matched_keywords[topic] = sorted(set(hits))
    return matched_topics, matched_keywords


def extract_candidates_from_doc(data: Any, origin_file: Path) -> list[CandidateArticle]:
    candidates: list[CandidateArticle] = []

    def add_items(items: Iterable[dict[str, Any]], *, query_key: str | None = None, query_text: str | None = None,
                  origin_path: str) -> None:
        for item in items or []:
            url = item.get("link") or item.get("url")
            if not url:
                continue
            candidates.append(CandidateArticle(
                url=url,
                title=item.get("title"),
                source=item.get("source"),
                snippet=item.get("snippet"),
                published_at_search=item.get("published_at") or item.get("date"),
                query_key=query_key,
                query_text=query_text,
                origin_file=origin_file.name,
                origin_path=origin_path,
                position=item.get("position"),
                raw=item,
            ))

    if isinstance(data, dict):
        if isinstance(data.get("news_results"), list):
            add_items(data["news_results"], origin_path="news_results")

        responses_raw = data.get("responses_raw")
        if isinstance(responses_raw, dict):
            queries = data.get("queries", {}) if isinstance(data.get("queries"), dict) else {}
            for qkey, qdata in responses_raw.items():
                if isinstance(qdata, dict) and isinstance(qdata.get("news_results"), list):
                    add_items(
                        qdata["news_results"],
                        query_key=qkey,
                        query_text=queries.get(qkey),
                        origin_path=f"responses_raw.{qkey}.news_results",
                    )

        responses_clean = data.get("responses_clean")
        if isinstance(responses_clean, dict):
            queries = data.get("queries", {}) if isinstance(data.get("queries"), dict) else {}
            for qkey, qdata in responses_clean.items():
                if isinstance(qdata, dict) and isinstance(qdata.get("news_results"), list):
                    add_items(
                        qdata["news_results"],
                        query_key=qkey,
                        query_text=queries.get(qkey),
                        origin_path=f"responses_clean.{qkey}.news_results",
                    )

    return candidates


def dedupe_candidates(candidates: Iterable[CandidateArticle]) -> list[CandidateArticle]:
    seen: set[str] = set()
    out: list[CandidateArticle] = []
    for item in candidates:
        canon = canonicalize_url(item.url)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append(item)
    return out


def load_candidates(inputs: list[Path]) -> list[CandidateArticle]:
    all_candidates: list[CandidateArticle] = []
    for path in inputs:
        data = load_json(path)
        candidates = extract_candidates_from_doc(data, path)
        log.info("Loaded %s candidates from %s", len(candidates), path)
        all_candidates.extend(candidates)
    return dedupe_candidates(all_candidates)


def extract_candidate(item: CandidateArticle) -> ExtractionResult:
    source_domain = urlparse(item.url).netloc.lower()
    source_root = domain_root(source_domain)
    canon = canonicalize_url(item.url)

    result = ExtractionResult(
        url=item.url,
        url_canonical=canon,
        url_hash=sha256_hex(canon),
        final_url=None,
        final_url_canonical=None,
        final_url_hash=None,
        domain=source_domain,
        domain_root=source_root,
        title_search=item.title,
        source_search=item.source,
        snippet_search=item.snippet,
        published_at_search=item.published_at_search,
        query_key=item.query_key,
        query_text=item.query_text,
        origin_file=item.origin_file,
        origin_path=item.origin_path,
        position=item.position,
        extraction_ok=False,
        fetch_error=None,
        http_status=None,
        extracted_at=datetime.now(timezone.utc).isoformat(),
        matched_topics=[],
        matched_keywords={},
        raw_search_result=item.raw,
    )

    try:
        final_url, status, html = resolve_and_fetch(item.url)
        final_domain = urlparse(final_url).netloc.lower()
        final_root = domain_root(final_domain)
        final_canon = canonicalize_url(final_url)

        result.final_url = final_url
        result.final_url_canonical = final_canon
        result.final_url_hash = sha256_hex(final_canon) if final_canon else None
        result.http_status = status
        result.domain = final_domain
        result.domain_root = final_root
        result.preferred_source = final_root in s.preferred_domains or final_domain in s.preferred_domains
        result.source_rank = s.source_rank.get(final_domain, s.source_rank.get(final_root, 0))

        if final_root in AGGREGATOR_DOMAINS:
            result.fetch_error = "AGGREGATOR"
            return result

        text_out, published_real, lang = extract_text_and_metadata(html)
        if not text_out:
            try:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                host = final_domain.replace(":", "_") or "unknown"
                debug_file = DEBUG_HTML_DIR / f"fail_{stamp}_{host}.html"
                debug_file.write_text(html or "", encoding="utf-8")
            except Exception:
                pass
            result.fetch_error = "JS_RENDER_REQUIRED" if is_js_shell(html) else "EXTRACTION_EMPTY"
            return result

        matched_topics, matched_keywords = find_topic_matches(
            "\n".join(filter(None, [item.title or "", item.snippet or "", text_out])),
            s.topic_keywords,
        )

        result.extraction_ok = True
        result.content_text = text_out
        result.content_hash = sha256_hex(text_out)
        result.extracted_chars = len(text_out)
        result.published_at_real = published_real
        result.lang_detected = lang
        result.matched_topics = matched_topics
        result.matched_keywords = matched_keywords
        return result

    except requests.exceptions.ConnectionError:
        result.fetch_error = "NETWORK_UNREACHABLE"
        return result
    except requests.exceptions.Timeout:
        result.fetch_error = "TIMEOUT"
        return result
    except requests.HTTPError as e:
        result.http_status = getattr(e.response, "status_code", None)
        result.fetch_error = f"HTTPError: {e}"
        return result
    except Exception as e:
        result.fetch_error = f"{type(e).__name__}: {e}"
        return result


def build_output_payload(results: list[ExtractionResult], inputs: list[Path]) -> dict[str, Any]:
    ok = sum(1 for r in results if r.extraction_ok)
    fail = len(results) - ok
    return {
        "run": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "input_files": [str(p) for p in inputs],
            "input_count": len(inputs),
            "articles_total": len(results),
            "articles_ok": ok,
            "articles_failed": fail,
            "extract_max": s.extract_max,
            "preferred_domains_count": len(s.preferred_domains),
            "source_rank_count": len(s.source_rank),
            "topic_groups_count": len(s.topic_keywords),
        },
        "results": [asdict(r) for r in results],
    }


def default_output_for(inputs: list[Path]) -> Path:
    if len(inputs) == 1:
        return inputs[0].with_name(inputs[0].stem + "_articles.json")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return s.paths.bundle_dir / f"extracted_articles_{stamp}.json"


def resolve_input_paths(cli_inputs: list[str]) -> list[Path]:
    if cli_inputs:
        paths = [Path(p).expanduser().resolve() for p in cli_inputs]
    else:
        preferred = [
            s.paths.bundle_dir / "news_bundle.json",
            s.paths.bundle_dir / "news_combined.json",
        ]
        existing = [p for p in preferred if p.exists()]
        if existing:
            return existing
        paths = sorted(s.paths.runs_dir.glob("*.json"))

    resolved: list[Path] = []
    for path in paths:
        if path.is_dir():
            resolved.extend(sorted(path.glob("*.json")))
        elif path.exists():
            resolved.append(path)
    return resolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract article bodies from search result JSON files.")
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Input JSON file(s) or directorie(s). Supports news_bundle.json, news_combined.json and q*_clean/raw files.",
    )
    parser.add_argument("-o", "--output", help="Output JSON path.")
    parser.add_argument("--limit", type=int, default=s.extract_max, help="Maximum number of unique URLs to extract.")
    parser.add_argument("--timeout-connect", type=int, default=10)
    parser.add_argument("--timeout-read", type=int, default=25)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = resolve_input_paths(args.inputs)
    if not paths:
        raise SystemExit("No input JSON files found.")

    global REQUEST_HEADERS
    REQUEST_HEADERS = dict(REQUEST_HEADERS)

    candidates = load_candidates(paths)
    if not candidates:
        raise SystemExit("No article URLs found in the supplied JSON files.")

    candidates = candidates[: max(1, args.limit)]
    log.info("Processing %s unique URLs", len(candidates))

    results = [extract_candidate(item) for item in candidates]
    payload = build_output_payload(results, paths)

    output_path = Path(args.output).expanduser().resolve() if args.output else default_output_for(paths)
    save_json(output_path, payload)
    log.info("Extraction finished. ok=%s fail=%s output=%s",
             sum(1 for r in results if r.extraction_ok),
             sum(1 for r in results if not r.extraction_ok),
             output_path)
    print(output_path)


if __name__ == "__main__":
    main()
