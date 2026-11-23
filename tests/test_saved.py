"""Saved searches: name a query and its filters so it can be re-run with one click."""
import pytest

from me3 import db, saved


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_save_list_and_delete(conn):
    sid = saved.save(conn, "Big PDFs", "report", {"kind": "document", "minmb": "5"})
    saved.save(conn, "Photos", "sunset", {"kind": "image"})
    rows = saved.list_all(conn)
    assert [r["name"] for r in rows] == ["Big PDFs", "Photos"]  # alphabetical
    assert rows[0]["query"] == "report" and rows[0]["filters"]["minmb"] == "5"
    assert saved.delete(conn, sid) is True
    assert [r["name"] for r in saved.list_all(conn)] == ["Photos"]
    assert saved.delete(conn, 9999) is False


def test_resaving_the_same_name_updates_it(conn):
    a = saved.save(conn, "Mine", "one", {})
    b = saved.save(conn, "Mine", "two", {"kind": "video"})
    assert a == b  # one entry, not two
    rows = saved.list_all(conn)
    assert len(rows) == 1 and rows[0]["query"] == "two" and rows[0]["filters"] == {"kind": "video"}


def test_name_is_required(conn):
    with pytest.raises(ValueError):
        saved.save(conn, "   ", "x")
