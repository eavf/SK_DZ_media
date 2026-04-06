from __future__ import annotations

import argparse
import json
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import text

import logging

from config.config import get_db_engine, init_context, init_cli
from translate import translate_ar_fr


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

s, paths = init_context()

logger = logging.getLogger("ingest_to_dz_news")

# Only configs needed for ingestion / dedupe
PREFERRED_DOMAINS = s.preferred_domains
SOURCE_RANK = s.source_rank
DROP_QUERY_KEYS = set(s.drop_query_keys or set())
BLOCKLIST_DOMAINS = s.blocklist_domains or set()
BAD_PATH_MARKERS = s.bad_path_markers or set()
TITLE_BAD_WORDS = s.title_bad_words or set()

logger.info(
    f"Config loaded: {len(PREFERRED_DOMAINS)} preferred domains, "
    f"{len(SOURCE_RANK)} ranked sources, "
    f"{len(DROP_QUERY_KEYS)} URL cleanup keys"
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


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


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_title(title: Optional[str]) -> str:
    """
    Normalize title for fuzzy dedupe inside the same domain.
    Keeps it conservative: lowercase, collapse whitespace,
    strip punctuation at edges, remove repeated separators.
    """
    if not title:
        return ""

    t = str(title).strip().lower()

    # unify common separators
    t = t.replace("—", "-").replace("–", "-").replace(":", " ")

    # remove bracket-like noise
    t = re.sub(r"[\[\]\(\)\{\}\"'«»]+", " ", t)

    # keep letters/numbers/spaces/hyphen, replace the rest with space
    t = re.sub(r"[^\w\s\-]", " ", t, flags=re.UNICODE)

    # collapse whitespace / repeated hyphens
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"-{2,}", "-", t)

    return t


# ---------------------------------------------------------------------------
# Published-at confidence
# ---------------------------------------------------------------------------

class PublishedAtConfidence:
    NONE = "none"
    RELATIVE = "relative"
    ABSOLUTE = "absolute"


_RELATIVE_PATTERNS = [
    r"\b(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks)\s+ago\b",
    r"\b(yesterday|today)\b",
    r"\b(il y a)\s+\d+\s+(minute|minutes|heure|heures|jour|jours|semaine|semaines)\b",
    r"\b(hier|aujourd'hui)\b",
    r"\bقبل\s+\d+\s+(دقيقة|ساعات|ساعة|يوم|أيام|أسبوع|أسابيع)\b",
    r"\bأمس\b",
]

_ABSOLUTE_PATTERNS = [
    r"\b20\d{2}-\d{2}-\d{2}\b",
    r"\b\d{1,2}\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+20\d{2}\b",
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b.*\b20\d{2}\b",
    r"\b\d{1,2}\s+\w+\.\s+20\d{2}\b",
]


_FR_REL = re.compile(
    r"il\s+y\s+a\s+(\d+)\s+(minute|minutes|heure|heures|jour|jours|semaine|semaines|mois)",
    re.IGNORECASE,
)
_EN_REL = re.compile(
    r"(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks)\s+ago",
    re.IGNORECASE,
)


def _resolve_raw_date(date_str: Optional[str], fetched_at: Optional[datetime]) -> Optional[str]:
    """Normalizuje surový SerpAPI dátum na ISO reťazec (YYYY-MM-DD).
    Relatívne reťazce ("Il y a 6 jours") sa prepočítajú podľa fetched_at.
    Výsledok sa ukladá do published_at_text — published_at_real ostáva NULL.
    Ak sa nedá parsovať, vráti pôvodný reťazec."""
    if not date_str:
        return date_str
    s = date_str.strip()

    # ISO YYYY-MM-DD
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]

    # DD.MM.YYYY
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass

    if not fetched_at:
        return date_str

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

    return date_str  # neznámy formát, vrátime pôvodný


def classify_published_at(date_str: Optional[str]) -> str:
    if not date_str:
        return PublishedAtConfidence.NONE

    t = date_str.strip().lower()

    for pat in _ABSOLUTE_PATTERNS:
        if re.search(pat, t, flags=re.IGNORECASE):
            return PublishedAtConfidence.ABSOLUTE

    for pat in _RELATIVE_PATTERNS:
        if re.search(pat, t, flags=re.IGNORECASE):
            return PublishedAtConfidence.RELATIVE

    if "utc" in t and re.search(r"\b20\d{2}-\d{2}-\d{2}\b", t):
        return PublishedAtConfidence.ABSOLUTE

    return PublishedAtConfidence.RELATIVE


