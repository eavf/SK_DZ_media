"""
Tests for classify_ai.py — AI response parsing and enable switch.
HTTP calls to NVIDIA API are mocked; DB functions are not tested here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import classify_ai
from classify_ai import ApiUnavailable, ClassificationStopped, RateLimited, _call_ai, classify_sport, is_enabled


def _mock_response(monkeypatch, status=200, content=""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    if status >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    post = MagicMock(return_value=resp)
    monkeypatch.setattr(classify_ai.requests, "post", post)
    return post


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("AI_CLASSIFY_ENABLED", "1")
    monkeypatch.setattr(classify_ai, "_consecutive_failures", 0)


class TestIsEnabled:
    def test_enabled_with_key(self):
        assert is_enabled() is True

    def test_disabled_without_key(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        assert is_enabled() is False

    def test_disabled_by_flag(self, monkeypatch):
        monkeypatch.setenv("AI_CLASSIFY_ENABLED", "0")
        assert is_enabled() is False


class TestClassifySport:
    def test_sport_true(self, monkeypatch):
        _mock_response(monkeypatch, content='{"sport": true, "reason": "football match report"}')
        assert classify_sport("Algérie - Slovaquie 2-1", "Match amical...") == (True, "football match report")

    def test_sport_false(self, monkeypatch):
        _mock_response(monkeypatch, content='{"sport": false, "reason": "diplomatic visit"}')
        assert classify_sport("Visite du ministre", "...") == (False, "diplomatic visit")

    def test_json_wrapped_in_text(self, monkeypatch):
        _mock_response(monkeypatch, content='Here is the answer:\n```json\n{"sport": false, "reason": "economy"}\n```')
        assert classify_sport("t", "c") == (False, "economy")

    def test_no_json_raises(self, monkeypatch):
        _mock_response(monkeypatch, content="I think it is sport.")
        with pytest.raises(ValueError):
            classify_sport("t", "c")

    def test_non_bool_raises(self, monkeypatch):
        _mock_response(monkeypatch, content='{"sport": "yes"}')
        with pytest.raises(ValueError):
            classify_sport("t", "c")

    def test_rate_limited(self, monkeypatch):
        _mock_response(monkeypatch, status=429)
        with pytest.raises(RateLimited):
            classify_sport("t", "c")

    def test_text_is_truncated(self, monkeypatch):
        post = _mock_response(monkeypatch, content='{"sport": false, "reason": ""}')
        classify_sport("t", "x" * 10_000)
        user_msg = post.call_args.kwargs["json"]["messages"][1]["content"]
        assert len(user_msg) < classify_ai.MAX_TEXT_CHARS + 100


class TestConsecutiveFailures:
    def test_stops_after_max_failures(self, monkeypatch):
        _mock_response(monkeypatch, status=500)
        for _ in range(classify_ai.MAX_CONSECUTIVE_FAILURES - 1):
            with pytest.raises(Exception) as exc:
                _call_ai("t", "c")
            assert not isinstance(exc.value, ClassificationStopped)
        with pytest.raises(ApiUnavailable):
            _call_ai("t", "c")

    def test_success_resets_counter(self, monkeypatch):
        _mock_response(monkeypatch, status=500)
        for _ in range(classify_ai.MAX_CONSECUTIVE_FAILURES - 1):
            with pytest.raises(Exception):
                _call_ai("t", "c")
        _mock_response(monkeypatch, content='{"sport": false, "reason": ""}')
        assert _call_ai("t", "c") == (False, "")
        assert classify_ai._consecutive_failures == 0

    def test_rate_limit_is_classification_stopped(self, monkeypatch):
        _mock_response(monkeypatch, status=429)
        with pytest.raises(ClassificationStopped):
            _call_ai("t", "c")
