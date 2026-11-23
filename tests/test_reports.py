"""Scheduled cleanup reports: non-destructive summaries kept as history."""
import time

import pytest

from me3 import crawler, db, reports, sources


@pytest.fixture
def conn(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.txt").write_text("same contents here")
    (root / "b.txt").write_text("same contents here")
    conn = db.connect(tmp_path / "t.db")
    sid = sources.add_source(conn, "local", str(root))
    crawler.scan(conn, sources.load(conn, sid))
    return conn


def test_report_counts_duplicates_and_is_stored(conn):
    assert reports.latest(conn) is None
    d = reports.generate(conn)["data"]
    assert d["files"] == 2
    assert d["duplicate_groups"] == 1
    assert d["duplicate_wasted_bytes"] == len("same contents here")
    assert reports.latest(conn)["data"]["files"] == 2
    assert len(reports.history(conn)) == 1


def test_due_follows_the_interval(conn, monkeypatch):
    monkeypatch.setattr(reports, "INTERVAL_H", 1.0)
    assert reports.due(conn) is True  # nothing written yet
    reports.generate(conn)
    assert reports.due(conn) is False  # just written
    conn.execute("UPDATE reports SET created=? WHERE kind='cleanup'", (time.time() - 7200,))
    conn.commit()
    assert reports.due(conn) is True  # older than the interval


def test_history_is_trimmed_to_the_keep_limit(conn):
    for i in range(reports.KEEP + 3):
        conn.execute("INSERT INTO reports(kind, created, data) VALUES('cleanup', ?, '{}')", (time.time() + i,))
    conn.commit()
    reports.generate(conn)
    assert len(reports.history(conn, reports.KEEP + 10)) == reports.KEEP
