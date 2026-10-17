"""
Tests for api.py — read-only JSON API (autentifikácia, validácia, filtre). DB je mockovaná.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from flask import Flask

import api as api_module

KEY = "test-key"


def _row(id_, *, title="Accord Algérie-Slovaquie", text=None, relevance=None, found=None, lang="fr"):
    return {
        "id": id_, "url": f"https://aps.dz/{id_}", "final_url": None, "title": title, "title_fr": None,
        "snippet": "snippet", "snippet_fr": None, "lang": lang,
        "first_seen_at": found or datetime(2026, 10, 15, 12, 0), "published_at_real": None,
        "relevance": relevance, "relevance_note": None, "content_text": text, "content_text_fr": None,
        "domain": "aps.dz",
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEYS", f"other,{KEY}")
    monkeypatch.setattr(api_module.get_settings(), "topic_keywords",
                        {"politics": ["diplomat", "accord"], "economy": ["gaz"], "security": ["armée"]})
    rows: list[dict] = []
    calls: list = []

    def execute(stmt, params=None):
        calls.append((str(stmt), params))
        res = MagicMock()
        res.mappings.return_value.fetchall.return_value = rows
        res.mappings.return_value.fetchone.return_value = rows[0] if rows else None
        res.fetchall.return_value = []
        return res

    conn = MagicMock()
    conn.execute.side_effect = execute
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    monkeypatch.setattr(api_module, "get_db_engine", lambda: engine)

    app = Flask(__name__)
    app.register_blueprint(api_module.api)
    c = app.test_client()
    c.rows, c.calls = rows, calls
    return c


def _get(client, path, key=KEY, **kw):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.get(path, headers=headers, **kw)


class TestAuth:
    def test_missing_key(self, client):
        assert _get(client, "/api/articles", key=None).status_code == 401

    def test_wrong_key(self, client):
        assert _get(client, "/api/articles", key="zly").status_code == 401

    def test_x_api_key_header(self, client):
        assert client.get("/api/articles", headers={"X-API-Key": KEY}).status_code == 200

    def test_disabled_without_keys(self, client, monkeypatch):
        monkeypatch.setenv("API_KEYS", "")
        assert _get(client, "/api/articles").status_code == 503

    def test_openapi_is_public(self, client):
        r = client.get("/api/openapi.json")
        assert r.status_code == 200 and "/api/articles" in r.get_json()["paths"]


class TestListArticles:
    def test_default_last_7_days(self, client):
        d = _get(client, "/api/articles").get_json()
        assert d["to"] == date.today().isoformat()
        assert d["from"] == (date.today() - timedelta(days=7)).isoformat()
        assert d["filters"]["relevance"] == ["relevant", "unrated"]

    def test_sql_excludes_deleted_and_uses_dates(self, client):
        _get(client, "/api/articles?from=2026-10-01&to=2026-10-07")
        sql, params = client.calls[-1]
        assert "a.deleted_at IS NULL" in sql and "a.first_seen_at >= :date_from" in sql
        assert params["date_from"] == date(2026, 10, 1)
        assert params["date_to_excl"] == date(2026, 10, 8)  # 'to' vrátane

    def test_published_date_field(self, client):
        _get(client, "/api/articles?date_field=published")
        assert "COALESCE(a.published_at_real, a.first_seen_at)" in client.calls[-1][0]

    def test_bad_date(self, client):
        assert _get(client, "/api/articles?from=2026-13-01").status_code == 400

    def test_from_after_to(self, client):
        assert _get(client, "/api/articles?from=2026-10-10&to=2026-10-01").status_code == 400

    def test_unknown_relevance(self, client):
        assert _get(client, "/api/articles?relevance=maybe").status_code == 400

    def test_unknown_topic(self, client):
        r = _get(client, "/api/articles?topic=sport")
        assert r.status_code == 400 and "politics" in r.get_json()["error"]

    def test_topic_filter_and_labels(self, client):
        client.rows.extend([
            _row(1, title="Accord de coopération", relevance=1),
            _row(2, title="Prix du gaz", text="Le gaz algérien"),
        ])
        d = _get(client, "/api/articles?topic=economy").get_json()
        assert d["total"] == 1 and d["articles"][0]["id"] == 2
        assert d["articles"][0]["topics"] == ["economy"] and d["articles"][0]["relevance"] == "unrated"
        d = _get(client, "/api/articles").get_json()
        assert {a["id"]: a["relevance"] for a in d["articles"]} == {1: "relevant", 2: "unrated"}

    def test_pagination(self, client):
        client.rows.extend(_row(i) for i in range(5))
        d = _get(client, "/api/articles?limit=2&offset=2").get_json()
        assert d["total"] == 5 and d["count"] == 2 and [a["id"] for a in d["articles"]] == [2, 3]

    def test_include_text_truncated(self, client):
        client.rows.append(_row(1, text="x" * 500))
        a = _get(client, "/api/articles?include_text=1&max_chars=100").get_json()["articles"][0]
        assert len(a["text"]) == 100 and a["text_truncated"] is True

    def test_text_not_included_by_default(self, client):
        client.rows.append(_row(1, text="obsah"))
        a = _get(client, "/api/articles").get_json()["articles"][0]
        assert "text" not in a and a["has_text"] is True

    def test_lang_filter_in_sql(self, client):
        _get(client, "/api/articles?lang=ar,fr")
        sql, params = client.calls[-1]
        assert "IN" in sql and params["langs"] == ["ar", "fr"]


class TestGetArticle:
    def test_detail_includes_full_text(self, client):
        client.rows.append(_row(7, text="y" * 30000))
        d = _get(client, "/api/articles/7").get_json()
        assert d["id"] == 7 and len(d["text"]) == 30000 and d["text_truncated"] is False

    def test_not_found(self, client):
        assert _get(client, "/api/articles/999").status_code == 404
