"""Testy AI návrhu relevancie (ai_relevance.py) — bez volania API a bez DB."""
from datetime import datetime
from types import SimpleNamespace

import pytest

import ai_relevance as ar


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    monkeypatch.delenv("AI_RELEVANCE_ENABLED", raising=False)
    monkeypatch.delenv("AI_RELEVANCE_SKIP_DOMAINS", raising=False)
    monkeypatch.delenv("AI_RELEVANCE_MODEL", raising=False)
    monkeypatch.setattr(ar, "_consecutive_failures", 0)


def _row(**kw):
    base = {"id": 1, "title": "t", "snippet": "s", "text": "La Slovaquie et l'Algérie…", "relevance": None,
            "deleted_at": None, "ai_relevance_at": None, "domain": "aps.dz"}
    base.update(kw)
    return base


class TestSettings:
    def test_skip_domains_default_and_subdomains(self):
        assert ar.is_skipped_domain("mfa.gov.dz")
        assert ar.is_skipped_domain("embbratislava.mfa.gov.dz")
        assert ar.is_skipped_domain("www.mfa.gov.dz")
        assert not ar.is_skipped_domain("aps.dz")
        assert not ar.is_skipped_domain("notmfa.gov.dz")

    def test_skip_domains_custom_and_empty(self, monkeypatch):
        monkeypatch.setenv("AI_RELEVANCE_SKIP_DOMAINS", "aps.dz, .elwatan.com")
        assert ar.is_skipped_domain("aps.dz") and ar.is_skipped_domain("www.elwatan.com")
        assert not ar.is_skipped_domain("mfa.gov.dz")
        monkeypatch.setenv("AI_RELEVANCE_SKIP_DOMAINS", "")
        assert not ar.is_skipped_domain("mfa.gov.dz")

    def test_enabled(self, monkeypatch):
        assert ar.is_enabled()
        monkeypatch.setenv("AI_RELEVANCE_ENABLED", "0")
        assert not ar.is_enabled()
        monkeypatch.setenv("AI_RELEVANCE_ENABLED", "1")
        monkeypatch.setenv("NVIDIA_API_KEY", "")
        assert not ar.is_enabled()

    def test_default_model(self, monkeypatch):
        assert ar._model() == ar.DEFAULT_MODEL
        monkeypatch.setenv("AI_RELEVANCE_MODEL", "x/y")
        assert ar._model() == "x/y"


class TestEligible:
    def test_ok(self):
        assert ar._eligible(_row())

    @pytest.mark.parametrize("kw", [
        {"relevance": 0}, {"relevance": 1}, {"deleted_at": datetime.now()}, {"ai_relevance_at": datetime.now()},
        {"text": ""}, {"text": None}, {"domain": "embbratislava.mfa.gov.dz"},
    ])
    def test_skipped(self, kw):
        assert not ar._eligible(_row(**kw))

    def test_none(self):
        assert not ar._eligible(None)


class TestParse:
    def test_valid(self):
        assert ar.parse_answer('x {"relevant": true, "confidence": 85, "reason": "visite"} y') == (True, 85, "visite")

    def test_confidence_clamped_and_optional(self):
        assert ar.parse_answer('{"relevant": false, "confidence": 140, "reason": ""}')[1] == 100
        assert ar.parse_answer('{"relevant": false}') == (False, None, "")

    @pytest.mark.parametrize("ans", ["", "no json", '{"relevant": "yes"}'])
    def test_invalid(self, ans):
        with pytest.raises(ValueError):
            ar.parse_answer(ans)


