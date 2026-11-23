"""Move-to-trash: freedesktop.org Trash on Linux/BSD, the Recycle Bin on Windows (both restorable from the file manager)."""
import sqlite3
import sys
from urllib.parse import unquote

import pytest

from me3 import cleanup

is_windows = sys.platform.startswith("win")
posix_only = pytest.mark.skipif(is_windows, reason="freedesktop.org Trash is Linux/BSD only; Windows uses the Recycle Bin")
windows_only = pytest.mark.skipif(not is_windows, reason="Recycle Bin is Windows only")


@windows_only
def test_send_to_trash_uses_recycle_bin(tmp_path):
    src = tmp_path / "to_delete.txt"
    src.write_text("bye")
    cleanup.send_to_trash(str(src))
    assert not src.exists()  # moved to the Recycle Bin (restorable from Explorer)


@posix_only
def test_send_to_trash_writes_trashinfo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    src = tmp_path / "docs" / "my report.txt"
    src.parent.mkdir()
    src.write_text("hello")
    dest = cleanup.send_to_trash(str(src))
    trash = tmp_path / "share" / "Trash"
    assert not src.exists()
    assert dest == str(trash / "files" / "my report.txt")
    info = (trash / "info" / "my report.txt.trashinfo").read_text()
    assert info.startswith("[Trash Info]\n")
    assert unquote(info.split("Path=")[1].split("\n")[0]) == str(src)
    assert "DeletionDate=" in info


@posix_only
def test_same_name_gets_unique_trash_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "x.jpg").write_bytes(d.encode())
        cleanup.send_to_trash(str(tmp_path / d / "x.jpg"))
    files = sorted(p.name for p in (tmp_path / "share" / "Trash" / "files").iterdir())
    assert files == ["x.1.jpg", "x.jpg"]
    assert (tmp_path / "share" / "Trash" / "info" / "x.1.jpg.trashinfo").exists()


@posix_only
def test_trash_files_drops_from_index(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setattr(cleanup.crawler, "remove_ids", lambda conn, ids: conn.executemany("DELETE FROM files WHERE id=?", [(i,) for i in ids]))
    f = tmp_path / "dup.txt"
    f.write_text("x")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE sources(id INTEGER PRIMARY KEY, kind TEXT); CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT, source_id INT);"
                       "INSERT INTO sources VALUES (1,'local'),(2,'gdrive');")
    conn.execute("INSERT INTO files VALUES (1,?,1),(2,'drive://x',2),(3,?,1)", (str(f), str(tmp_path / "gone.txt")))
    r = cleanup.trash_files(conn, [1, 2, 3])
    assert r["moved"] == 1 and len(r["errors"]) == 2
    assert not f.exists() and (tmp_path / "share" / "Trash" / "files" / "dup.txt").exists()
    assert [x[0] for x in conn.execute("SELECT id FROM files")] == [2, 3]