# ---------------------------------------------------------------------------
# Candidate model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArticleCandidate:
    query_id: str
    rank: int
    engine: str

    url: str
    url_canonical: str
    url_hash: str

    domain: str
    is_preferred: bool

    title: Optional[str]
    normalized_title: str
    title_hash: str
    snippet: Optional[str]

    published_at_raw: Optional[str]
    published_at_confidence: str
    published_at_iso: Optional[str]

    source_label: Optional[str]
    language: Optional[str]
    auto_irrelevant: bool = False


# ---------------------------------------------------------------------------
# Bundle / result parsing
# ---------------------------------------------------------------------------

def _infer_query_id_from_name(path: Path) -> str:
    stem = path.stem.lower()
    for qid in ("q1", "q2", "q3"):
        if qid in stem:
            return qid
    return "single"


def _extract_news_results(payload: dict, default_query_id: str = "single") -> list[tuple[str, int, dict]]:
    """
    Supports the existing JSON structures produced by the search script:

    1) bundle file:
       {
         "run": ...,
         "queries": ...,
         "responses_raw": {...},
         "responses_clean": {...}
       }
       -> items from responses_clean[qid].news_results
          fallback to responses_raw[qid].news_results

    2) single / combined result file:
       {
         "news_results": [...]
       }
       -> items tagged with default_query_id, typically inferred from filename
          (e.g. q1, q2, q3), otherwise "single"
    """
    out: list[tuple[str, int, dict]] = []

    if not isinstance(payload, dict):
        return out

    # Bundle structure: responses_clean / responses_raw
    if "responses_clean" in payload or "responses_raw" in payload:
        clean = payload.get("responses_clean") or {}
        raw = payload.get("responses_raw") or {}

        qids: set[str] = set()
        if isinstance(clean, dict):
            qids.update(str(k) for k in clean.keys())
        if isinstance(raw, dict):
            qids.update(str(k) for k in raw.keys())

        if not qids:
            qids = {"q1", "q2", "q3"}

        for qid in sorted(qids):
            section = {}

            if isinstance(clean, dict):
                sec = clean.get(qid)
                if isinstance(sec, dict):
                    section = sec

            if not section and isinstance(raw, dict):
                sec = raw.get(qid)
                if isinstance(sec, dict):
                    section = sec

            items = section.get("news_results") or []
            if not isinstance(items, list):
                continue

            for idx, item in enumerate(items, start=1):
                if isinstance(item, dict):
                    out.append((qid, idx, item))

        return out

    # Single / combined structure: top-level news_results
    items = payload.get("news_results") or []
    if isinstance(items, list):
        qid = (default_query_id or "single").strip().lower() or "single"
        for idx, item in enumerate(items, start=1):
            if isinstance(item, dict):
                out.append((qid, idx, item))

    return out


def _engine_from_payload(payload: dict) -> str:
    if isinstance(payload, dict):
        run = payload.get("run") or {}
        if isinstance(run, dict) and run.get("engine"):
            return str(run["engine"])
    return "google_news"


def news_item_to_candidate(item: dict, query_id: str, rank: int, engine: str, preferred_domains: set[str], fetched_at: Optional[datetime] = None) -> Optional[ArticleCandidate]:
    url = item.get("link") or ""
    if not url:
        return None

    canon = canonicalize_url(url)
    if not canon:
        return None

    dom = domain_of(canon) or domain_of(url)
    if not dom:
        return None

    published_raw = _resolve_raw_date(item.get("date"), fetched_at)
    published_iso = item.get("published_at")  # ISO date added by search_flow_news enrichment
    conf = classify_published_at(published_raw)

    title = item.get("title")
    normalized_title = normalize_title(title)
    title_hash = sha256_hex(normalized_title) if normalized_title else ""

    low_url = canon.lower()
    text_for_filter = f"{title or ''} {item.get('snippet') or ''}".lower()
    auto_irrelevant = (
        dom in BLOCKLIST_DOMAINS
        or any(m in low_url for m in BAD_PATH_MARKERS)
        or any(w.lower() in text_for_filter for w in TITLE_BAD_WORDS)
    )

    return ArticleCandidate(
        query_id=query_id,
        rank=rank,
        engine=engine,
        url=url,
        url_canonical=canon,
        url_hash=sha256_hex(canon),
        domain=dom,
        is_preferred=(dom in preferred_domains),
        title=title,
        normalized_title=normalized_title,
        title_hash=title_hash,
        snippet=item.get("snippet"),
        published_at_raw=published_raw,
        published_at_confidence=conf,
        published_at_iso=published_iso,
        source_label=item.get("source"),
        language=item.get("language"),
        auto_irrelevant=auto_irrelevant,
    )


