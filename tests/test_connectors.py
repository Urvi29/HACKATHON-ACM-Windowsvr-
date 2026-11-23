import json
from pathlib import Path

import pytest

from me3 import crawler, db, search, sources
from me3.connectors import gdrive
from me3.connectors.gdrive import GDriveSource

ROOT = Path(__file__).resolve().parents[1]


class _Exec:
    def __init__(self, v): self.v = v
    def execute(self): return self.v


class FakeDrive:
    """Minimal stand-in for googleapiclient's Drive v3 service."""
    FILES = [
        {"id": "f1", "name": "Documents", "mimeType": "application/vnd.google-apps.folder", "parents": ["root1"]},
        {"id": "d1", "name": "Fee receipt sem5", "mimeType": "application/vnd.google-apps.document", "parents": ["f1"], "modifiedTime": "2025-08-14T10:00:00Z"},
        {"id": "p1", "name": "trip.jpg", "mimeType": "image/jpeg", "size": "12345", "parents": ["f1"], "modifiedTime": "2025-01-02T10:00:00Z"},
        {"id": "x1", "name": "A form", "mimeType": "application/vnd.google-apps.form", "parents": ["f1"]},
    ]
    def files(self): return self
    def list(self, **kw): return _Exec({"files": self.FILES})
    def about(self): return self
    def get(self, **kw): return _Exec({"user": {}})


def test_gdrive_walk_paths_and_types():
    src = GDriveSource(1, service=FakeDrive())
    ents = {e.name: e for e in src.walk()}
    assert set(ents) == {"Fee receipt sem5.gdoc", "trip.jpg"}  # folder + unsupported form skipped
    assert ents["trip.jpg"].path == "/Documents/trip.jpg#p1" and ents["trip.jpg"].size == 12345
    assert src._ids[ents["Fee receipt sem5.gdoc"].path][1].endswith("document")


def test_gdrive_indexes_through_crawler(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    sid = sources.add_source(conn, "gdrive", "/", "Drive")
    src = GDriveSource(sid, service=FakeDrive())
    assert crawler.scan(conn, src)["new"] == 2
    assert search.search(conn, "fee receipt", semantic=False)["total"] == 1  # filename match; .gdoc is a document


def _write_client(p, kind="installed", project="aquaflare"):
    p.write_text(json.dumps({kind: {"client_id": "x.apps.googleusercontent.com", "project_id": project}}))
    return p


def test_client_info_reads_desktop_client(tmp_path):
    info = gdrive.client_info(_write_client(tmp_path / "c.json"))
    assert info["kind"] == "Desktop" and info["project_id"] == "aquaflare"


def test_find_client_secret_uses_env_path(tmp_path, monkeypatch):
    f = _write_client(tmp_path / "client_secret_1.json")
    monkeypatch.setenv("ME3_GOOGLE_CLIENT", str(f))
    assert gdrive.find_client_secret() == f.resolve()


def test_find_client_secret_scans_downloads(tmp_path, monkeypatch):
    monkeypatch.delenv("ME3_GOOGLE_CLIENT", raising=False)
    monkeypatch.setattr(gdrive, "CREDS", tmp_path / "credentials.json")  # project root has none
    monkeypatch.setattr(gdrive, "SEARCH_DIRS", [tmp_path / "Downloads"])
    (tmp_path / "Downloads").mkdir()
    f = _write_client(tmp_path / "Downloads" / "client_secret_9.json")
    assert gdrive.find_client_secret() == f.resolve()


def test_find_client_secret_rejects_web_client(tmp_path, monkeypatch):
    monkeypatch.delenv("ME3_GOOGLE_CLIENT", raising=False)
    monkeypatch.setattr(gdrive, "CREDS", tmp_path / "credentials.json")
    monkeypatch.setattr(gdrive, "SEARCH_DIRS", [tmp_path])
    _write_client(tmp_path / "client_secret_web.json", kind="web")
    with pytest.raises(RuntimeError, match="Desktop"):
        gdrive.find_client_secret()
