"""
Tests for scheduler.py — email filtering helpers.
"""
from __future__ import annotations

from scheduler import _EMAIL_FILTER_SQL, _mentions_slovakia


class TestMentionsSlovakia:
    def test_french(self):
        assert _mentions_slovakia("Accord entre l'Algérie et la Slovaquie", None)

    def test_arabic_in_snippet(self):
        assert _mentions_slovakia("عنوان", "اتفاق مع سلوفاكيا")

    def test_case_insensitive(self):
        assert _mentions_slovakia("BRATISLAVA accueille")

    def test_sport_without_slovakia(self):
        assert not _mentions_slovakia("Match Rangers International - MC Alger", "les buts, les compositions")

    def test_empty(self):
        assert not _mentions_slovakia(None, "")


class TestEmailFilterSql:
    def test_excludes_deleted_irrelevant_and_sport(self):
        assert "deleted_at IS NULL" in _EMAIL_FILTER_SQL
        assert "COALESCE(a.relevance, 1) <> 0" in _EMAIL_FILTER_SQL
        assert "'sport'" in _EMAIL_FILTER_SQL


class TestParseExtractSummary:
    """app._parse_extract_summary — súhrn hromadnej extrakcie na stránke Process."""

    def test_current_format_with_duplicate(self):
        from app import _parse_extract_summary
        out = "… [INFO] extract_bulk: Hotovo: ok=15, soft_deleted=3, commercial=0, failed=1, duplicate=2, ai_sport=4 in /app/x.py:1"
        assert _parse_extract_summary(out) == {"ok": 15, "soft_deleted": 3, "commercial": 0, "failed": 1,
                                               "duplicate": 2, "ai_sport": 4, "pdf_unreadable": 0, "bad_url": 0,
                                               "ai_relevant": 0, "ai_irrelevant": 0}

    def test_old_format_without_new_keys(self):
        from app import _parse_extract_summary
        assert _parse_extract_summary("Hotovo: ok=1, soft_deleted=0, commercial=0, failed=0") == {
            "ok": 1, "soft_deleted": 0, "commercial": 0, "failed": 0, "duplicate": 0, "ai_sport": 0, "pdf_unreadable": 0, "bad_url": 0,
            "ai_relevant": 0, "ai_irrelevant": 0}

    def test_ai_relevance_counts(self):
        from app import _parse_extract_summary
        s = _parse_extract_summary("Hotovo: ok=3, failed=0, ai_relevant=2, ai_irrelevant=1")
        assert s["ai_relevant"] == 2 and s["ai_irrelevant"] == 1

    def test_no_summary(self):
        from app import _parse_extract_summary
        assert _parse_extract_summary("Žiadne články na extrakciu.") is None


class TestSerpapiErrors:
    """scheduler.serpapi_errors — chyby SerpAPI z bundla sa dostanú do e-mailu."""

    def test_errors_found(self, tmp_path):
        import json
        from scheduler import serpapi_errors
        p = tmp_path / "news_bundle.json"
        p.write_text(json.dumps({"responses_raw": {
            "q1": {"error": "site: not supported", "news_results": []},
            "q2": {"news_results": [{"link": "x"}]},
            "q3": None,
        }}), encoding="utf-8")
        assert serpapi_errors(p) == ["Q1: site: not supported"]

    def test_no_errors(self, tmp_path):
        import json
        from scheduler import serpapi_errors
        p = tmp_path / "news_bundle.json"
        p.write_text(json.dumps({"responses_raw": {"q1": {"news_results": []}}}), encoding="utf-8")
        assert serpapi_errors(p) == []

    def test_missing_bundle_reported(self, tmp_path):
        from scheduler import serpapi_errors
        errs = serpapi_errors(tmp_path / "missing.json")
        assert len(errs) == 1 and "bundle sa nedá načítať" in errs[0]


class TestExtractStateSql:
    """„Neúspešná extrakcia“ na úvodnej stránke a filter /browse?state=failed používajú tú istú podmienku."""

    def test_failed_excludes_irrelevant(self):
        from app import _extract_state_sql
        sql = _extract_state_sql("failed")
        assert "(a.relevance IS NULL OR a.relevance <> 0)" in sql
        assert "a.content_text IS NULL" in sql

    def test_pending_keeps_irrelevant(self):
        from app import _extract_state_sql
        assert "relevance" not in _extract_state_sql("pending")

    def test_browse_filter_uses_same_condition(self):
        from app import build_filters, _extract_state_sql
        where, _ = build_filters(0, "all", False, "all", False, False, extract_state="failed")
        assert _extract_state_sql("failed") in where
        assert "a.deleted_at IS NULL" in where and "s.is_avoided = 0" in where
