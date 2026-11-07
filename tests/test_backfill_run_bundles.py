"""Tests for backfill_run_bundles.py a ingest bundle_label (párovanie runov s priečinkami)."""
from __future__ import annotations

from backfill_run_bundles import best_bundle
from ingest_to_dz_news_reworked import bundle_label


class TestBundleLabel:
    def test_run_folder_and_file(self, tmp_path):
        b = tmp_path / "run_20261104_004531" / "news_bundle.json"
        b.parent.mkdir()
        b.write_text("{}")
        assert bundle_label(b) == "run_20261104_004531/news_bundle.json"

    def test_latest_symlink_resolved(self, tmp_path):
        run = tmp_path / "runs" / "run_20261104_004726"
        run.mkdir(parents=True)
        (run / "news_bundle.json").write_text("{}")
        latest = tmp_path / "latest"
        latest.symlink_to(run)
        assert bundle_label(latest / "news_bundle.json") == "run_20261104_004726/news_bundle.json"


class TestBestBundle:
    def test_highest_containment(self):
        bundles = {"a": ({"1", "2", "3"}, 100.0), "b": ({"1"}, 100.0)}
        assert best_bundle({"1", "2"}, 200.0, bundles) == ("a", 1.0)

    def test_tie_latest_before_ingest(self):
        # rovnaké hľadanie 3× za sebou, ingest po druhom
        bundles = {"r1": ({"1", "2"}, 1000.0), "r2": ({"1", "2"}, 2000.0), "r3": ({"1", "2"}, 4000.0)}
        assert best_bundle({"1", "2"}, 3000.0, bundles)[0] == "r2"

    def test_tie_all_after_ingest_closest(self):
        bundles = {"r1": ({"1"}, 500.0), "r2": ({"1"}, 900.0)}
        assert best_bundle({"1"}, 300.0, bundles)[0] == "r1"

    def test_no_overlap(self):
        assert best_bundle({"x"}, 0.0, {"a": ({"1"}, 0.0)}) == (None, 0.0)
