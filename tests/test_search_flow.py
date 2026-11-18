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

    def test_dd_mm_yyyy_is_absolute(self):
        iso, is_abs = _parse_serp_date("9.12.2025", self._NOW)
        assert iso == "2025-12-09"
        assert is_abs is True

    def test_dd_mm_yyyy_zero_padded(self):
        iso, is_abs = _parse_serp_date("09.01.2026", self._NOW)
        assert iso == "2026-01-09"
        assert is_abs is True

    def test_dd_mm_yyyy_invalid_date_returns_none(self):
        iso, is_abs = _parse_serp_date("32.13.2025", self._NOW)
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


# ── SerpAPI engine google_news ────────────────────────────────────────────────

class TestGoogleNews:
    def test_when_operator(self):
        from search_flow_news import to_google_news_when
        assert to_google_news_when("7d") == "when:7d"
        assert to_google_news_when("2h") == "when:2h"
        assert to_google_news_when("2w") == "when:14d"
        assert to_google_news_when("1m") == "when:30d"
        assert to_google_news_when("nonsense") == "when:7d"

    def test_query_strips_inurl_and_old_when(self):
        from search_flow_news import build_google_news_query
        q = "(site:aps.dz OR site:elwatan.com) (Slovaquie) -inurl:recherche -inurl:tag when:30d"
        assert build_google_news_query(q, "7d") == "(site:aps.dz OR site:elwatan.com) (Slovaquie) when:7d"

    def test_normalize_items(self):
        from search_flow_news import normalize_google_news_items
        items = [
            {"position": 5, "title": "A", "link": "https://aps.dz/a", "source": {"name": "APS", "icon": "x"},
             "date": "11/12/2026, 08:49 PM, +0000 UTC", "iso_date": "2026-11-12T20:49:18Z"},
            {"position": 6, "title": "cluster", "stories": [
                {"title": "B", "link": "https://elwatan.com/b", "source": {"name": "El Watan"},
                 "iso_date": "2026-11-13T10:00:00Z"}]},
            {"title": "bez linku"},
        ]
        out = normalize_google_news_items(items)
        assert [i["title"] for i in out] == ["A", "B"]
        assert out[0]["source"] == "APS" and out[1]["source"] == "El Watan"
        assert out[0]["date"] == "2026-11-12T20:49:18Z"
        assert [i["position"] for i in out] == [1, 2]

    def test_filter_window_drops_old(self):
        from datetime import datetime, timedelta, timezone
        from search_flow_news import _filter_window
        now = datetime.now(timezone.utc)
        fresh = {"link": "https://aps.dz/a", "iso_date": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        old = {"link": "https://aps.dz/b", "iso_date": "2023-03-12T23:08:16Z"}
        nodate = {"link": "https://aps.dz/c"}
        assert _filter_window([fresh, old, nodate], "7d") == [fresh, nodate]

    def test_search_uses_google_news_and_handles_error(self, monkeypatch):
        import search_flow_news as sfn
        sent = {}

        class FakeSearch:
            def __init__(self, params): sent.update(params)
            def get_dict(self): return {"error": "Some SerpAPI failure"}

        monkeypatch.setattr(sfn, "GoogleSearch", FakeSearch)
        data = sfn.serpapi_google_news_search("site:.dz (Slovaquie)", "k", 20, "fr", "dz", "7d")
        assert sent["engine"] == "google_news" and "tbm" not in sent
        assert sent["q"] == "site:.dz (Slovaquie) when:7d"
        assert data["news_results"] == [] and data["error"] == "Some SerpAPI failure"

    def test_search_returns_normalized_capped(self, monkeypatch):
        from datetime import datetime, timezone
        import search_flow_news as sfn
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        results = [{"title": f"T{i}", "link": f"https://aps.dz/{i}", "source": {"name": "APS"}, "iso_date": iso}
                   for i in range(5)]

        class FakeSearch:
            def __init__(self, params): pass
            def get_dict(self): return {"news_results": results}

        monkeypatch.setattr(sfn, "GoogleSearch", FakeSearch)
        data = sfn.serpapi_google_news_search("q", "k", 3, "fr", "dz", "7d")
        assert [i["title"] for i in data["news_results"]] == ["T0", "T1", "T2"]
        assert data["news_results"][0]["source"] == "APS"

    def test_transient_error_detection(self):
        import search_flow_news as sfn
        assert sfn.is_transient_serp_error("We couldn't get valid results for this search. Please try again later.")
        assert not sfn.is_transient_serp_error("Invalid API key. Your API key should be here: …")
        assert not sfn.is_transient_serp_error("Your account has run out of searches.")
        assert not sfn.is_transient_serp_error(None)

    def _fake_sequence(self, monkeypatch, responses):
        import search_flow_news as sfn
        calls = []

        class FakeSearch:
            def __init__(self, params): calls.append(params)
            def get_dict(self): return responses[len(calls) - 1]

        monkeypatch.setattr(sfn, "GoogleSearch", FakeSearch)
        monkeypatch.setattr(sfn, "SERP_RETRY_DELAY", 0)
        return sfn, calls

    def test_transient_error_retried_then_succeeds(self, monkeypatch):
        from datetime import datetime, timezone
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ok = {"news_results": [{"title": "T", "link": "https://aps.dz/1", "source": {"name": "APS"}, "iso_date": iso}]}
        err = {"error": "We couldn't get valid results for this search. Please try again later."}
        sfn, calls = self._fake_sequence(monkeypatch, [err, ok])
        data = sfn.serpapi_google_news_search("q", "k", 20, "fr", "dz", "7d")
        assert len(calls) == 2 and calls[1]["no_cache"] == "true" and "no_cache" not in calls[0]
        assert "error" not in data and len(data["news_results"]) == 1

    def test_transient_error_gives_up_after_retries(self, monkeypatch):
        err = {"error": "We couldn't get valid results for this search. Please try again later."}
        sfn, calls = self._fake_sequence(monkeypatch, [dict(err) for _ in range(5)])
        data = sfn.serpapi_google_news_search("q", "k", 20, "fr", "dz", "7d")
        assert len(calls) == 1 + sfn.SERP_RETRIES
        assert data["error"] == err["error"] and data["news_results"] == []

    def test_permanent_error_not_retried(self, monkeypatch):
        sfn, calls = self._fake_sequence(monkeypatch, [{"error": "Invalid API key."}])
        sfn.serpapi_google_news_search("q", "k", 20, "fr", "dz", "7d")
        assert len(calls) == 1


class TestNormalizeWhen:
    def test_values(self):
        from search_flow_news import normalize_when
        assert normalize_when("35") == "35d"
        assert normalize_when(" 35D ") == "35d"
        assert normalize_when("2w") == "14d"
        assert normalize_when("2h") == "2h"
        assert normalize_when("1m") == "1m"
        assert normalize_when("abc") == "7d"
        assert normalize_when(None) == "7d"
