"""Testy skriptu na meranie AI relevancie (bez volania API)."""
import pytest

import eval_ai_relevance as e

CSV = ("﻿id;domain;published;url;title;snippet;fetch_error;relevance;note\n"
       "1;aps.dz;;u;A;s;;1;\n"
       "2;aps.dz;;u;B;s;;0;ručne\n"
       "3;aps.dz;;u;C;s;;0;auto: komerčné\n"
       "4;aps.dz;;u;D;s;;;\n"
       "5;aps.dz;;u;E;s;;1;\n"
       "6;aps.dz;;u;F;s;;1;\n")


def test_load_labeled_only_manual(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text(CSV, encoding="utf-8")
    rows = e.load_labeled(p)
    assert [r["id"] for r in rows] == ["1", "2", "5", "6"]
    assert rows[0]["relevance"] == 1 and rows[1]["relevance"] == 0


def test_balanced_sample():
    rows = [{"id": str(i), "relevance": 1} for i in range(10)] + [{"id": f"n{i}", "relevance": 0} for i in range(2)]
    s = e.balanced_sample(rows, 6)
    assert len(s) == 6 and sum(r["relevance"] == 0 for r in s) == 2   # nerelevantných je málo → doplní relevantné
    assert e.balanced_sample(rows, None) == rows


def test_parse_answer():
    assert e.parse_answer('ok {"relevant": true, "confidence": 80, "reason": "visit"}') == (True, 80, "visit")
    assert e.parse_answer('{"relevant": false, "reason": "x"}') == (False, None, "x")
    with pytest.raises(ValueError):
        e.parse_answer("")
    with pytest.raises(ValueError):
        e.parse_answer('{"relevant": "yes"}')


def test_report_counts():
    res = [{"id": "1", "domain": "d", "title": "t", "manual": 1, "ai": 1, "confidence": 90, "reason": ""},
           {"id": "2", "domain": "d", "title": "t", "manual": 1, "ai": 0, "confidence": 60, "reason": "r"},
           {"id": "3", "domain": "d", "title": "t", "manual": 0, "ai": 0, "confidence": 90, "reason": ""}]
    out = e.report(res)
    assert "Zhoda celkom: 67 %" in out and "#2" in out and "#1 " not in out


def test_text_excerpt_keeps_late_mention():
    text = "A" * 5000 + " la Slovaquie a voté contre " + "B" * 5000
    ex = e.text_excerpt(text)
    assert ex.startswith("A" * 100) and "Slovaquie a voté contre" in ex and len(ex) < 3000


def test_user_msg_with_text_column():
    msg = e.build_user_msg({"domain": "d", "title": "t", "snippet": "s", "text": ""})
    assert "empty" in msg
    msg = e.build_user_msg({"domain": "d", "title": "t", "snippet": "s", "text": "Bratislava"})
    assert "Article text (10 chars)" in msg
    assert "Article text" not in e.build_user_msg({"domain": "d", "title": "t", "snippet": "s"})


def test_apply_overrides():
    rows = [{"id": "1", "relevance": 1}, {"id": "2", "relevance": 1}]
    assert [r["relevance"] for r in e.apply_overrides(rows, ["2=0"])] == [1, 0]
    with pytest.raises(SystemExit):
        e.apply_overrides(rows, ["2=x"])
