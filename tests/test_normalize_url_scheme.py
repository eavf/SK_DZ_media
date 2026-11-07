"""Tests for normalize_url_scheme.py — plán prepočtu http → https (bez DB)."""
from __future__ import annotations

from extract_articles_reworked import canonicalize_url, sha256_hex
from normalize_url_scheme import build_plan, to_https


def _row(id_, url, final=None, active=True):
    return {"id": id_, "url_canonical": url, "final_url_canonical": final, "active": active}


class TestToHttps:
    def test_http(self):
        assert to_https("http://aps.dz/x") == "https://aps.dz/x"

    def test_https_unchanged(self):
        assert to_https("https://aps.dz/x") is None

    def test_none(self):
        assert to_https(None) is None

    def test_matches_new_canonicalize(self):
        # prepočet musí dať presne to, čo nový canonicalize_url pre tú istú http URL
        old = "http://news.radioalgerie.dz/ar/node/93990"
        assert to_https(old) == canonicalize_url(old)


class TestBuildPlan:
    def test_updates_url_and_final(self):
        plan = build_plan([_row(1, "http://a.dz/x", "http://a.dz/x")], {})
        h = sha256_hex("https://a.dz/x")
        assert plan.url_updates == [{"id": 1, "canon": "https://a.dz/x", "hash": h}]
        assert plan.final_updates == [{"id": 1, "canon": "https://a.dz/x", "hash": h}]
        assert plan.collisions == []

    def test_only_final_is_http(self):
        plan = build_plan([_row(1, "https://www.google.com/goto?url=X", "http://a.dz/x")], {})
        assert plan.url_updates == []
        assert len(plan.final_updates) == 1

    def test_collision_with_deleted_keeps_hash(self):
        # 3200 (http) vs 3167 (https) — 3200 je už zmazaný duplikát
        existing = {sha256_hex("https://r.dz/n/1"): (3167, True)}
        plan = build_plan([_row(3200, "http://r.dz/n/1", active=False)], existing)
        assert plan.url_updates == []
        assert plan.collisions == [(3200, 3167, False)]

    def test_collision_both_active_flagged(self):
        existing = {sha256_hex("https://entv.dz/x"): (1095, True)}
        plan = build_plan([_row(1156, "http://entv.dz/x", "http://entv.dz/x")], existing)
        assert plan.collisions == [(1156, 1095, True)]
        # final_url nemá UNIQUE → prepočíta sa aj pri kolízii
        assert len(plan.final_updates) == 1
