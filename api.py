"""
api.py — Read-only JSON API pre AI agentov a iné systémy (blueprint /api).

Autentifikácia: API kľúč z API_KEYS v .env (čiarkou oddelené), poslaný ako
    Authorization: Bearer <kľúč>     alebo     X-API-Key: <kľúč>
Bez nastaveného API_KEYS je API vypnuté (503).

Endpointy:
    GET /api/articles        zoznam článkov za obdobie (default posledných 7 dní) s filtrami
    GET /api/articles/<id>   detail článku vrátane textu
    GET /api/meta            hodnoty filtrov (témy, jazyky, zdroje)
    GET /api/openapi.json    OpenAPI 3 popis (bez autentifikácie) — pre ChatGPT Actions, n8n, ...
"""
from __future__ import annotations

import hmac
import os
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, jsonify, request
from sqlalchemy import bindparam, text

from config.config import get_db_engine, get_settings

api = Blueprint("api", __name__, url_prefix="/api")

DEFAULT_DAYS = 7
MAX_LIMIT = 200
DEFAULT_TEXT_CHARS = 20000

_RELEVANCE_SQL = {
    "relevant": "a.relevance >= 1",
    "unrated": "a.relevance IS NULL",
    "irrelevant": "a.relevance = 0",
}
_RELEVANCE_LABEL = {None: "unrated", 0: "irrelevant"}


# ── autentifikácia ──────────────────────────────────────────────────────────────

def _api_keys() -> list[str]:
    return [k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()]


def _request_key() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def api_key_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        keys = _api_keys()
        if not keys:
            return _error(503, "API je vypnuté — nastav API_KEYS v .env")
        given = _request_key()
        if not given or not any(hmac.compare_digest(given, k) for k in keys):
            return _error(401, "Chýba alebo je neplatný API kľúč (Authorization: Bearer <kľúč> alebo X-API-Key)")
        return f(*args, **kwargs)
    return decorated


def _error(status: int, message: str):
    resp = jsonify({"error": message, "status": status})
    resp.status_code = status
    return resp


# ── pomocné ────────────────────────────────────────────────────────────────────

def _parse_date(name: str, default: date) -> date:
    raw = request.args.get(name, "").strip()
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        abort(_error(400, f"Neplatný dátum '{name}': {raw} (očakávam YYYY-MM-DD)"))


def _csv_arg(name: str) -> list[str]:
    return [v.strip().lower() for v in request.args.get(name, "").split(",") if v.strip()]


