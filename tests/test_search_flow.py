"""
Tests for pure helper functions in search_flow_news.py.
SerpAPI calls and filesystem operations are not tested here.
"""
from __future__ import annotations

import pytest

from datetime import datetime, timezone

from search_flow_news import (
    BLOCKLIST_DOMAINS,
    _enrich_dates,
    _parse_serp_date,
    clean_news_results,
    contains_preferred_links,
    deduplicate_news,
    domain_of,
    domain_stats,
    extract_links_from_news,
    extract_news_items,
    is_full_news_page_raw,
    looks_unwanted_news_item,
    mentions_slovakia,
    should_run_preferred_fallback,
    slovakia_terms,
)


# ── domain_of ─────────────────────────────────────────────────────────────────

class TestDomainOf:
    def test_strips_www(self):
        assert domain_of("https://www.aps.dz/news") == "aps.dz"

    def test_plain(self):
        assert domain_of("https://elwatan.com/article") == "elwatan.com"

    def test_lowercases(self):
        assert domain_of("https://TSA-ALGERIE.COM/page") == "tsa-algerie.com"

    def test_empty(self):
        assert domain_of("") == ""


# ── mentions_slovakia ─────────────────────────────────────────────────────────

class TestMentionsSlovakia:
    def test_detects_slovakia_in_title(self):
        assert mentions_slovakia({"title": "Slovakia signs trade deal", "snippet": ""})

    def test_detects_bratislava_in_snippet(self):
        assert mentions_slovakia({"title": "Summit", "snippet": "Held in Bratislava"})

    def test_case_insensitive(self):
        assert mentions_slovakia({"title": "SLOVAKIA", "snippet": ""})
        assert mentions_slovakia({"title": "slovak relations", "snippet": ""})

    def test_arabic_term(self):
        assert mentions_slovakia({"title": "سلوفاكيا توقع اتفاقية", "snippet": ""})

    def test_no_mention(self):
        assert not mentions_slovakia({"title": "Algeria economy", "snippet": "GDP growth"})

    def test_missing_fields(self):
        assert not mentions_slovakia({})


# ── looks_unwanted_news_item ──────────────────────────────────────────────────

class TestLooksUnwantedNewsItem:
    def test_no_link_is_unwanted(self):
        assert looks_unwanted_news_item({"title": "Something"})

    def test_blocklisted_domain(self):
        domain = next(iter(BLOCKLIST_DOMAINS))
        assert looks_unwanted_news_item({"link": f"https://{domain}/page"})

    def test_ecom_path(self):
        assert looks_unwanted_news_item({"link": "https://aps.dz/produit/shoes"})
        assert looks_unwanted_news_item({"link": "https://aps.dz/shop/basket"})

    def test_bad_path_markers(self):
        assert looks_unwanted_news_item({"link": "https://aps.dz/tag/politics"})
        assert looks_unwanted_news_item({"link": "https://aps.dz/page/3"})
        assert looks_unwanted_news_item({"link": "https://aps.dz/search?q=test"})

    def test_bad_title_word(self):
        assert looks_unwanted_news_item({"link": "https://aps.dz/news", "title": "Promo spéciale"})
        assert looks_unwanted_news_item({"link": "https://aps.dz/news", "title": "Prix réduits"})

    def test_normal_article_is_wanted(self):
        item = {"link": "https://aps.dz/politics/slovakia-summit", "title": "Slovakia summit in Algeria"}
        assert not looks_unwanted_news_item(item)

    def test_opac_css_path(self):
        assert looks_unwanted_news_item({"link": "https://library.dz/opac_css/"})


# ── extract_news_items ────────────────────────────────────────────────────────

class TestExtractNewsItems:
    def test_basic(self):
        r = {"news_results": [{"link": "https://aps.dz/a"}]}
        assert extract_news_items(r) == [{"link": "https://aps.dz/a"}]

    def test_missing_key_returns_empty(self):
        assert extract_news_items({}) == []

    def test_none_value_returns_empty(self):
        assert extract_news_items({"news_results": None}) == []


# ── clean_news_results ────────────────────────────────────────────────────────