class TestAssess:
    def _fake_post(self, monkeypatch, status=200, content='{"relevant": true, "confidence": 90, "reason": "r"}'):
        sent = {}

        def post(url, headers, json, timeout):
            sent.update(url=url, json=json)
            return SimpleNamespace(
                status_code=status,
                raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError(f"HTTP {status}")) if status >= 400 else None,
                json=lambda: {"choices": [{"message": {"content": content}}]},
            )

        monkeypatch.setattr(ar.requests, "post", post)
        return sent

    def test_request_shape(self, monkeypatch):
        sent = self._fake_post(monkeypatch)
        assert ar.assess(_row()) == (True, 90, "r")
        body = sent["json"]
        assert sent["url"].endswith("/chat/completions")
        assert body["model"] == ar.DEFAULT_MODEL and body["temperature"] == 0
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["messages"][0]["content"] == ar.SYSTEM_PROMPT
        assert "Article text" in body["messages"][1]["content"]

    def test_rate_limit(self, monkeypatch):
        self._fake_post(monkeypatch, status=429)
        with pytest.raises(ar.RateLimited):
            ar.assess(_row())

    def test_consecutive_failures_stop(self, monkeypatch):
        self._fake_post(monkeypatch, content="garbage")
        for _ in range(ar.MAX_CONSECUTIVE_FAILURES - 1):
            with pytest.raises(ValueError):
                ar._assess_counted(_row())
        with pytest.raises(ar.ApiUnavailable):
            ar._assess_counted(_row())


class FakeConn:
    """Minimálna náhrada DB spojenia: SELECT článku vráti row, UPDATE si zapamätá."""

    def __init__(self, row, ready=True):
        self.row, self.ready, self.updates = row, ready, []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "information_schema" in sql:
            return SimpleNamespace(scalar=lambda: 1 if self.ready else 0)
        if sql.lstrip().startswith("UPDATE"):
            self.updates.append(params)
            return None
        return SimpleNamespace(mappings=lambda: SimpleNamespace(fetchone=lambda: self.row))


class FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    def begin(self):
        conn = self.conn

        class Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *a):
                return False
        return Ctx()


class TestAssessAndSave:
    def _setup(self, monkeypatch, row, ready=True, result=(False, 95, "šport")):
        conn = FakeConn(row, ready)
        monkeypatch.setattr(ar, "get_db_engine", lambda: FakeEngine(conn))
        monkeypatch.setattr(ar, "_columns_ready", None)
        calls = []
        monkeypatch.setattr(ar, "assess", lambda r: calls.append(r) or result)
        return conn, calls

    def test_saves_suggestion_only(self, monkeypatch):
        conn, calls = self._setup(monkeypatch, _row())
        assert ar.assess_and_save(1) == 0
        assert len(calls) == 1
        assert conn.updates == [{"id": 1, "rel": 0, "conf": 95, "reason": "šport"}]

    def test_manual_label_not_assessed(self, monkeypatch):
        conn, calls = self._setup(monkeypatch, _row(relevance=1))
        assert ar.assess_and_save(1) is None and not calls and not conn.updates

    def test_without_migration_skipped(self, monkeypatch):
        conn, calls = self._setup(monkeypatch, _row(), ready=False)
        assert ar.assess_and_save(1) is None and not calls

    def test_api_error_not_saved(self, monkeypatch):
        conn, _ = self._setup(monkeypatch, _row())
        monkeypatch.setattr(ar, "assess", lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
        assert ar.assess_and_save(1) is None and not conn.updates

    def test_columns_ready_rechecks_until_true(self, monkeypatch):
        monkeypatch.setattr(ar, "_columns_ready", None)
        conn = FakeConn(None, ready=False)
        assert not ar.columns_ready(conn)
        conn.ready = True                       # migrácia spustená za behu aplikácie
        assert ar.columns_ready(conn)
        conn.ready = False                      # kladný výsledok ostáva v pamäti
        assert ar.columns_ready(conn)


def test_batch_env(monkeypatch):
    assert ar.ai_relevance_batch() == 20
    monkeypatch.setenv("AI_RELEVANCE_BATCH", "5")
    assert ar.ai_relevance_batch() == 5
    monkeypatch.setenv("AI_RELEVANCE_BATCH", "x")
    assert ar.ai_relevance_batch() == 20