def _int_arg(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except ValueError:
        abort(_error(400, f"Neplatné číslo '{name}'"))


def _topics(*texts: str | None) -> list[str]:
    """Témy podľa kľúčových slov z config/topics.json (rovnako ako pri extrakcii)."""
    haystack = " ".join(t for t in texts if t).lower()
    return [topic for topic, words in get_settings().topic_keywords.items()
            if any(str(w).strip() and str(w).strip().lower() in haystack for w in words)]


def _iso(dt) -> str | None:
    return dt.isoformat() if isinstance(dt, (datetime, date)) else None


def _relevance_label(value) -> str:
    return _RELEVANCE_LABEL.get(value, "relevant")


def _article_json(r, *, topics: list[str], include_text: bool, max_chars: int) -> dict:
    out = {
        "id": r["id"],
        "title": r["title"],
        "title_fr": r["title_fr"],
        "snippet": r["snippet"],
        "snippet_fr": r["snippet_fr"],
        "url": r["final_url"] or r["url"],
        "source": r["domain"],
        "language": r["lang"],
        "found_at": _iso(r["first_seen_at"]),
        "published_at": _iso(r["published_at_real"]),
        "relevance": _relevance_label(r["relevance"]),
        "relevance_note": r["relevance_note"],
        "topics": topics,
        "has_text": r["content_text"] is not None,
    }
    if include_text:
        for key, col in (("text", "content_text"), ("text_fr", "content_text_fr")):
            val = r[col]
            out[key] = val[:max_chars] if val else None
            out[f"{key}_truncated"] = bool(val and len(val) > max_chars)
    return out


_SELECT = """
    SELECT a.id, a.url, a.final_url, a.title, a.title_fr, a.snippet, a.snippet_fr,
           COALESCE(a.lang_detected, a.language) AS lang, a.first_seen_at, a.published_at_real,
           a.relevance, a.relevance_note, a.content_text, a.content_text_fr, s.domain
    FROM articles a
    JOIN sources s ON s.id = a.source_id
"""


# ── endpointy ──────────────────────────────────────────────────────────────────

@api.get("/articles")
@api_key_required
def list_articles():
    today = date.today()
    date_to = _parse_date("to", today)
    date_from = _parse_date("from", date_to - timedelta(days=DEFAULT_DAYS))
    if date_from > date_to:
        return _error(400, "'from' je po 'to'")

    date_field = request.args.get("date_field", "found").strip().lower()
    if date_field not in ("found", "published"):
        return _error(400, "date_field musí byť 'found' alebo 'published'")
    date_col = "a.first_seen_at" if date_field == "found" else "COALESCE(a.published_at_real, a.first_seen_at)"

    relevance = _csv_arg("relevance") or ["relevant", "unrated"]
    if "all" in relevance:
        relevance = list(_RELEVANCE_SQL)
    unknown = [r for r in relevance if r not in _RELEVANCE_SQL]
    if unknown:
        return _error(400, f"Neznáma relevance: {unknown} (relevant, unrated, irrelevant, all)")

    topics = _csv_arg("topic")
    known_topics = set(get_settings().topic_keywords)
    if [t for t in topics if t not in known_topics]:
        return _error(400, f"Neznáma téma (dostupné: {sorted(known_topics)})")

    langs, sources = _csv_arg("lang"), _csv_arg("source")
    only_with_text = request.args.get("has_text", "").lower() in ("1", "true", "yes")
    include_text = request.args.get("include_text", "").lower() in ("1", "true", "yes")
    max_chars = _int_arg("max_chars", DEFAULT_TEXT_CHARS, 100, 200000)
    limit = _int_arg("limit", 50, 1, MAX_LIMIT)
    offset = _int_arg("offset", 0, 0, 10**6)

    where = [
        "a.deleted_at IS NULL",
        f"{date_col} >= :date_from",
        f"{date_col} < :date_to_excl",
        "(" + " OR ".join(_RELEVANCE_SQL[r] for r in relevance) + ")",
    ]
    params: dict = {"date_from": date_from, "date_to_excl": date_to + timedelta(days=1)}
    stmt_params = []
    if langs:
        where.append("COALESCE(a.lang_detected, a.language) IN :langs")
        params["langs"] = langs
        stmt_params.append(bindparam("langs", expanding=True))
    if sources:
        where.append("s.domain IN :sources")
        params["sources"] = sources
        stmt_params.append(bindparam("sources", expanding=True))
    if only_with_text:
        where.append("a.content_text IS NOT NULL")

    stmt = text(_SELECT + " WHERE " + " AND ".join(where) + f" ORDER BY {date_col} DESC, a.id DESC")
    if stmt_params:
        stmt = stmt.bindparams(*stmt_params)
    with get_db_engine().connect() as conn:
        rows = conn.execute(stmt, params).mappings().fetchall()

    # témy sa počítajú z textu (v DB nie sú) → filter a stránkovanie v Pythone
    items = []
    for r in rows:
        t = _topics(r["title"], r["title_fr"], r["snippet"], r["snippet_fr"], r["content_text"], r["content_text_fr"])
        if topics and not set(topics) & set(t):
            continue
        items.append((r, t))

    page = items[offset:offset + limit]
    return jsonify({
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "date_field": date_field,
        "filters": {"relevance": relevance, "topic": topics, "lang": langs, "source": sources,
                    "has_text": only_with_text},
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "count": len(page),
        "articles": [_article_json(r, topics=t, include_text=include_text, max_chars=max_chars) for r, t in page],
    })


@api.get("/articles/<int:article_id>")
@api_key_required
def get_article(article_id: int):
    with get_db_engine().connect() as conn:
        r = conn.execute(text(_SELECT + " WHERE a.id = :id AND a.deleted_at IS NULL"),
                         {"id": article_id}).mappings().fetchone()
    if not r:
        return _error(404, f"Článok {article_id} neexistuje alebo je vyradený")
    t = _topics(r["title"], r["title_fr"], r["snippet"], r["snippet_fr"], r["content_text"], r["content_text_fr"])
    max_chars = _int_arg("max_chars", 10**6, 100, 10**6)
    return jsonify(_article_json(r, topics=t, include_text=True, max_chars=max_chars))


@api.get("/meta")
@api_key_required
def meta():
    with get_db_engine().connect() as conn:
        langs = conn.execute(text("""
            SELECT COALESCE(lang_detected, language) AS lang, COUNT(*) AS n
            FROM articles WHERE deleted_at IS NULL GROUP BY 1 ORDER BY n DESC
        """)).fetchall()
        sources = conn.execute(text("""
            SELECT s.domain, COUNT(*) AS n FROM articles a JOIN sources s ON s.id = a.source_id
            WHERE a.deleted_at IS NULL AND COALESCE(a.relevance, 1) <> 0
            GROUP BY s.domain ORDER BY n DESC LIMIT 50
        """)).fetchall()
    return jsonify({
        "topics": sorted(get_settings().topic_keywords),
        "relevance": list(_RELEVANCE_SQL) + ["all"],
        "languages": [{"lang": l, "count": n} for l, n in langs if l],
        "top_sources": [{"source": d, "count": n} for d, n in sources],
        "default_days": DEFAULT_DAYS,
    })


@api.get("/openapi.json")
def openapi():
    base = request.url_root.rstrip("/")
    str_param = lambda name, desc, **kw: {"name": name, "in": "query", "required": False,
                                         "description": desc, "schema": {"type": "string", **kw}}
    return jsonify({
        "openapi": "3.1.0",
        "info": {
            "title": "DZ News Monitor API",
            "version": "1.0",
            "description": "Read-only prístup k článkom alžírskych médií o vzťahoch Slovensko–Alžírsko. "
                           "Vyradené články (šport, bez SK kontextu, duplikáty) sa nevracajú.",
        },
        "servers": [{"url": base}],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearer": []}],
        "paths": {
            "/api/articles": {"get": {
                "operationId": "listArticles",
                "summary": "Články za obdobie (default posledných 7 dní)",
                "parameters": [
                    str_param("from", "Od dátumu YYYY-MM-DD (default: to − 7 dní)", format="date"),
                    str_param("to", "Do dátumu YYYY-MM-DD vrátane (default: dnes)", format="date"),
                    str_param("date_field", "found = kedy aplikácia článok našla (default), published = dátum publikácie",
                              enum=["found", "published"]),
                    str_param("relevance", "Čiarkou: relevant, unrated, irrelevant, all (default relevant,unrated)"),
                    str_param("topic", "Čiarkou: politics, economy, security"),
                    str_param("lang", "Čiarkou: ar, fr, en, ..."),
                    str_param("source", "Čiarkou: domény, napr. aps.dz"),
                    str_param("has_text", "1 = len články so stiahnutým textom"),
                    str_param("include_text", "1 = pridať text článku (text, text_fr)"),
                    {"name": "max_chars", "in": "query", "required": False, "schema": {"type": "integer"},
                     "description": "Max. dĺžka textu pri include_text (default 20000)"},
                    {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "maximum": MAX_LIMIT},
                     "description": "Počet článkov (default 50, max 200)"},
                    {"name": "offset", "in": "query", "required": False, "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "Zoznam článkov"}},
            }},
            "/api/articles/{id}": {"get": {
                "operationId": "getArticle",
                "summary": "Detail článku vrátane celého textu (originál aj francúzsky preklad)",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Článok"}, "404": {"description": "Neexistuje"}},
            }},
            "/api/meta": {"get": {
                "operationId": "getMeta",
                "summary": "Dostupné hodnoty filtrov (témy, jazyky, zdroje)",
                "responses": {"200": {"description": "Meta"}},
            }},
        },
    })