class TestCleanNewsResults:
    def _results(self, items):
        return {"news_results": items}

    def test_removes_unwanted(self):
        items = [
            {"link": "https://aps.dz/good-article", "title": "Good"},
            {"link": "https://aps.dz/tag/sports", "title": "Tag page"},
        ]
        cleaned = clean_news_results(self._results(items), limit=10)
        assert len(cleaned["news_results"]) == 1
        assert cleaned["news_results"][0]["title"] == "Good"

    def test_respects_limit(self):
        items = [{"link": f"https://aps.dz/art{i}", "title": f"T{i}"} for i in range(20)]
        cleaned = clean_news_results(self._results(items), limit=5)
        assert len(cleaned["news_results"]) == 5

    def test_cleaning_info_added(self):
        items = [{"link": "https://aps.dz/good", "title": "Good"}]
        cleaned = clean_news_results(self._results(items), limit=10)
        assert "cleaning_info" in cleaned
        assert cleaned["cleaning_info"]["before"] == 1
        assert cleaned["cleaning_info"]["after"] == 1

    def test_empty_results(self):
        cleaned = clean_news_results(self._results([]), limit=10)
        assert cleaned["news_results"] == []


# ── extract_links_from_news ───────────────────────────────────────────────────

class TestExtractLinksFromNews:
    def test_extracts_links(self):
        r = {"news_results": [
            {"link": "https://aps.dz/a"},
            {"link": "https://tsa-algerie.com/b"},
        ]}
        assert extract_links_from_news(r) == ["https://aps.dz/a", "https://tsa-algerie.com/b"]

    def test_skips_items_without_link(self):
        r = {"news_results": [{"title": "No link"}, {"link": "https://aps.dz/ok"}]}
        assert extract_links_from_news(r) == ["https://aps.dz/ok"]


# ── deduplicate_news ──────────────────────────────────────────────────────────

class TestDeduplicateNews:
    def test_removes_duplicate_links(self):
        items = [
            {"link": "https://aps.dz/art"},
            {"link": "https://aps.dz/art"},
            {"link": "https://tsa-algerie.com/other"},
        ]
        result = deduplicate_news(items)
        assert len(result) == 2

    def test_keeps_order_of_first_occurrence(self):
        items = [
            {"link": "https://aps.dz/first"},
            {"link": "https://tsa-algerie.com/second"},
            {"link": "https://aps.dz/first"},  # duplicate
        ]
        result = deduplicate_news(items)
        assert result[0]["link"] == "https://aps.dz/first"

    def test_skips_items_without_link(self):
        items = [{"title": "No link"}, {"link": "https://aps.dz/ok"}]
        result = deduplicate_news(items)
        assert len(result) == 1

    def test_empty_input(self):
        assert deduplicate_news([]) == []


# ── contains_preferred_links ──────────────────────────────────────────────────

class TestContainsPreferredLinks:
    def test_true_when_preferred_present(self):
        # conftest sets PREFERRED_DOMAINS = {"aps.dz", "tsa-algerie.com"}
        assert contains_preferred_links(["https://aps.dz/article"])

    def test_false_when_no_preferred(self):
        assert not contains_preferred_links(["https://unknown-site.dz/article"])

    def test_empty_list(self):
        assert not contains_preferred_links([])


# ── is_full_news_page_raw ─────────────────────────────────────────────────────

class TestIsFullNewsPageRaw:
    def test_full_page(self):
        r = {"news_results": [{"link": f"https://aps.dz/art{i}"} for i in range(10)]}
        assert is_full_news_page_raw(r, num=10) is True

    def test_less_than_num(self):
        r = {"news_results": [{"link": "https://aps.dz/art1"}]}
        assert is_full_news_page_raw(r, num=10) is False

    def test_empty_results(self):
        assert is_full_news_page_raw({"news_results": []}, num=5) is False


# ── should_run_preferred_fallback ─────────────────────────────────────────────

