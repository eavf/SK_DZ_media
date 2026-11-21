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
                                               "duplicate": 2, "ai_sport": 4, "pdf_unreadable": 0, "bad_url": 0,
                                               "ai_relevant": 0, "ai_irrelevant": 0}

    def test_old_format_without_new_keys(self):
        from app import _parse_extract_summary
        assert _parse_extract_summary("Hotovo: ok=1, soft_deleted=0, commercial=0, failed=0") == {
            "ok": 1, "soft_deleted": 0, "commercial": 0, "failed": 0, "duplicate": 0, "ai_sport": 0, "pdf_unreadable": 0, "bad_url": 0,
            "ai_relevant": 0, "ai_irrelevant": 0}

    def test_ai_relevance_counts(self):
        from app import _parse_extract_summary
        s = _parse_extract_summary("Hotovo: ok=3, failed=0, ai_relevant=2, ai_irrelevant=1")
        assert s["ai_relevant"] == 2 and s["ai_irrelevant"] == 1

    def test_no_summary(self):
        from app import _parse_extract_summary
        assert _parse_extract_summary("Žiadne články na extrakciu.") is None


class TestSerpapiErrors:
    """scheduler.serpapi_errors — chyby SerpAPI z bundla sa dostanú do e-mailu."""

    def test_errors_found(self, tmp_path):
        import json
        from scheduler import serpapi_errors
        p = tmp_path / "news_bundle.json"
        p.write_text(json.dumps({"responses_raw": {
            "q1": {"error": "site: not supported", "news_results": []},
            "q2": {"news_results": [{"link": "x"}]},
            "q3": None,
        }}), encoding="utf-8")
        assert serpapi_errors(p) == ["Q1: site: not supported"]

    def test_no_errors(self, tmp_path):
        import json
        from scheduler import serpapi_errors
        p = tmp_path / "news_bundle.json"
        p.write_text(json.dumps({"responses_raw": {"q1": {"news_results": []}}}), encoding="utf-8")
        assert serpapi_errors(p) == []

    def test_missing_bundle_reported(self, tmp_path):
        from scheduler import serpapi_errors
        errs = serpapi_errors(tmp_path / "missing.json")
        assert len(errs) == 1 and "bundle sa nedá načítať" in errs[0]


class TestExtractStateSql:
    """„Neúspešná extrakcia“ na úvodnej stránke a filter /browse?state=failed používajú tú istú podmienku."""

    def test_failed_excludes_irrelevant(self):
        from app import _extract_state_sql
        sql = _extract_state_sql("failed")
        assert "(a.relevance IS NULL OR a.relevance <> 0)" in sql
        assert "a.content_text IS NULL" in sql

    def test_pending_keeps_irrelevant(self):
        from app import _extract_state_sql
        assert "relevance" not in _extract_state_sql("pending")

    def test_browse_filter_uses_same_condition(self):
        from app import build_filters, _extract_state_sql
        where, _ = build_filters(0, "all", False, "all", False, False, extract_state="failed")
        assert _extract_state_sql("failed") in where
        assert "a.deleted_at IS NULL" in where and "s.is_avoided = 0" in where


class TestEmailBreakdown:
    """Rozpis v dennom maili — prečo nové/znovu videné články v maili nie sú."""

    def test_buckets(self):
        from scheduler import _email_bucket as b
        assert b("sport", 0, "auto: sport (AI) – x", True, True, False) == "sport"
        assert b(None, 0, "auto: duplikát #12", True, True, True) == "duplicate"
        assert b(None, 0, "auto: komerčné (promo)", None, True, True) == "commercial"
        assert b(None, 0, "auto: PDF bez textu (sken)", True, False, False) == "unreadable"
        assert b(None, 0, "auto: URL (/tag/)", None, True, False) == "unreadable"
        assert b(None, 0, "ručne", None, True, True) == "irrelevant"
        assert b(None, None, None, True, True, False) == "deleted"
        assert b(None, None, None, False, False, True) == "no_text_sk"
        assert b(None, None, None, False, False, False) == "no_text"
        assert b("other", 1, None, False, True, True) == "shown"

    def test_format_order_and_zeroes(self):
        from scheduler import format_breakdown
        counts = {"shown": 2, "no_text": 2, "sport": 3, "deleted": 7, "commercial": 0}
        assert format_breakdown(counts) == (
            "šport (AI) 3, bez zmienky o Slovensku (zmazané) 7, text sa nestiahol, Slovensko nespomína 2")
        assert format_breakdown({"shown": 4}) == ""

    def test_email_body(self, monkeypatch):
        from unittest.mock import MagicMock
        import scheduler
        for k, v in {"smtp_host": "smtp.x", "notify_to": "a@b", "smtp_user": "u", "smtp_pass": "p",
                     "smtp_port": 587}.items():
            monkeypatch.setattr(scheduler.s, k, v, raising=False)
        smtp = MagicMock()
        monkeypatch.setattr(scheduler.smtplib, "SMTP", smtp)
        arts = [{"title": "t", "snippet": "", "url": "u", "lang": "fr"}] * 2
        scheduler.send_notification(14, 17, None, arts, [{"title": "r", "url": "u", "lang": "fr"}] * 4, [], [],
                                    ({"shown": 2, "deleted": 7, "sport": 3, "no_text": 2},
                                     {"shown": 4, "deleted": 10, "irrelevant": 3}))
        msg = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        body = msg.get_content()
        assert "Nové články: 14, z toho v maili: 2" in body
        assert "vyradené filtrami: šport (AI) 3, bez zmienky o Slovensku (zmazané) 7, " \
               "text sa nestiahol, Slovensko nespomína 2" in body
        assert "Znovu videné: 17, z toho v maili: 4" in body
        assert "ostatné: označené ako nerelevantné 3, bez zmienky o Slovensku (zmazané) 10" in body
        assert msg["Subject"] == "DZ News – 2 na posúdenie (14 nových / 17 znovu videných)"
