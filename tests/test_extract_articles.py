"""
Tests for pure helper functions in extract_articles_reworked.py.
Network calls (resolve_and_fetch) and trafilatura are not tested here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from extract_articles_reworked import (
    CandidateArticle,
    canonicalize_url,
    dedupe_candidates,
    domain_root,
    extract_candidates_from_doc,
    find_topic_matches,
    is_js_shell,
    parse_dt,
    sha256_hex,
)


# ── sha256_hex ────────────────────────────────────────────────────────────────

class TestSha256Hex:
    def test_known_hash(self):
        import hashlib
        expected = hashlib.sha256(b"hello").hexdigest()
        assert sha256_hex("hello") == expected

    def test_empty_string(self):
        import hashlib
        assert sha256_hex("") == hashlib.sha256(b"").hexdigest()

    def test_different_inputs_differ(self):
        assert sha256_hex("a") != sha256_hex("b")

    def test_returns_hex_string(self):
        h = sha256_hex("test")
        assert isinstance(h, str)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ── domain_root ───────────────────────────────────────────────────────────────

class TestDomainRoot:
    def test_strips_www(self):
        assert domain_root("www.example.com") == "example.com"

    def test_plain_domain(self):
        assert domain_root("example.com") == "example.com"

    def test_subdomain(self):
        assert domain_root("news.bbc.co.uk") == "co.uk"

    def test_two_part_domain(self):
        assert domain_root("aps.dz") == "aps.dz"

    def test_empty_string(self):
        assert domain_root("") == ""

    def test_single_label(self):
        assert domain_root("localhost") == "localhost"

    def test_uppercased_input(self):
        assert domain_root("WWW.Example.COM") == "example.com"


# ── canonicalize_url ──────────────────────────────────────────────────────────

class TestCanonicalizeUrl:
    def test_strips_www(self):
        # root path "/" is preserved; only non-root trailing slashes are stripped
        assert canonicalize_url("https://www.example.com/") == "https://example.com/"

    def test_removes_utm_params(self):
        url = "https://example.com/article?utm_source=twitter&utm_medium=social"
        assert "utm_source" not in canonicalize_url(url)
        assert "utm_medium" not in canonicalize_url(url)

    def test_keeps_non_tracking_params(self):
        url = "https://example.com/search?q=slovakia&page=2"
        canon = canonicalize_url(url)
        assert "q=slovakia" in canon
        assert "page=2" in canon

    def test_sorts_query_params(self):
        url1 = "https://example.com/?b=2&a=1"
        url2 = "https://example.com/?a=1&b=2"
        assert canonicalize_url(url1) == canonicalize_url(url2)

    def test_strips_trailing_slash(self):
        assert canonicalize_url("https://example.com/path/") == "https://example.com/path"

    def test_root_slash_kept(self):
        # Root "/" is not stripped — only non-root trailing slashes are
        result = canonicalize_url("https://example.com/")
        assert result == "https://example.com/"

    def test_empty_string_returns_empty(self):
        assert canonicalize_url("") == ""

    def test_lowercases_scheme_and_host(self):
        result = canonicalize_url("HTTPS://EXAMPLE.COM/Path")
        assert result.startswith("https://example.com")

    def test_drops_fbclid(self):
        url = "https://example.com/a?fbclid=abc123"
        assert "fbclid" not in canonicalize_url(url)

    def test_fragment_stripped(self):
        url = "https://example.com/page#section"
        assert "#section" not in canonicalize_url(url)


# ── parse_dt ──────────────────────────────────────────────────────────────────

class TestParseDt:
    def test_none_returns_none(self):
        assert parse_dt(None) is None

    def test_empty_string_returns_none(self):
        assert parse_dt("") is None

    def test_iso_date_string(self):
        result = parse_dt("2024-01-15")
        assert result is not None
        assert "2024-01-15" in result

    def test_iso_datetime_with_z(self):
        result = parse_dt("2024-01-15T12:30:00Z")
        assert result is not None
        assert "2024-01-15" in result

    def test_datetime_object_with_tz(self):
        dt = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
        result = parse_dt(dt)
        assert result is not None
        assert "2024-06-01" in result

    def test_datetime_object_naive_gets_utc(self):
        dt = datetime(2024, 6, 1, 10, 0)
        result = parse_dt(dt)
        assert result is not None
        assert "+00:00" in result

    def test_unparseable_returns_original_string(self):
        result = parse_dt("not-a-date")
        assert result == "not-a-date"


# ── is_js_shell ───────────────────────────────────────────────────────────────

class TestIsJsShell:
    def test_detects_root_div_with_bundle(self):
        html = '<html><div id="root"></div><script src="/bundles/v1/views/latest/main.js"></script></html>'
        assert is_js_shell(html) is True

    def test_detects_ssr_markers(self):
        html = '<html data-ssr-entry="1"><div ssr-service-entry="app"></div></html>'
        assert is_js_shell(html) is True

    def test_normal_html_is_not_shell(self):
        html = "<html><body><h1>Normal article</h1><p>Content here.</p></body></html>"
        assert is_js_shell(html) is False

    def test_empty_html(self):
        assert is_js_shell("") is False

    def test_none_html(self):
        assert is_js_shell(None) is False


# ── find_topic_matches ────────────────────────────────────────────────────────

class TestFindTopicMatches:
    KEYWORDS = {
        "diplomacy": ["Slovakia", "Bratislava", "ambassador"],
        "economy": ["trade", "export", "GDP"],
    }

    def test_single_topic_match(self):
        topics, kws = find_topic_matches("Slovakia signs new trade deal", self.KEYWORDS)
        assert "diplomacy" in topics
        assert "Slovakia" in kws["diplomacy"]

    def test_multiple_topics_matched(self):
        text = "Bratislava trade mission boosts exports"
        topics, kws = find_topic_matches(text, self.KEYWORDS)
        assert "diplomacy" in topics
        assert "economy" in topics

    def test_no_match(self):
        topics, kws = find_topic_matches("Weather forecast for tomorrow", self.KEYWORDS)
        assert topics == []
        assert kws == {}

    def test_case_insensitive(self):
        topics, _ = find_topic_matches("SLOVAKIA relations with Algeria", self.KEYWORDS)
        assert "diplomacy" in topics

    def test_empty_text(self):
        topics, kws = find_topic_matches("", self.KEYWORDS)
        assert topics == []

    def test_empty_keywords_dict(self):
        topics, kws = find_topic_matches("Slovakia", {})
        assert topics == []

    def test_duplicate_keywords_deduplicated(self):
        # "Slovakia" appears twice in the text, should only appear once in hits
        _, kws = find_topic_matches("Slovakia Slovakia Slovakia", self.KEYWORDS)
        assert kws.get("diplomacy", []).count("Slovakia") == 1


# ── extract_candidates_from_doc ───────────────────────────────────────────────

class TestExtractCandidatesFromDoc:
    def _path(self):
        return Path("test_bundle.json")

    def test_top_level_news_results(self):
        doc = {
            "news_results": [
                {"link": "https://aps.dz/article1", "title": "Title 1"},
                {"link": "https://tsa-algerie.com/article2", "title": "Title 2"},
            ]
        }
        candidates = extract_candidates_from_doc(doc, self._path())
        assert len(candidates) == 2
        urls = {c.url for c in candidates}
        assert "https://aps.dz/article1" in urls

    def test_responses_raw_structure(self):
        doc = {
            "queries": {"q1": "Slovakia site:.dz"},
            "responses_raw": {
                "q1": {
                    "news_results": [
                        {"link": "https://aps.dz/story", "title": "Story"},
                    ]
                }
            },
        }
        candidates = extract_candidates_from_doc(doc, self._path())
        assert len(candidates) == 1
        assert candidates[0].query_key == "q1"
        assert candidates[0].query_text == "Slovakia site:.dz"

    def test_responses_clean_structure(self):
        doc = {
            "responses_clean": {
                "q2": {
                    "news_results": [
                        {"link": "https://elwatan.com/news", "title": "News"},
                    ]
                }
            }
        }
        candidates = extract_candidates_from_doc(doc, self._path())
        assert len(candidates) == 1
        assert candidates[0].origin_path == "responses_clean.q2.news_results"

    def test_skips_items_without_url(self):
        doc = {"news_results": [{"title": "No URL here"}]}
        candidates = extract_candidates_from_doc(doc, self._path())
        assert candidates == []

    def test_url_field_alternative(self):
        doc = {"news_results": [{"url": "https://example.com/page", "title": "Alt URL"}]}
        candidates = extract_candidates_from_doc(doc, self._path())
        assert len(candidates) == 1
        assert candidates[0].url == "https://example.com/page"

    def test_non_dict_returns_empty(self):
        assert extract_candidates_from_doc([], self._path()) == []
        assert extract_candidates_from_doc(None, self._path()) == []


# ── dedupe_candidates ─────────────────────────────────────────────────────────

class TestDedupeCandiates:
    def _make(self, url: str) -> CandidateArticle:
        return CandidateArticle(url=url, title="Title")

    def test_deduplicates_same_url(self):
        url = "https://aps.dz/article"
        items = [self._make(url), self._make(url)]
        result = dedupe_candidates(items)
        assert len(result) == 1

    def test_deduplicates_by_canonical_form(self):
        # www vs no-www and trailing slash
        a = self._make("https://www.aps.dz/article/")
        b = self._make("https://aps.dz/article")
        result = dedupe_candidates([a, b])
        assert len(result) == 1

    def test_deduplicates_utm_variants(self):
        a = self._make("https://aps.dz/art?utm_source=fb")
        b = self._make("https://aps.dz/art?utm_medium=email")
        result = dedupe_candidates([a, b])
        assert len(result) == 1

    def test_different_urls_kept(self):
        items = [
            self._make("https://aps.dz/art1"),
            self._make("https://aps.dz/art2"),
        ]
        result = dedupe_candidates(items)
        assert len(result) == 2

    def test_skips_empty_url(self):
        items = [self._make(""), self._make("https://aps.dz/valid")]
        result = dedupe_candidates(items)
        assert len(result) == 1
        assert result[0].url == "https://aps.dz/valid"