def build_candidates(payload: dict, *, preferred_domains: set[str], default_query_id: str = "single") -> list[ArticleCandidate]:
    engine_name = _engine_from_payload(payload)
    out: list[ArticleCandidate] = []

    ts = (payload.get("run") or {}).get("timestamp")
    fetched_at = datetime.fromtimestamp(ts) if ts else None

    for qid, rank, item in _extract_news_results(payload, default_query_id=default_query_id):
        cand = news_item_to_candidate(item, qid, rank, engine_name, preferred_domains, fetched_at)
        if cand:
            out.append(cand)

    return out


_CONF_ORDER = {
    PublishedAtConfidence.ABSOLUTE: 2,
    PublishedAtConfidence.RELATIVE: 1,
    PublishedAtConfidence.NONE: 0,
}


def dedupe_candidates(cands: list[ArticleCandidate], source_rank: dict[str, int]) -> list[ArticleCandidate]:
    """
    Two-stage dedupe:
      1. strict dedupe by canonical URL hash
      2. soft dedupe by (domain, normalized_title)

    Tie-break:
      1. preferred domain
      2. source_rank (higher is better)
      3. published_at confidence
      4. better rank (1 beats 10)
    """

    def candidate_score(c: ArticleCandidate) -> tuple[int, int, int, int]:
        return (
            1 if c.is_preferred else 0,
            int(source_rank.get(c.domain, 0)),
            _CONF_ORDER.get(c.published_at_confidence, 0),
            -c.rank,
        )

    # Stage 1: strict URL dedupe
    best_by_url: dict[str, ArticleCandidate] = {}

    for c in cands:
        key = c.url_hash
        if key not in best_by_url:
            best_by_url[key] = c
            continue

        if candidate_score(c) > candidate_score(best_by_url[key]):
            best_by_url[key] = c

    # Stage 2: soft dedupe by domain + normalized title
    best_by_title: dict[tuple[str, str], ArticleCandidate] = {}

    for c in best_by_url.values():
        # if no usable title, keep article as-is under unique fallback key
        if not c.normalized_title:
            fallback_key = (c.domain, f"__no_title__::{c.url_hash}")
            best_by_title[fallback_key] = c
            continue

        key = (c.domain, c.normalized_title)

        if key not in best_by_title:
            best_by_title[key] = c
            continue

        if candidate_score(c) > candidate_score(best_by_title[key]):
            best_by_title[key] = c

    return list(best_by_title.values())


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def upsert_source(conn, domain: str, *, preferred_domains: set[str]) -> None:
    is_pref = 1 if domain in preferred_domains else 0

    conn.execute(text("""
        INSERT INTO sources (domain, is_preferred)
        VALUES (:domain, :is_preferred)
        ON DUPLICATE KEY UPDATE
            is_preferred = GREATEST(is_preferred, VALUES(is_preferred))
    """), {"domain": domain, "is_preferred": is_pref})


