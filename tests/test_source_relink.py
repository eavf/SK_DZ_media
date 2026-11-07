"""
Tests for source_relink.py — preradenie článkov z google.com/goto na skutočnú doménu.
DB spojenie je mockované; overuje sa rozhodovanie, nie SQL.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import requests

from extract_articles_reworked import CandidateArticle, extract_candidate
import extract_articles_reworked
from source_relink import domain_of, is_listing_url, mark_content_duplicate, mark_url_duplicate, relink_source, url_from_fetch_error


def _conn(current_domain: str = "google.com", dup_row: dict | None = None):
    """Mock spojenia: SELECT domény vráti current_domain, SELECT duplikátu vráti dup_row."""
    conn = MagicMock()
    calls = []

    def execute(stmt, params=None):
        sql = str(stmt)
        calls.append((sql, params))
        res = MagicMock()
        if "JOIN sources" in sql:
            res.mappings.return_value.fetchone.return_value = {"domain": current_domain}
        elif "JOIN articles o" in sql:
            res.mappings.return_value.fetchone.return_value = dup_row
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
        assert relink_source(conn, 7, "https://www.al24news.dz/clanok") == "al24news.dz"
        assert any("INSERT INTO sources" in q for q in _sqls(conn))
        assert any("SET source_id" in q for q in _sqls(conn))

    def test_skips_non_redirect_source(self):
        conn = _conn("aps.dz")
        assert relink_source(conn, 7, "https://www.al24news.dz/clanok") is None
        assert not any("SET source_id" in q for q in _sqls(conn))

    def test_skips_when_final_url_is_still_google(self):
        conn = _conn("google.com")
        assert relink_source(conn, 7, "https://www.google.com/goto?url=abc") is None
        assert conn.execute.call_count == 0

    def test_skips_without_final_url(self):
        conn = _conn("google.com")
        assert relink_source(conn, 7, None) is None
        assert conn.execute.call_count == 0

    def test_preferred_flag(self):
        conn = _conn("google.com")
        relink_source(conn, 7, "https://aps.dz/x")
        params = next(p for q, p in conn.calls if "INSERT INTO sources" in q)
        assert params == {"domain": "aps.dz", "is_preferred": 1}

    def test_does_not_delete(self):
        conn = _conn("google.com")
        relink_source(conn, 7, "https://al24news.dz/x")
        assert not any("deleted_at = NOW()" in q for q in _sqls(conn))


class TestMarkUrlDuplicate:
    @staticmethod
    def _deleted(conn):
        return [p for q, p in conn.calls if "deleted_at = NOW()" in q]

    def test_no_duplicate(self):
        conn = _conn(dup_row=None)
        assert mark_url_duplicate(conn, 7) is None
        assert self._deleted(conn) == []

    def test_newer_unlabeled_is_deleted(self):
        # #3630 (priamy odkaz) prišiel po #3604 (google.com/goto) → zmaže sa #3630
        conn = _conn(dup_row={"a_rel": None, "o_id": 3604, "o_rel": None})
        res = mark_url_duplicate(conn, 3630)
        assert (res.keep_id, res.delete_id, res.deleted) == (3604, 3630, True)
        assert self._deleted(conn) == [{"id": 3630, "note": "auto: duplikát #3604"}]

    def test_current_older_deletes_other(self):
        conn = _conn(dup_row={"a_rel": None, "o_id": 50, "o_rel": None})
        res = mark_url_duplicate(conn, 7)
        assert (res.keep_id, res.delete_id) == (7, 50)
        assert self._deleted(conn) == [{"id": 50, "note": "auto: duplikát #7"}]

    def test_labeled_newer_is_kept_older_deleted(self):
        conn = _conn(dup_row={"a_rel": 2, "o_id": 42, "o_rel": None})
        res = mark_url_duplicate(conn, 99)
        assert (res.keep_id, res.delete_id, res.deleted) == (99, 42, True)

    def test_both_labeled_nothing_deleted(self):
        conn = _conn(dup_row={"a_rel": 1, "o_id": 42, "o_rel": 2})
        res = mark_url_duplicate(conn, 99)
        assert res.deleted is False
        assert self._deleted(conn) == []

    def test_dry_run_decides_but_does_not_write(self):
        conn = _conn(dup_row={"a_rel": None, "o_id": 3604, "o_rel": None})
        res = mark_url_duplicate(conn, 3630, apply=False)
        assert (res.keep_id, res.delete_id, res.deleted) == (3604, 3630, True)
        assert self._deleted(conn) == []

    def test_dry_run_both_labeled_not_counted(self):
        conn = _conn(dup_row={"a_rel": 1, "o_id": 42, "o_rel": 1})
        assert mark_url_duplicate(conn, 99, apply=False).deleted is False

    def test_ignores_homepage_final_url(self):
        conn = _conn(dup_row=None)
        mark_url_duplicate(conn, 7)
        sql = next(q for q in _sqls(conn) if "JOIN articles o" in q)
        assert "NOT REGEXP" in sql


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


class TestResolveRedirect:
    def test_returns_location(self, monkeypatch):
        import source_relink
        resp = MagicMock(is_redirect=True, headers={"Location": "https://www.algerie360.com/x/"})
        get = MagicMock(return_value=resp)
        monkeypatch.setattr(source_relink.requests, "get", get)
        assert source_relink.resolve_redirect("https://www.google.com/goto?url=abc") == "https://www.algerie360.com/x/"
        assert get.call_args.kwargs["allow_redirects"] is False  # cieľový web sa neotvára

    def test_relative_location(self, monkeypatch):
        import source_relink
        resp = MagicMock(is_redirect=True, headers={"Location": "/sorry"})
        monkeypatch.setattr(source_relink.requests, "get", MagicMock(return_value=resp))
        assert source_relink.resolve_redirect("https://www.google.com/goto?url=abc") == "https://www.google.com/sorry"

    def test_no_redirect(self, monkeypatch):
        import source_relink
        resp = MagicMock(is_redirect=False, headers={})
        monkeypatch.setattr(source_relink.requests, "get", MagicMock(return_value=resp))
        assert source_relink.resolve_redirect("https://www.google.com/goto?url=abc") is None

    def test_network_error(self, monkeypatch):
        import source_relink
        monkeypatch.setattr(source_relink.requests, "get", MagicMock(side_effect=requests.ConnectionError("x")))
        assert source_relink.resolve_redirect("https://www.google.com/goto?url=abc") is None


class TestResolveMissing:
    def _conn(self, rows):
        conn = MagicMock()
        conn.updates = []

        def execute(stmt, params=None):
            sql = str(stmt)
            res = MagicMock()
            if "a.final_url IS NULL" in sql:
                res.mappings.return_value.fetchall.return_value = rows
            elif "UPDATE articles SET final_url" in sql:
                conn.updates.append(params)
            return res

        conn.execute.side_effect = execute
        return conn

    def test_saves_final_url(self, monkeypatch):
        import source_relink
        monkeypatch.setattr(source_relink, "RESOLVE_DELAY", 0)
        monkeypatch.setattr(source_relink, "resolve_redirect",
                            lambda u: {"g1": "http://www.bola.dz/a.pdf", "g2": None,
                                       "g3": "https://www.google.com/sorry"}[u])
        conn = self._conn([{"id": 1, "url": "g1"}, {"id": 2, "url": "g2"}, {"id": 3, "url": "g3"}])
        assert source_relink.resolve_missing(conn) == {"resolved": 1, "failed": 2}
        assert len(conn.updates) == 1
        u = conn.updates[0]
        assert u["id"] == 1 and u["u"] == "http://www.bola.dz/a.pdf"
        assert u["c"] == "https://bola.dz/a.pdf"  # kanonický tvar (https, bez www)


class TestIsListingUrl:
    import pytest as _pytest

    @_pytest.mark.parametrize("url", [
        "https://site.dz/",
        "https://site.dz",
        "https://embbratislava.mfa.gov.dz/fr",
        "https://site.dz/ar/",
        "https://elmawkie.dz/fr/category/uncategorized",
        "https://site.dz/tag/slovaquie",
        "https://site.dz/rubrique/politique",
        "https://site.dz/author/admin",
        "https://site.dz/page/2",
        "https://site.dz/archives",
        "https://SITE.dz/Category/x",
    ])
    def test_listing(self, url):
        assert is_listing_url(url)

    @_pytest.mark.parametrize("url", [
        "https://www.algerie360.com/visa-schengen-2026-pays-plus-faciles",
        "https://cdn.lespoir.dz/wp-content/uploads/2026/08/26-08-2026.pdf",
        "https://www.arpce.dz/fr/service/agr/cc/h2c0s1",
        "https://site.dz/fr/actualite/tags-et-categories-du-jour",  # slovo v slugu, nie segment
        "https://site.dz/frontpage",
        None,
    ])
    def test_article(self, url):
        assert not is_listing_url(url)


class TestMarkContentDuplicate:
    @staticmethod
    def _deleted(conn):
        return [p for q, p in conn.calls if "deleted_at = NOW()" in q]

    def test_no_duplicate(self):
        conn = _conn(dup_row=None)
        assert mark_content_duplicate(conn, 7) is None
        assert self._deleted(conn) == []

    def test_newer_deleted_with_text_note(self):
        # pressbee: AMP verzia (#2996) a pôvodná (#2970) s rovnakým textom
        conn = _conn(dup_row={"a_rel": None, "o_id": 2970, "o_rel": None})
        res = mark_content_duplicate(conn, 2996)
        assert (res.keep_id, res.delete_id, res.deleted) == (2970, 2996, True)
        assert self._deleted(conn) == [{"id": 2996, "note": "auto: duplikát #2970 (text)"}]

    def test_both_labeled_kept(self):
        conn = _conn(dup_row={"a_rel": 1, "o_id": 184, "o_rel": 1})
        assert mark_content_duplicate(conn, 188).deleted is False
        assert self._deleted(conn) == []

    def test_same_domain_and_min_length_in_query(self):
        import source_relink
        conn = _conn(dup_row=None)
        mark_content_duplicate(conn, 7)
        sql, params = next((q, p) for q, p in conn.calls if "JOIN articles o" in q)
        assert "o.source_id = a.source_id" in sql and "o.content_hash = a.content_hash" in sql
        assert params["min_chars"] == source_relink.MIN_DUP_TEXT_CHARS == 500
