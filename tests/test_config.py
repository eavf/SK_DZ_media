"""
Tests for pure helper functions in config/config.py.
These functions have no side effects and need no DB / network.
"""
from __future__ import annotations

import pytest

from config.config import (
    _csv_set,
    _normalize_domain_list,
    _normalize_rank_map,
    _normalize_string_list,
    _normalize_topics,
    _to_bool,
    _to_int,
)


# ── _to_bool ──────────────────────────────────────────────────────────────────

class TestToBool:
    @pytest.mark.parametrize("val", ["1", "true", "True", "TRUE", "yes", "y", "on"])
    def test_truthy_values(self, val):
        assert _to_bool(val) is True

    @pytest.mark.parametrize("val", ["0", "false", "False", "FALSE", "no", "n", "off"])
    def test_falsy_values(self, val):
        assert _to_bool(val) is False

    def test_none_returns_default_false(self):
        assert _to_bool(None) is False

    def test_none_returns_custom_default(self):
        assert _to_bool(None, default=True) is True

    def test_unknown_string_returns_default(self):
        assert _to_bool("maybe") is False
        assert _to_bool("maybe", default=True) is True

    def test_strips_whitespace(self):
        assert _to_bool("  true  ") is True
        assert _to_bool("  0  ") is False


# ── _to_int ───────────────────────────────────────────────────────────────────

class TestToInt:
    def test_valid_integer_string(self):
        assert _to_int("42", 0) == 42

    def test_negative_integer(self):
        assert _to_int("-7", 0) == -7

    def test_none_returns_default(self):
        assert _to_int(None, 99) == 99

    def test_empty_string_returns_default(self):
        assert _to_int("", 5) == 5
        assert _to_int("   ", 5) == 5

    def test_non_numeric_returns_default(self):
        assert _to_int("abc", 10) == 10

    def test_strips_whitespace(self):
        assert _to_int("  7  ", 0) == 7


# ── _csv_set ──────────────────────────────────────────────────────────────────

class TestCsvSet:
    def test_simple_csv(self):
        assert _csv_set("a,b,c") == {"a", "b", "c"}

    def test_lowercases(self):
        assert _csv_set("Foo,BAR") == {"foo", "bar"}

    def test_strips_whitespace(self):
        assert _csv_set("  a , b , c  ") == {"a", "b", "c"}

    def test_empty_string_returns_empty_set(self):
        assert _csv_set("") == set()
        assert _csv_set("   ") == set()

    def test_none_returns_empty_set(self):
        assert _csv_set(None) == set()

    def test_skips_empty_segments(self):
        assert _csv_set("a,,b") == {"a", "b"}


# ── _normalize_domain_list ────────────────────────────────────────────────────

class TestNormalizeDomainList:
    def test_basic_list(self):
        assert _normalize_domain_list(["Foo.com", "BAR.org"]) == {"foo.com", "bar.org"}

    def test_strips_whitespace(self):
        assert _normalize_domain_list(["  aps.dz  "]) == {"aps.dz"}

    def test_none_returns_empty(self):
        assert _normalize_domain_list(None) == set()

    def test_empty_list(self):
        assert _normalize_domain_list([]) == set()

    def test_non_list_returns_empty(self):
        assert _normalize_domain_list("not a list") == set()
        assert _normalize_domain_list(42) == set()

    def test_skips_blank_entries(self):
        assert _normalize_domain_list(["aps.dz", "", "  "]) == {"aps.dz"}

    def test_set_input(self):
        result = _normalize_domain_list({"A.com", "B.com"})
        assert result == {"a.com", "b.com"}


# ── _normalize_rank_map ───────────────────────────────────────────────────────

class TestNormalizeRankMap:
    def test_basic_dict(self):
        assert _normalize_rank_map({"aps.dz": 100, "TSA.COM": 90}) == {
            "aps.dz": 100,
            "tsa.com": 90,
        }

    def test_non_dict_returns_empty(self):
        assert _normalize_rank_map([]) == {}
        assert _normalize_rank_map(None) == {}

    def test_skips_invalid_score(self):
        result = _normalize_rank_map({"good.dz": 50, "bad.dz": "not_a_number"})
        assert "good.dz" in result
        assert "bad.dz" not in result

    def test_skips_blank_domain(self):
        result = _normalize_rank_map({"": 10, "  ": 20, "real.dz": 5})
        assert result == {"real.dz": 5}

    def test_string_numbers_coerced(self):
        assert _normalize_rank_map({"aps.dz": "75"}) == {"aps.dz": 75}


# ── _normalize_topics ─────────────────────────────────────────────────────────

class TestNormalizeTopics:
    def test_basic(self):
        result = _normalize_topics({"Diplomacy": ["Slovakia", "Bratislava"]})
        assert result == {"diplomacy": ["Slovakia", "Bratislava"]}

    def test_non_dict_returns_empty(self):
        assert _normalize_topics(None) == {}
        assert _normalize_topics([]) == {}

    def test_skips_non_list_values(self):
        result = _normalize_topics({"valid": ["kw"], "invalid": "not a list"})
        assert "valid" in result
        assert "invalid" not in result

    def test_skips_blank_topic_keys(self):
        result = _normalize_topics({"": ["kw"], "  ": ["kw2"], "real": ["kw3"]})
        assert result == {"real": ["kw3"]}

    def test_skips_blank_keywords(self):
        result = _normalize_topics({"t": ["word", "", "  "]})
        assert result == {"t": ["word"]}


# ── _normalize_string_list ────────────────────────────────────────────────────

class TestNormalizeStringList:
    def test_basic(self):
        assert _normalize_string_list(["UTM_SOURCE", "fbclid"]) == {"utm_source", "fbclid"}

    def test_none_returns_empty(self):
        assert _normalize_string_list(None) == set()

    def test_empty_list(self):
        assert _normalize_string_list([]) == set()

    def test_skips_blank_entries(self):
        assert _normalize_string_list(["a", "", "  "]) == {"a"}

    def test_non_list_returns_empty(self):
        assert _normalize_string_list("not-a-list") == set()
