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
