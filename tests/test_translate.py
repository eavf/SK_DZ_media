"""
Tests for translate.py — NVIDIA backend (chunking, request, retry). HTTP calls are mocked.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import translate
from translate import _split_chunks, translate_ar_fr


def _resp(status=200, content="traduction", finish="stop"):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    if status >= 400:
        r.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return r


@pytest.fixture
def nvidia(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.delenv("NVIDIA_TRANSLATE_MODEL", raising=False)
    monkeypatch.setattr(translate, "get_translator_backend", lambda: "nvidia")
    monkeypatch.setattr(translate.time, "sleep", lambda s: None)
    post = MagicMock(return_value=_resp())
    monkeypatch.setattr(translate.requests, "post", post)
    return post


class TestSplitChunks:
    def test_short_text_single_chunk(self):
        assert _split_chunks("ligne 1\nligne 2", 100) == ["ligne 1\nligne 2"]

    def test_splits_on_lines(self):
        assert _split_chunks("a" * 10 + "\n" + "b" * 5, 12) == ["a" * 10, "b" * 5]

    def test_long_line_split_on_sentences(self):
        chunks = _split_chunks(("xxxxx. " * 10).strip(), 20)
        assert all(len(c) <= 20 for c in chunks)
        assert "".join(chunks).replace(" ", "") == ("xxxxx. " * 10).replace(" ", "")

    def test_no_text_lost(self):
        text = "\n".join(f"odsek {i} " + "slovo " * 50 for i in range(20))
        chunks = _split_chunks(text, 500)
        assert all(len(c) <= 500 for c in chunks)
        assert "\n".join(chunks).split() == text.split()


class TestNvidiaBackend:
    def test_request_payload(self, nvidia):
        assert translate_ar_fr(["نص"]) == ["traduction"]
        payload = nvidia.call_args.kwargs["json"]
        assert payload["model"] == translate.NVIDIA_DEFAULT_TRANSLATE_MODEL
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert payload["messages"][1]["content"] == "نص"
        assert "French" in payload["messages"][0]["content"]
        assert nvidia.call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"

    def test_model_from_env(self, nvidia, monkeypatch):
        monkeypatch.setenv("NVIDIA_TRANSLATE_MODEL", "other/model")
        translate_ar_fr(["نص"])
        assert nvidia.call_args.kwargs["json"]["model"] == "other/model"

    def test_long_text_translated_in_chunks(self, nvidia, monkeypatch):
        monkeypatch.setattr(translate, "NVIDIA_CHUNK_CHARS", 10)
        out = translate_ar_fr(["aaaaaaaa\nbbbbbbbb"])
        assert nvidia.call_count == 2
        assert out == ["traduction\ntraduction"]

    def test_empty_text_not_sent(self, nvidia):
        assert translate_ar_fr(["", "نص"]) == ["", "traduction"]
        assert nvidia.call_count == 1

    def test_retry_on_429(self, nvidia):
        nvidia.side_effect = [_resp(429), _resp(content="ok")]
        assert translate_ar_fr(["نص"]) == ["ok"]
        assert nvidia.call_count == 2

    def test_truncated_output_raises(self, nvidia):
        nvidia.return_value = _resp(finish="length")
        with pytest.raises(RuntimeError, match="useknutý"):
            translate_ar_fr(["نص"])

    def test_missing_key_raises(self, nvidia, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        with pytest.raises(ValueError):
            translate_ar_fr(["نص"])
