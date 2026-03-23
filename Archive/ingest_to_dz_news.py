import os
import json
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Iterable, List, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from config import get_db_engine

# -------------------------
# CONFIG
# -------------------------

# Zoznam domén, ktoré považujeme za prioritné alebo dôveryhodnejšie.
# Pri deduplikácii (ak máme rovnaký článok z viacerých zdrojov) uprednostníme tieto.
PREFERRED_DOMAINS = {
    "lalgerieaujourdhui.dz",
    "radioalgerie.dz",
    "horizons.dz",
}

# Parametre v URL, ktoré ignorujeme pri vytváraní kanonickej URL.
# Ide o sledovacie parametre (marketing), ktoré nemenia obsah stránky.
DROP_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "yclid", "mc_cid", "mc_eid", "ref", "ref_src",
}


# -------------------------
# URL helpers
# -------------------------

def domain_of(url: str) -> str:
    """Vráti doménu (host) z URL bez 'www.' prefixu."""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def canonicalize_url(url: str) -> str:
    """
    Vytvorí normalizovanú (kanonickú) verziu URL pre porovnávanie.
    - Odstráni 'www.'
    - Odstráni sledovacie parametre (utm_*, fbclid, atď.)
    - Zoradí query parametre abecedne
    - Odstráni koncový lomítko
    """
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
    """Vráti SHA-256 hash reťazca v hex formáte."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# -------------------------
# Published-at confidence (kept in code; not stored unless column exists)
# -------------------------

class PublishedAtConfidence:
    """Úrovne istoty ohľadom dátumu publikovania."""
    NONE = "none"        # Dátum nebol nájdený
    RELATIVE = "relative" # Relatívny čas (napr. "pred 2 hodinami")
    ABSOLUTE = "absolute" # Presný dátum (napr. "2023-10-27")


# Regexy pre relatívne časy (EN, FR, AR)
_RELATIVE_PATTERNS = [
    r"\b(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks)\s+ago\b",
    r"\b(yesterday|today)\b",
    r"\b(il y a)\s+\d+\s+(minute|minutes|heure|heures|jour|jours|semaine|semaines)\b",
    r"\b(hier|aujourd'hui)\b",
    r"\bقبل\s+\d+\s+(دقيقة|ساعات|ساعة|يوم|أيام|أسبوع|أسابيع)\b",
    r"\bأمس\b",
]

# Regexy pre absolútne dátumy
_ABSOLUTE_PATTERNS = [
    r"\b20\d{2}-\d{2}-\d{2}\b",
    r"\b\d{1,2}\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+20\d{2}\b",
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b.*\b20\d{2}\b",
]


def classify_published_at(date_str: Optional[str]) -> str:
    """Určí typ dátumu (absolútny vs relatívny) na základe textu."""
    if not date_str:
        return PublishedAtConfidence.NONE

    t = date_str.strip().lower()

    for pat in _ABSOLUTE_PATTERNS:
        if re.search(pat, t, flags=re.IGNORECASE):
            return PublishedAtConfidence.ABSOLUTE

    for pat in _RELATIVE_PATTERNS:
        if re.search(pat, t, flags=re.IGNORECASE):
            return PublishedAtConfidence.RELATIVE

    return PublishedAtConfidence.RELATIVE


# -------------------------
# Candidate model
# -------------------------

@dataclass(frozen=True)
class ArticleCandidate:
    """
    Reprezentuje jeden nájdený článok pred vložením do DB.
    Obsahuje všetky potrebné metadáta na rozhodnutie o deduplikácii.
    """
    query_id: str
    rank: int
    engine: str

    url: str
    url_canonical: str
    url_hash: str  # CHAR(64) sha256 hex

    domain: str
    is_preferred: bool

    title: Optional[str]
    snippet: Optional[str]

    published_at_raw: Optional[str]
    published_at_confidence: str

    source_label: Optional[str]
    language: Optional[str]


def news_item_to_candidate(item: dict, query_id: str, rank: int, engine: str) -> Optional[ArticleCandidate]:
    """Konvertuje raw item z JSONu na ArticleCandidate objekt."""
    url = item.get("link") or ""
    if not url:
        return None

    canon = canonicalize_url(url)
    if not canon:
        return None

    dom = domain_of(canon) or domain_of(url)
    if not dom:
        return None

    published_raw = item.get("date")
    conf = classify_published_at(published_raw)

    return ArticleCandidate(
        query_id=query_id,
        rank=rank,
        engine=engine,
        url=url,
        url_canonical=canon,
        url_hash=sha256_hex(canon),
        domain=dom,
        is_preferred=(dom in PREFERRED_DOMAINS),
        title=item.get("title"),
        snippet=item.get("snippet"),
        published_at_raw=published_raw,
        published_at_confidence=conf,
        source_label=item.get("source"),
        language=item.get("language"),
    )


def iter_news_items(bundle: dict, use_clean: bool = True) -> Iterable[Tuple[str, int, dict]]:
    """Iteruje cez všetky položky v bundle (pre všetky query q1, q2, q3)."""
    key = "responses_clean" if use_clean else "responses_raw"
    resp = bundle.get(key) or {}
    for qid in ("q1", "q2", "q3"):
        r = resp.get(qid) or {}
        items = r.get("news_results") or []
        for idx, it in enumerate(items, start=1):
            yield qid, idx, it


def build_candidates(bundle: dict, use_clean: bool = True) -> List[ArticleCandidate]:
    """Vytvorí zoznam všetkých kandidátov z bundle."""
    engine_name = (bundle.get("run", {}).get("engine") or "google_news")
    out: List[ArticleCandidate] = []
    for qid, rank, item in iter_news_items(bundle, use_clean=use_clean):
        cand = news_item_to_candidate(item, qid, rank, engine_name)
        if cand:
            out.append(cand)
    return out


_CONF_ORDER = {
    PublishedAtConfidence.ABSOLUTE: 2,
    PublishedAtConfidence.RELATIVE: 1,
    PublishedAtConfidence.NONE: 0,
}


def dedupe_candidates(cands: List[ArticleCandidate]) -> List[ArticleCandidate]:
    """
    Odstráni duplicity na základe URL hashu.
    Ak existuje viac kandidátov s rovnakým URL, vyberie toho 'najlepšieho' podľa skóre:
    1. Preferovaná doména
    2. Lepšia istota dátumu (absolútny > relatívny > žiadny)
    3. Vyšší rank (nižšie číslo ranku, ale tu je to negované, takže pozor na logiku)
       Pozn: rank 1 je lepší ako rank 10.
    """
    best: dict[str, ArticleCandidate] = {}
    for c in cands:
        key = c.url_hash
        if key not in best:
            best[key] = c
            continue

        b = best[key]
        # Skóre: (is_preferred, date_confidence, -rank)
        # -rank zabezpečí, že menší rank (napr. 1) je "väčší" ako väčší rank (napr. 10) pri porovnávaní tuples
        score_c = (1 if c.is_preferred else 0, _CONF_ORDER.get(c.published_at_confidence, 0), -c.rank)
        score_b = (1 if b.is_preferred else 0, _CONF_ORDER.get(b.published_at_confidence, 0), -b.rank)

        if score_c > score_b:
            best[key] = c

    return list(best.values())


# -------------------------
# DB
# -------------------------


def upsert_source(conn, domain: str) -> int:
    """
    Vloží zdroj (doménu) ak neexistuje.
    Ak existuje, aktualizuje príznak is_preferred (ak je nový true).
    Vráti ID zdroja.
    """
    is_pref = 1 if domain in PREFERRED_DOMAINS else 0

    conn.execute(text("""
        INSERT INTO sources (domain, is_preferred)
        VALUES (:domain, :is_preferred)
        ON DUPLICATE KEY UPDATE
            is_preferred = GREATEST(is_preferred, VALUES(is_preferred))
    """), {"domain": domain, "is_preferred": is_pref})

    row = conn.execute(text("SELECT id FROM sources WHERE domain=:domain"), {"domain": domain}).fetchone()
    return int(row[0])


def insert_run(conn, bundle_run: dict, bundle_filename: str) -> int:
    """
    Vloží záznam o behu (run) do tabuľky runs.
    Spracováva metadáta o vyhľadávaní (engine, time_filter, window).
    Vráti ID nového behu.
    """
    engine_name = bundle_run.get("engine") or "google_news"
    time_filter = bundle_run.get("time_filter_query") or bundle_run.get("time_filter") or "when:7d"

    # Optional window fields (may be missing)
    ws = bundle_run.get("window_start")
    we = bundle_run.get("window_end")
    wtype = bundle_run.get("window_type")

    # Fallback: if old style 'after_date' exists, store it in window_type to keep trace
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


def upsert_article(conn, c: ArticleCandidate, source_id: int) -> int:
    """
    Vloží alebo aktualizuje článok v tabuľke articles.
    Používa url_hash na detekciu duplicity.
    Pri aktualizácii prepíše polia novými hodnotami (COALESCE zabezpečí, že NULL neprepíše existujúcu hodnotu).
    Vráti ID článku.
    """
    conn.execute(text("""
        INSERT INTO articles (
            source_id, url, url_canonical, url_hash,
            title, published_at_text, snippet, language,
            ingestion_engine, ingestion_query_id, ingestion_rank,
            first_seen_at, last_seen_at
        )
        VALUES (
            :source_id, :url, :url_canonical, :url_hash,
            :title, :published_at_text, :snippet, :language,
            :ingestion_engine, :ingestion_query_id, :ingestion_rank,
            NOW(), NOW()
        )
        ON DUPLICATE KEY UPDATE
            last_seen_at = NOW(),
            title = COALESCE(VALUES(title), title),
            published_at_text = COALESCE(VALUES(published_at_text), published_at_text),
            snippet = COALESCE(VALUES(snippet), snippet),
            language = COALESCE(VALUES(language), language),
            ingestion_engine = COALESCE(VALUES(ingestion_engine), ingestion_engine),
            ingestion_query_id = COALESCE(VALUES(ingestion_query_id), ingestion_query_id),
            ingestion_rank = COALESCE(VALUES(ingestion_rank), ingestion_rank)
    """), {
        "source_id": source_id,
        "url": c.url,
        "url_canonical": c.url_canonical,
        "url_hash": c.url_hash,  # CHAR(64)
        "title": c.title,
        "published_at_text": c.published_at_raw,
        "snippet": c.snippet,
        "language": c.language,
        "ingestion_engine": c.engine,
        "ingestion_query_id": c.query_id,
        "ingestion_rank": c.rank,
    })

    row = conn.execute(text("SELECT id FROM articles WHERE url_hash=:h"), {"h": c.url_hash}).fetchone()
    return int(row[0])


def link_run_article(conn, run_id: int, article_id: int, query_id: str):
    """Vytvorí M:N väzbu medzi behom (run) a článkom (article)."""
    conn.execute(text("""
        INSERT IGNORE INTO run_articles (run_id, article_id, query_id)
        VALUES (:run_id, :article_id, :query_id)
    """), {"run_id": run_id, "article_id": article_id, "query_id": query_id})


# -------------------------
# MAIN
# -------------------------

def main():
    load_dotenv()

    bundle_path = os.getenv("BUNDLE_PATH")
    if not bundle_path:
        raise SystemExit("Set BUNDLE_PATH to your news_bundle_*.json (env var).")

    # Načítanie JSON bundle
    with open(bundle_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    # Spracovanie: Normalizácia -> Kandidáti -> Deduplikácia
    cands = build_candidates(bundle, use_clean=True)
    cands_dedup = dedupe_candidates(cands)

    print(f"Candidates: {len(cands)}")
    print(f"Deduped:    {len(cands_dedup)}")

    # Uloženie do DB v jednej transakcii
    engine = get_db_engine()
    with engine.begin() as conn:
        # 1. Vytvor záznam o behu
        run_id = insert_run(conn, bundle.get("run", {}), os.path.basename(bundle_path))

        # 2. Spracuj každý článok
        for c in cands_dedup:
            sid = upsert_source(conn, c.domain)
            aid = upsert_article(conn, c, sid)
            link_run_article(conn, run_id, aid, c.query_id)

    print(f"OK: run_id={run_id}, inserted/linked={len(cands_dedup)}")


if __name__ == "__main__":
    main()
