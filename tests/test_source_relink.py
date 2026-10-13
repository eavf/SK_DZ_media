"""
Tests for source_relink.py — preradenie článkov z google.com/goto na skutočnú doménu.
DB spojenie je mockované; overuje sa rozhodovanie, nie SQL.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import requests

from extract_articles_reworked import CandidateArticle, extract_candidate
import extract_articles_reworked
from source_relink import domain_of, relink_source, url_from_fetch_error


def _conn(current_domain: str, relevance=None, duplicate_id=None):
    """Mock spojenia: 1. SELECT vráti aktuálnu doménu článku, SELECT duplikátu vráti duplicate_id."""
    conn = MagicMock()
    calls = []

    def execute(stmt, params=None):
        sql = str(stmt)
        calls.append((sql, params))
        res = MagicMock()
        if "JOIN sources" in sql:
            res.mappings.return_value.fetchone.return_value = {"domain": current_domain, "relevance": relevance}
        elif "url_hash = :h" in sql:
            res.fetchone.return_value = (duplicate_id,) if duplicate_id else None
        return res

    conn.execute.side_effect = execute
    conn.calls = calls
    return conn


def _sqls(conn) -> list[str]:
    return [sql for sql, _ in conn.calls]


class TestHelpers:
    def test_domain_of_strips_www_and_port(self):
        assert domain_of("https://www.Aps.dz:443/fr/x") == "aps.dz"

    def test_domain_of_empty(self):
        assert domain_of(None) == ""

    def test_url_from_fetch_error(self):
        err = "HTTPError: 403 Client Error: Forbidden for url: https://www.winwin.com/a/b?x=1"
        assert url_from_fetch_error(err) == "https://www.winwin.com/a/b?x=1"

    def test_url_from_fetch_error_none(self):
        assert url_from_fetch_error("TIMEOUT") is None
        assert url_from_fetch_error(None) is None


class TestRelinkSource:
    def test_relinks_google_article(self):
        conn = _conn("google.com")
        res = relink_source(conn, 7, "https://www.al24news.dz/clanok", "h1")
        assert res.domain == "al24news.dz"
        assert res.duplicate_of is None and res.deleted is False
        assert any("INSERT INTO sources" in q for q in _sqls(conn))
        assert any("SET source_id" in q for q in _sqls(conn))

    def test_skips_non_redirect_source(self):
        conn = _conn("aps.dz")
        assert relink_source(conn, 7, "https://www.al24news.dz/clanok", "h1") is None
        assert not any("SET source_id" in q for q in _sqls(conn))

    def test_skips_when_final_url_is_still_google(self):
        conn = _conn("google.com")
        assert relink_source(conn, 7, "https://www.google.com/goto?url=abc", "h1") is None
        assert conn.execute.call_count == 0

    def test_skips_without_final_url(self):
        conn = _conn("google.com")
        assert relink_source(conn, 7, None) is None
        assert conn.execute.call_count == 0

    def test_preferred_flag(self):
        conn = _conn("google.com")
        relink_source(conn, 7, "https://aps.dz/x", None)
        params = next(p for q, p in conn.calls if "INSERT INTO sources" in q)
        assert params == {"domain": "aps.dz", "is_preferred": 1}

    def test_duplicate_without_manual_label_is_soft_deleted(self):
        conn = _conn("google.com", relevance=None, duplicate_id=42)
        res = relink_source(conn, 7, "https://al24news.dz/x", "h1")
        assert res.duplicate_of == 42 and res.deleted is True
        params = next(p for q, p in conn.calls if "deleted_at = NOW()" in q)
        assert params == {"id": 7, "note": "auto: duplikát #42"}

    def test_duplicate_with_manual_label_is_kept(self):
        conn = _conn("google.com", relevance=2, duplicate_id=42)
        res = relink_source(conn, 7, "https://al24news.dz/x", "h1")
        assert res.duplicate_of == 42 and res.deleted is False
        assert not any("deleted_at = NOW()" in q for q in _sqls(conn))


class TestHttpErrorFinalUrl:
    def test_http_error_after_redirect_keeps_final_url(self, monkeypatch):
        resp = MagicMock(status_code=403, url="https://www.winwin.com/clanok")
        err = requests.HTTPError("403 Client Error: Forbidden for url: https://www.winwin.com/clanok", response=resp)
        monkeypatch.setattr(extract_articles_reworked, "resolve_and_fetch", MagicMock(side_effect=err))

        result = extract_candidate(CandidateArticle(url="https://www.google.com/goto?url=abc", title="t", snippet=""))
        assert result.http_status == 403
        assert result.final_url == "https://www.winwin.com/clanok"
        assert result.final_url_hash

    def test_http_error_without_redirect(self, monkeypatch):
        resp = MagicMock(status_code=404, url="https://aps.dz/x")
        err = requests.HTTPError("404", response=resp)
        monkeypatch.setattr(extract_articles_reworked, "resolve_and_fetch", MagicMock(side_effect=err))

        result = extract_candidate(CandidateArticle(url="https://aps.dz/x", title="t", snippet=""))
        assert result.final_url is None
