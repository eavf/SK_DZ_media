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
                                               "duplicate": 2, "ai_sport": 4, "pdf_unreadable": 0, "bad_url": 0}

    def test_old_format_without_new_keys(self):
        from app import _parse_extract_summary
        assert _parse_extract_summary("Hotovo: ok=1, soft_deleted=0, commercial=0, failed=0") == {
            "ok": 1, "soft_deleted": 0, "commercial": 0, "failed": 0, "duplicate": 0, "ai_sport": 0, "pdf_unreadable": 0, "bad_url": 0}

    def test_no_summary(self):
        from app import _parse_extract_summary
        assert _parse_extract_summary("Žiadne články na extrakciu.") is None