class TestShouldRunPreferredFallback:
    def _items(self, n: int, preferred: bool = False) -> list[dict]:
        domain = "aps.dz" if preferred else "unknown.dz"
        return [{"link": f"https://{domain}/art{i}"} for i in range(n)]

    def test_triggers_when_too_few_results(self):
        combined = self._items(3)  # only 3 out of 10 needed (70% threshold)
        result = should_run_preferred_fallback({}, {}, combined, num=10)
        assert result is True

    def test_triggers_when_no_preferred_sources(self):
        combined = self._items(10)  # enough results but no preferred domain
        result = should_run_preferred_fallback({}, {}, combined, num=10)
        assert result is True

    def test_no_fallback_when_ok(self):
        # 10 items, at least 1 from preferred domain (aps.dz)
        combined = self._items(9) + self._items(1, preferred=True)
        result = should_run_preferred_fallback({}, {}, combined, num=10)
        assert result is False

    def test_empty_combined_triggers(self):
        result = should_run_preferred_fallback({}, {}, [], num=10)
        assert result is True


# ── _parse_serp_date ──────────────────────────────────────────────────────────

class TestParseSerp:
    _NOW = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)

    def test_iso_date_is_absolute(self):
        iso, is_abs = _parse_serp_date("2025-12-09", self._NOW)
        assert iso == "2025-12-09"
        assert is_abs is True

    def test_yesterday_is_relative(self):
        iso, is_abs = _parse_serp_date("yesterday", self._NOW)
        assert iso == "2026-03-29"
        assert is_abs is False

    def test_hier_is_relative(self):
        _, is_abs = _parse_serp_date("Hier", self._NOW)
        assert is_abs is False

    def test_fr_relative_jours(self):
        iso, is_abs = _parse_serp_date("il y a 3 jours", self._NOW)
        assert iso == "2026-03-27"
        assert is_abs is False

    def test_en_relative_days(self):
        iso, is_abs = _parse_serp_date("5 days ago", self._NOW)
        assert iso == "2026-03-25"
        assert is_abs is False

    def test_none_input(self):
        iso, is_abs = _parse_serp_date(None, self._NOW)
        assert iso is None
        assert is_abs is False

    def test_unparseable_returns_none(self):
        iso, is_abs = _parse_serp_date("some garbage string", self._NOW)
        assert iso is None
        assert is_abs is False


# ── _enrich_dates ─────────────────────────────────────────────────────────────

class TestEnrichDates:
    _NOW = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)

    def test_absolute_date_is_stored(self):
        items = [{"link": "https://aps.dz/a", "date": "2025-12-09"}]
        _enrich_dates(items, self._NOW)
        assert items[0].get("published_at") == "2025-12-09"

    def test_relative_date_is_not_stored(self):
        items = [{"link": "https://aps.dz/a", "date": "yesterday"}]
        _enrich_dates(items, self._NOW)
        assert "published_at" not in items[0]

    def test_relative_fr_date_is_not_stored(self):
        items = [{"link": "https://aps.dz/a", "date": "il y a 3 jours"}]
        _enrich_dates(items, self._NOW)
        assert "published_at" not in items[0]

    def test_existing_published_at_is_not_overwritten(self):
        items = [{"link": "https://aps.dz/a", "date": "2025-01-01", "published_at": "2025-12-09"}]
        _enrich_dates(items, self._NOW)
        assert items[0]["published_at"] == "2025-12-09"

    def test_no_date_field_leaves_no_published_at(self):
        items = [{"link": "https://aps.dz/a"}]
        _enrich_dates(items, self._NOW)
        assert "published_at" not in items[0]


# ── domain_stats ──────────────────────────────────────────────────────────────

class TestDomainStats:
    def test_counts_domains(self):
        items = [
            {"link": "https://aps.dz/a"},
            {"link": "https://aps.dz/b"},
            {"link": "https://tsa-algerie.com/c"},
        ]
        stats = domain_stats(items)
        assert stats["aps.dz"] == 2
        assert stats["tsa-algerie.com"] == 1

    def test_skips_items_without_link(self):
        items = [{"title": "no link"}, {"link": "https://aps.dz/a"}]
        stats = domain_stats(items)
        assert "aps.dz" in stats

    def test_empty_input(self):
        assert domain_stats([]) == {}
