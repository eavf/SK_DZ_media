"""extract_bulk.save_result — nečitateľné PDF sa vyradia (soft delete + relevance=0)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _result(fetch_error):
    return SimpleNamespace(final_url=None, extraction_ok=False, fetch_error=fetch_error, http_status=200,
                           no_slovak_context=False, is_commercial=False, published_at_real=None)


@pytest.fixture
def eb(monkeypatch):
    import extract_bulk
    monkeypatch.setattr(extract_bulk, "mark_url_duplicate", lambda conn, article_id: None)
    return extract_bulk


def _conn(rowcount):
    conn = MagicMock()
    conn.execute.return_value.rowcount = rowcount
    return conn


def _sqls(conn):
    return [str(c.args[0]) for c in conn.execute.call_args_list]


@pytest.mark.parametrize("err,note", [
    ("PDF_EXTRACTION_EMPTY", "auto: PDF bez textu (sken)"),
    ("PDF_LEGACY_ENCODING", "auto: PDF bez textu (staré kódovanie)"),
])
def test_unreadable_pdf_soft_deleted(eb, err, note):
    conn = _conn(1)
    assert eb.save_result(conn, 7, _result(err)) == "pdf_unreadable"
    last = conn.execute.call_args_list[-1]
    assert "deleted_at = NOW()" in str(last.args[0]) and "relevance IS NULL" in str(last.args[0])
    assert last.args[1] == {"note": note, "id": 7}


def test_unreadable_pdf_manually_labeled_kept(eb):
    conn = _conn(0)  # relevance už nastavená → UPDATE nič nezmení
    assert eb.save_result(conn, 7, _result("PDF_EXTRACTION_EMPTY")) == "failed"


def test_other_error_not_deleted(eb):
    conn = _conn(1)
    assert eb.save_result(conn, 7, _result("EXTRACTION_EMPTY")) == "failed"
    assert not any("deleted_at" in q for q in _sqls(conn))
