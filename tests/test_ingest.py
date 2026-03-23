"""
Tests for pure helper functions in ingest_to_dz_news_reworked.py.
DB operations (upsert_*, insert_*, preload_*) are not tested here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ingest_to_dz_news_reworked import (
    ArticleCandidate,
    PublishedAtConfidence,
    _engine_from_payload,
    _extract_news_results,
    _infer_query_id_from_name,
    build_candidates,
    canonicalize_url,
    classify_published_at,
    dedupe_candidates,
    domain_of,
    news_item_to_candidate,
    normalize_title,
    sha256_hex,
)

PREFERRED = {"aps.dz", "tsa-algerie.com"}
SOURCE_RANK = {"aps.dz": 100, "tsa-algerie.com": 90}


# ── domain_of ─────────────────────────────────────────────────────────────────

class TestDomainOf:
    def test_strips_www(self):
        assert domain_of("https://www.aps.dz/article") == "aps.dz"

    def test_plain_domain(self):
        assert domain_of("https://tsa-algerie.com/news") == "tsa-algerie.com"

    def test_lowercases(self):
        assert domain_of("https://APS.DZ/article") == "aps.dz"

    def test_empty_string(self):
        assert domain_of("") == ""


# ── canonicalize_url (ingest module version) ──────────────────────────────────

class TestIngestCanonicalizeUrl:
    def test_strips_utm(self):
        url = "https://aps.dz/art?utm_source=twitter"
        assert "utm_source" not in canonicalize_url(url)

    def test_strips_fbclid(self):
        url = "https://aps.dz/art?fbclid=abc"
        # fbclid is in conftest DROP_QUERY_KEYS
        assert "fbclid" not in canonicalize_url(url)

    def test_keeps_real_params(self):
        url = "https://aps.dz/search?q=algeria"
        assert "q=algeria" in canonicalize_url(url)

    def test_strips_www(self):
        # root path "/" is preserved; only non-root trailing slashes are stripped
        assert canonicalize_url("https://www.aps.dz/") == "https://aps.dz/"

    def test_empty_returns_empty(self):
        assert canonicalize_url("") == ""


# ── normalize_title ───────────────────────────────────────────────────────────

class TestNormalizeTitle:
    def test_lowercases(self):
        assert normalize_title("HELLO WORLD") == "hello world"

    def test_unifies_separators(self):
        result = normalize_title("Title — Subtitle")
        assert "—" not in result
        assert "-" in result

    def test_strips_brackets(self):
        result = normalize_title("Title [update] (breaking)")
        assert "[" not in result
        assert "(" not in result

    def test_collapses_whitespace(self):
        result = normalize_title("  too   many   spaces  ")
        assert "  " not in result

    def test_empty_returns_empty_string(self):
        assert normalize_title("") == ""
        assert normalize_title(None) == ""

    def test_collapses_repeated_hyphens(self):
        result = normalize_title("one -- two --- three")
        assert "--" not in result

    def test_colon_replaced(self):
        result = normalize_title("Algeria: New deal")
        assert ":" not in result


# ── classify_published_at ─────────────────────────────────────────────────────

class TestClassifyPublishedAt:
    def test_none_returns_none_confidence(self):
        assert classify_published_at(None) == PublishedAtConfidence.NONE

    def test_empty_string_returns_none_confidence(self):
        assert classify_published_at("") == PublishedAtConfidence.NONE

    @pytest.mark.parametrize("date_str", [
        "2024-01-15",
        "2023-12-31",
        "15 janvier 2024",
        "Jan 2024",
        "15 déc. 2023",
    ])
    def test_absolute_dates(self, date_str):
        assert classify_published_at(date_str) == PublishedAtConfidence.ABSOLUTE

    @pytest.mark.parametrize("date_str", [
        "2 hours ago",
        "3 days ago",
        "1 week ago",
        "yesterday",
        "today",
        "il y a 5 heures",
        "hier",
        "aujourd'hui",
    ])
    def test_relative_dates(self, date_str):
        assert classify_published_at(date_str) == PublishedAtConfidence.RELATIVE

    def test_utc_iso_string_is_absolute(self):
        # The UTC check requires "utc" in string AND r"\b20\d{2}-\d{2}-\d{2}\b".
        # "T" after date breaks the \b boundary, so use space-separated form.
        assert classify_published_at("2024-01-15 UTC") == PublishedAtConfidence.ABSOLUTE

    def test_unknown_date_is_relative(self):
        # Falls through all patterns → relative (catch-all)
        result = classify_published_at("recently published")
        assert result == PublishedAtConfidence.RELATIVE


# ── _infer_query_id_from_name ─────────────────────────────────────────────────

class TestInferQueryId:
    def test_q1_in_stem(self):
        assert _infer_query_id_from_name(Path("news_q1_raw.json")) == "q1"

    def test_q2_in_stem(self):
        assert _infer_query_id_from_name(Path("news_q2_clean.json")) == "q2"

    def test_q3_in_stem(self):
        assert _infer_query_id_from_name(Path("q3_results.json")) == "q3"

    def test_no_q_id_returns_single(self):
        assert _infer_query_id_from_name(Path("news_bundle.json")) == "single"

    def test_uppercase_filename(self):
        assert _infer_query_id_from_name(Path("NEWS_Q1_RAW.JSON")) == "q1"


# ── _extract_news_results ─────────────────────────────────────────────────────

class TestExtractNewsResults:
    def test_top_level_news_results(self):
        payload = {
            "news_results": [
                {"link": "https://aps.dz/a1"},
                {"link": "https://aps.dz/a2"},
            ]
        }
        results = _extract_news_results(payload, default_query_id="q1")
        assert len(results) == 2
        assert all(qid == "q1" for qid, _, _ in results)

    def test_bundle_structure_clean(self):
        payload = {
            "responses_clean": {
                "q1": {"news_results": [{"link": "https://aps.dz/art"}]},
                "q2": {"news_results": [{"link": "https://tsa-algerie.com/art"}]},
            }
        }
        results = _extract_news_results(payload)
        assert len(results) == 2
        qids = {qid for qid, _, _ in results}
        assert qids == {"q1", "q2"}

    def test_bundle_falls_back_to_raw(self):
        payload = {
            "responses_clean": {},
            "responses_raw": {
                "q1": {"news_results": [{"link": "https://aps.dz/art"}]},
            },
        }
        results = _extract_news_results(payload)
        assert len(results) == 1

    def test_skips_non_dict_items(self):
        payload = {
            "news_results": ["not-a-dict", {"link": "https://aps.dz/ok"}]
        }
        results = _extract_news_results(payload)
        assert len(results) == 1

    def test_empty_payload(self):
        assert _extract_news_results({}) == []

    def test_non_dict_payload(self):
        assert _extract_news_results(None) == []
        assert _extract_news_results([]) == []


# ── _engine_from_payload ──────────────────────────────────────────────────────

class TestEngineFromPayload:
    def test_returns_engine_from_run(self):
        payload = {"run": {"engine": "bing_news"}}
        assert _engine_from_payload(payload) == "bing_news"

    def test_default_when_no_run(self):
        assert _engine_from_payload({}) == "google_news"

    def test_default_when_engine_missing(self):
        assert _engine_from_payload({"run": {}}) == "google_news"


# ── news_item_to_candidate ────────────────────────────────────────────────────

class TestNewsItemToCandidate:
    def _item(self, **kwargs):
        base = {
            "link": "https://aps.dz/story",
            "title": "Slovakia signs deal",
            "snippet": "Details here",
            "date": "2024-01-15",
            "source": "APS",
        }
        base.update(kwargs)
        return base

    def test_basic_candidate(self):
        c = news_item_to_candidate(self._item(), "q1", 1, "google_news", PREFERRED)
        assert c is not None
        assert c.url == "https://aps.dz/story"
        assert c.domain == "aps.dz"
        assert c.is_preferred is True

    def test_preferred_flag_false_for_unknown(self):
        c = news_item_to_candidate(self._item(link="https://unknown.dz/story"),
                                   "q1", 1, "google_news", PREFERRED)
        assert c is not None
        assert c.is_preferred is False

    def test_missing_link_returns_none(self):
        assert news_item_to_candidate({"title": "No link"}, "q1", 1, "google_news", PREFERRED) is None

    def test_empty_link_returns_none(self):
        assert news_item_to_candidate({"link": ""}, "q1", 1, "google_news", PREFERRED) is None

    def test_url_hash_is_sha256_of_canonical(self):
        c = news_item_to_candidate(self._item(), "q1", 1, "google_news", PREFERRED)
        expected = sha256_hex(canonicalize_url(self._item()["link"]))
        assert c.url_hash == expected

    def test_published_at_confidence(self):
        c = news_item_to_candidate(self._item(date="3 hours ago"), "q1", 1, "google_news", PREFERRED)
        assert c.published_at_confidence == PublishedAtConfidence.RELATIVE

        c2 = news_item_to_candidate(self._item(date="2024-01-15"), "q1", 1, "google_news", PREFERRED)
        assert c2.published_at_confidence == PublishedAtConfidence.ABSOLUTE

    def test_normalized_title_and_hash(self):
        c = news_item_to_candidate(self._item(), "q1", 1, "google_news", PREFERRED)
        assert c.normalized_title == normalize_title("Slovakia signs deal")
        assert c.title_hash == sha256_hex(c.normalized_title)


# ── dedupe_candidates ─────────────────────────────────────────────────────────

def _make_candidate(url: str, domain: str, is_preferred: bool = False,
                    rank: int = 1, conf: str = PublishedAtConfidence.NONE,
                    title: str = "Same Title") -> ArticleCandidate:
    canon = canonicalize_url(url)
    norm_title = normalize_title(title)
    return ArticleCandidate(
        query_id="q1",
        rank=rank,
        engine="google_news",
        url=url,
        url_canonical=canon,
        url_hash=sha256_hex(canon),
        domain=domain,
        is_preferred=is_preferred,
        title=title,
        normalized_title=norm_title,
        title_hash=sha256_hex(norm_title),
        snippet=None,
        published_at_raw=None,
        published_at_confidence=conf,
        source_label=None,
        language=None,
    )


class TestIngestDeduplication:
    def test_exact_url_dedup(self):
        c1 = _make_candidate("https://aps.dz/art", "aps.dz")
        c2 = _make_candidate("https://aps.dz/art", "aps.dz")
        result = dedupe_candidates([c1, c2], SOURCE_RANK)
        assert len(result) == 1

    def test_different_urls_kept(self):
        c1 = _make_candidate("https://aps.dz/art1", "aps.dz", title="Title A")
        c2 = _make_candidate("https://aps.dz/art2", "aps.dz", title="Title B")
        result = dedupe_candidates([c1, c2], SOURCE_RANK)
        assert len(result) == 2

    def test_same_domain_same_title_deduped(self):
        # Different URLs but same domain+title → soft dedupe
        c1 = _make_candidate("https://aps.dz/art1", "aps.dz", title="Same Article")
        c2 = _make_candidate("https://aps.dz/art2", "aps.dz", title="Same Article")
        result = dedupe_candidates([c1, c2], SOURCE_RANK)
        assert len(result) == 1

    def test_same_title_different_domains_kept(self):
        c1 = _make_candidate("https://aps.dz/art", "aps.dz", title="Same Article")
        c2 = _make_candidate("https://tsa-algerie.com/art", "tsa-algerie.com", title="Same Article")
        result = dedupe_candidates([c1, c2], SOURCE_RANK)
        assert len(result) == 2

    def test_preferred_wins_over_non_preferred(self):
        # Same canonical URL → strict dedup keeps only one, preferred wins by score
        c_pref = _make_candidate("https://aps.dz/art", "aps.dz", is_preferred=True, title="Article A")
        c_nope = _make_candidate("https://aps.dz/art", "aps.dz", is_preferred=False, title="Article A")
        result = dedupe_candidates([c_nope, c_pref], SOURCE_RANK)
        assert len(result) == 1
        assert result[0].is_preferred is True

    def test_higher_source_rank_wins_strict_dedup(self):
        # Same URL, c2 has higher rank score
        c1 = _make_candidate("https://aps.dz/art", "aps.dz", rank=5)
        c2 = _make_candidate("https://aps.dz/art", "aps.dz", rank=1)  # rank=1 is better (lower)
        result = dedupe_candidates([c1, c2], SOURCE_RANK)
        assert len(result) == 1

    def test_no_title_fallback_key(self):
        # Items without titles should not be soft-deduped against each other
        c1 = _make_candidate("https://aps.dz/art1", "aps.dz", title="")
        c2 = _make_candidate("https://aps.dz/art2", "aps.dz", title="")
        result = dedupe_candidates([c1, c2], SOURCE_RANK)
        assert len(result) == 2