def insert_run(conn, bundle_run: dict, bundle_filename: str) -> int:
    engine_name = bundle_run.get("engine") or "google_news"
    time_filter = bundle_run.get("time_filter_query") or bundle_run.get("time_filter") or "when:7d"

    ws = bundle_run.get("window_start")
    we = bundle_run.get("window_end")
    wtype = bundle_run.get("window_type")

    if (not ws and not we) and bundle_run.get("after_date"):
        wtype = wtype or f"legacy_after:{bundle_run.get('after_date')}"

    def parse_dt(s: Optional[str]):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    ws_dt = parse_dt(ws)
    we_dt = parse_dt(we)

    conn.execute(text("""
        INSERT INTO runs (
            started_at, engine, time_filter_query,
            window_start, window_end, window_type,
            num, hl, gl, sort, fallback_triggered, bundle_filename
        )
        VALUES (
            NOW(), :engine, :time_filter,
            :window_start, :window_end, :window_type,
            :num, :hl, :gl, :sort, :fallback, :bundle_filename
        )
    """), {
        "engine": engine_name,
        "time_filter": time_filter,
        "window_start": ws_dt,
        "window_end": we_dt,
        "window_type": wtype,
        "num": int(bundle_run.get("num", 0) or 0),
        "hl": bundle_run.get("hl"),
        "gl": bundle_run.get("gl"),
        "sort": bundle_run.get("sort") or "date",
        "fallback": 1 if bundle_run.get("fallback_triggered") else 0,
        "bundle_filename": bundle_filename,
    })

    return int(conn.execute(text("SELECT LAST_INSERT_ID()")).scalar_one())


def insert_minimal_run(conn, *, engine: str, bundle_filename: str) -> int:
    conn.execute(text("""
        INSERT INTO runs (
            started_at, engine, time_filter_query,
            window_start, window_end, window_type,
            num, hl, gl, sort, fallback_triggered, bundle_filename
        )
        VALUES (
            NOW(), :engine, :time_filter,
            NULL, NULL, :window_type,
            0, NULL, NULL, 'date', 0, :bundle_filename
        )
    """), {
        "engine": engine or "google_news",
        "time_filter": None,
        "window_type": "single_file",
        "bundle_filename": bundle_filename,
    })
    return int(conn.execute(text("SELECT LAST_INSERT_ID()")).scalar_one())


def upsert_article(conn, c: ArticleCandidate, source_id: int) -> int:
    """Returns rowcount: 1=inserted, 2=updated, 0=no change."""
    result = conn.execute(text("""
        INSERT INTO articles (
            source_id, url, url_canonical, url_hash,
            title, normalized_title, title_hash, published_at_text, published_at_real, published_conf,
            snippet, language,
            ingestion_engine, ingestion_query_id, ingestion_rank,
            relevance,
            first_seen_at, last_seen_at
        )
        VALUES (
            :source_id, :url, :url_canonical, :url_hash,
            :title, :normalized_title, :title_hash, :published_at_text, :published_at_real, :published_conf,
            :snippet, :language,
            :ingestion_engine, :ingestion_query_id, :ingestion_rank,
            :relevance,
            NOW(), NOW()
        )
        ON DUPLICATE KEY UPDATE
            last_seen_at = NOW(),
            title = COALESCE(VALUES(title), title),
            normalized_title = COALESCE(VALUES(normalized_title), normalized_title),
            title_hash = COALESCE(VALUES(title_hash), title_hash),
            published_at_text = COALESCE(VALUES(published_at_text), published_at_text),
            published_at_real = COALESCE(published_at_real, VALUES(published_at_real)),
            published_conf = COALESCE(published_conf, VALUES(published_conf)),
            snippet = COALESCE(VALUES(snippet), snippet),
            language = COALESCE(VALUES(language), language),
            ingestion_engine = COALESCE(VALUES(ingestion_engine), ingestion_engine),
            ingestion_query_id = COALESCE(VALUES(ingestion_query_id), ingestion_query_id),
            ingestion_rank = CASE
                WHEN ingestion_rank IS NULL THEN VALUES(ingestion_rank)
                WHEN VALUES(ingestion_rank) IS NULL THEN ingestion_rank
                ELSE LEAST(VALUES(ingestion_rank), ingestion_rank)
            END
    """), {
        "source_id": source_id,
        "url": c.url,
        "url_canonical": c.url_canonical,
        "url_hash": c.url_hash,
        "title": c.title,
        "normalized_title": c.normalized_title,
        "title_hash": c.title_hash,
        "published_at_text": c.published_at_raw,
        "published_at_real": c.published_at_iso[:19] if c.published_at_iso else None,
        "published_conf": "search" if c.published_at_iso else None,
        "snippet": c.snippet,
        "language": c.language,
        "ingestion_engine": c.engine,
        "ingestion_query_id": c.query_id,
        "ingestion_rank": c.rank,
        "relevance": 0 if c.auto_irrelevant else None,
    })
    return result.rowcount


