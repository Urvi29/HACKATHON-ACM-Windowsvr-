import os

from me3 import cleanup, crawler, db, sources
from me3.sources import Entry


def test_small_local_duplicates_are_found(tmp_path):
    d = tmp_path / "files"
    d.mkdir()
    (d / "a.txt").write_text("hi")           # 2 bytes: used to be skipped (< 1 KB)
    (d / "b.txt").write_text("hi")
    (d / "c.txt").write_text("ho")           # same size, different content
    conn = db.connect(tmp_path / "t.db")
    sid = sources.add_source(conn, "local", str(d))
    crawler.scan(conn, sources.load(conn, sid))
    groups = cleanup.find_duplicates(conn, limit_groups=100)["groups"]
    assert [sorted(os.path.basename(f["path"]) for f in g["files"]) for g in groups] == [["a.txt", "b.txt"]]


def test_drive_duplicates_use_md5(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    sid = sources.add_source(conn, "gdrive", "/", "Drive")
    for name, md5 in (("x.pdf", "aa"), ("x (1).pdf", "aa"), ("y.pdf", "bb")):
        crawler.upsert(conn, sid, Entry(f"/{name}#id-{name}", name, 100, 1.0, hash=f"md5:{md5}"))
    conn.commit()
    groups = cleanup.find_duplicates(conn, limit_groups=100)["groups"]
    assert len(groups) == 1 and groups[0]["remote"] and len(groups[0]["files"]) == 2
