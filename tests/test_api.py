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
        "fetch_error": None if text else "HTTPError: 403",
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


class TestFetchPage:
    """GET /api/articles/<id>/page — stiahne stránku teraz, nič neukladá."""

    def _patch(self, monkeypatch, result=None, exc=None):
        import extract_articles_reworked as ear
        def fake(url, timeout=None):
            if exc:
                raise exc
            return result
        monkeypatch.setattr(ear, "resolve_and_fetch", fake)

    def test_article_text(self, client, monkeypatch):
        client.rows.append(_row(5))
        html = "<html><body><article><p>" + "La Slovaquie et l'Algérie renforcent leur coopération. " * 20 + "</p></article></body></html>"
        self._patch(monkeypatch, ("https://aps.dz/5", 200, html, "text/html", html.encode()))
        r = _get(client, "/api/articles/5/page?max_chars=100")
        assert r.status_code == 200
        d = r.get_json()
        assert d["method"] == "article" and d["text_truncated"] and len(d["text"]) == 100
        assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in client.calls)

    def test_page_text_fallback(self, client, monkeypatch):
        import api as api_mod
        import extract_articles_reworked as ear
        client.rows.append(_row(5))
        self._patch(monkeypatch, ("https://aps.dz/5", 200, "<html><body><nav>Menu</nav></body></html>", "text/html", b""))
        monkeypatch.setattr(ear, "extract_text_and_metadata", lambda html: (None, None, None))
        d = _get(client, "/api/articles/5/page").get_json()
        assert d["method"] == "page_text" and "Menu" in d["text"]

    def test_http_error(self, client, monkeypatch):
        import requests
        client.rows.append(_row(5))
        self._patch(monkeypatch, exc=requests.HTTPError(response=MagicMock(status_code=403)))
        r = _get(client, "/api/articles/5/page")
        assert r.status_code == 502 and "403" in r.get_json()["error"]

    def test_missing_and_auth(self, client):
        assert _get(client, "/api/articles/9/page").status_code == 404
        assert _get(client, "/api/articles/9/page", key="zly").status_code == 401

    def test_list_has_fetch_error(self, client):
        client.rows.extend([_row(1), _row(2, text="La Slovaquie")])
        arts = _get(client, "/api/articles").get_json()["articles"]
        assert arts[0]["fetch_error"] == "HTTPError: 403" and arts[1]["fetch_error"] is None


_STORY = " ".join(f"slovo{i} Slovaquie Algérie coopération" for i in range(80))   # ~320 slov


class TestCopies:
    def test_versions_merged_longest_kept(self, client):
        client.rows.extend([
            _row(1, text=_STORY.replace("coopération", "«coopération»")),
            _row(2, text=_STORY + " dopísaný odsek o návšteve ministra" * 10),
            _row(3, text=" ".join(f"iny{i} text o plyne" for i in range(100))),
        ])
        d = _get(client, "/api/articles").get_json()
        assert d["total"] == 2 and d["copies_merged"] == 1
        a = {x["id"]: x for x in d["articles"]}
        assert [c["id"] for c in a[2]["copies"]] == [1] and "copies" not in a[3]
        assert a[2]["copies"][0]["url"] == "https://aps.dz/1"

    def test_copies_param_disables(self, client):
        client.rows.extend([_row(1, text=_STORY), _row(2, text=_STORY)])
        d = _get(client, "/api/articles?copies=1").get_json()
        assert d["total"] == 2 and d["copies_merged"] == 0

    def test_short_texts_and_pdfs_not_merged(self, client):
        pdf = _row(3, text=_STORY + " ďalšie články čísla novín")
        pdf["url"] = "https://media.dz/2026-11-19.pdf"
        client.rows.extend([_row(1, text="menu webu kontakt"), _row(2, text="menu webu kontakt"),
                            _row(4, text=_STORY), pdf])
        d = _get(client, "/api/articles").get_json()
        assert d["total"] == 4 and d["copies_merged"] == 0