def link_run_article(conn, run_id: int, article_id: int, query_id: str):
    conn.execute(text("""
        INSERT IGNORE INTO run_articles (run_id, article_id, query_id)
        VALUES (:run_id, :article_id, :query_id)
    """), {"run_id": run_id, "article_id": article_id, "query_id": query_id})


def preload_sources(conn) -> dict[str, int]:
    rows = conn.execute(text("""
        SELECT id, domain
        FROM sources
    """)).fetchall()

    return {str(row.domain).strip().lower(): int(row.id) for row in rows}


def preload_article_ids_by_url_hash(conn, url_hashes: set[str]) -> dict[str, int]:
    if not url_hashes:
        return {}

    placeholders = ", ".join([f":h{i}" for i in range(len(url_hashes))])
    params = {f"h{i}": h for i, h in enumerate(url_hashes)}

    rows = conn.execute(text(f"""
        SELECT id, url_hash
        FROM articles
        WHERE url_hash IN ({placeholders})
    """), params).fetchall()

    return {str(row.url_hash): int(row.id) for row in rows}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_file(bundle_path: Path, *, use_clean: bool = True) -> tuple[int, int, int]:
    default_query_id = _infer_query_id_from_name(bundle_path)

    with bundle_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    cands = build_candidates(
        payload,
        preferred_domains=PREFERRED_DOMAINS,
        default_query_id=default_query_id,
    )
    deduped = dedupe_candidates(cands, source_rank=SOURCE_RANK)

    engine = get_db_engine()
    with engine.begin() as conn:
        run_data = payload.get("run") if isinstance(payload, dict) else None
        if isinstance(run_data, dict) and run_data:
            run_id = insert_run(conn, run_data, bundle_path.name)
        else:
            run_id = insert_minimal_run(
                conn,
                engine=_engine_from_payload(payload),
                bundle_filename=bundle_path.name,
            )

        # 1. preload existing sources once
        source_map = preload_sources(conn)

        # 2. ensure all domains exist
        missing_domains = {
            c.domain for c in deduped
            if c.domain not in source_map
        }
        for domain in sorted(missing_domains):
            upsert_source(conn, domain, preferred_domains=PREFERRED_DOMAINS)

        # 3. reload sources once after inserts
        if missing_domains:
            source_map = preload_sources(conn)

        # 4. upsert all articles without per-row SELECT
        inserted = 0
        updated = 0
        for c in deduped:
            source_id = source_map[c.domain]
            rc = upsert_article(conn, c, source_id)
            if rc == 1:
                inserted += 1
            elif rc == 2:
                updated += 1

        # 5. preload article ids once for all deduped url_hashes
        article_id_map = preload_article_ids_by_url_hash(
            conn,
            {c.url_hash for c in deduped}
        )

        # 6. link run <-> article
        linked = 0
        for c in deduped:
            article_id = article_id_map.get(c.url_hash)
            if article_id is None:
                logger.warning(
                    "Article ID not found after upsert for url_hash=%s url=%s",
                    c.url_hash, c.url
                )
                continue

            link_run_article(conn, run_id, article_id, c.query_id)
            linked += 1

    logger.info(
        "Processed %s: candidates=%s deduped=%s inserted=%s updated=%s linked=%s run_id=%s",
        bundle_path.name, len(cands), len(deduped), inserted, updated, linked, run_id
    )

    _translate_pending_titles(article_id_map.values())

    return len(cands), len(deduped), run_id, inserted, updated


def _translate_pending_titles(article_ids) -> None:
    """Preloží title + snippet pre arabské články bez title_fr."""
    api_key = s.deepl_api_key
    if not api_key:
        return

    ids = list(article_ids)
    if not ids:
        return

    engine = get_db_engine()
    placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
    params = {f"id{i}": v for i, v in enumerate(ids)}

    with engine.begin() as conn:
        rows = conn.execute(text(f"""
            SELECT id, title, snippet
            FROM articles
            WHERE id IN ({placeholders})
              AND language = 'ar'
              AND title_fr IS NULL
              AND deleted_at IS NULL
        """), params).mappings().fetchall()

    if not rows:
        return

    logger.info("Prekladám title+snippet pre %s arabských článkov", len(rows))

    for row in rows:
        texts, keys = [], []
        if row["title"]:
            texts.append(row["title"]); keys.append("title_fr")
        if row["snippet"]:
            texts.append(row["snippet"]); keys.append("snippet_fr")
        if not texts:
            continue
        try:
            translated = translate_ar_fr(api_key, texts)
            updates = dict(zip(keys, translated))
            updates["id"] = row["id"]
            set_clause = ", ".join(f"{k} = :{k}" for k in keys)
            with engine.begin() as conn:
                conn.execute(text(f"UPDATE articles SET {set_clause} WHERE id = :id"), updates)
            logger.info("Preložené title/snippet: id=%s", row["id"])
        except Exception as e:
            logger.warning("DeepL zlyhal pre id=%s: %s", row["id"], e)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ingest search JSON results into dz_news DB."
    )
    p.add_argument(
        "inputs",
        nargs="*",
        help="JSON files to ingest. If omitted, the default latest bundle path from config.py is used.",
    )
    return p


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def get_syndicated_articles(conn) -> list[dict]:
    """
    :Title: Syndicated titles
    :param conn: connection to dz_news db
    :return: list of syndicated articles
    """
    rows = conn.execute(text("""
        SELECT
            a.title_hash,
            COUNT(*) AS articles,
            COUNT(DISTINCT a.source_id) AS sources
        FROM articles a
        WHERE a.title_hash IS NOT NULL
          AND a.title_hash <> ''
        GROUP BY a.title_hash
        HAVING COUNT(DISTINCT a.source_id) > 1
        ORDER BY sources DESC, articles DESC
    """)).fetchall()

    return [
        {
            "title_hash": str(r.title_hash),
            "articles": int(r.articles),
            "sources": int(r.sources),
        }
        for r in rows
    ]


def get_url_hash_duplicates(conn) -> list[dict]:
    """
    Title: duplicity podľa url_hash
    :param conn: connection to dz_news db
    :return: list of duplicated urls
    """
    rows = conn.execute(text("""
        SELECT
            url_hash,
            COUNT(*) AS duplicates
        FROM articles
        GROUP BY url_hash
        HAVING COUNT(*) > 1
        ORDER BY duplicates DESC
    """)).fetchall()

    return [
        {
            "url_hash": str(r.url_hash),
            "duplicates": int(r.duplicates),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():

    global s, paths, logger, PREFERRED_DOMAINS, SOURCE_RANK, DROP_QUERY_KEYS

    s, paths, logger = init_cli("ingest_to_dz_news")

    PREFERRED_DOMAINS = s.preferred_domains
    SOURCE_RANK = s.source_rank
    DROP_QUERY_KEYS = set(s.drop_query_keys or set())

    logger.info(
        "Config loaded: %s preferred domains, %s ranked sources, %s URL cleanup keys",
        len(PREFERRED_DOMAINS),
        len(SOURCE_RANK),
        len(DROP_QUERY_KEYS),
    )

    parser = build_arg_parser()
    args = parser.parse_args()

    inputs = [Path(x).expanduser().resolve() for x in args.inputs]

    if not inputs:
        inputs = [paths.latest_bundle_path.resolve()]

    for path in inputs:
        if not path.exists():
            raise SystemExit(f"Input file not found: {path}")

    total_candidates = 0
    total_deduped = 0
    total_inserted = 0
    total_updated = 0

    for path in inputs:
        cand_count, dedup_count, run_id, ins, upd = process_file(path)
        total_candidates += cand_count
        total_deduped += dedup_count
        total_inserted += ins
        total_updated += upd
        print(f"{path.name}: candidates={cand_count}, deduped={dedup_count}, inserted={ins}, updated={upd}, run_id={run_id}")

    if len(inputs) > 1:
        print(f"TOTAL: candidates={total_candidates}, deduped={total_deduped}, inserted={total_inserted}, updated={total_updated}")


if __name__ == "__main__":
    main()
