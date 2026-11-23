"""Storage sources behind one interface: walk / stat / fetch. Local here; Google Drive in connectors/."""
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass

from . import db
from .util import IGNORE_DIRS_LOWER, PSEUDO_FS


@dataclass
class Entry:
    path: str
    name: str
    size: int
    mtime: float
    is_dir: bool = False
    hash: str | None = None  # content checksum when the source provides one for free (Drive: 'md5:<hex>')


class LocalSource:
    kind = "local"
    watchable = True

    def __init__(self, id: int, root: str, name: str = "", config: dict | None = None):
        self.id, self.root, self.name, self.config = id, os.path.abspath(root), name or root, config or {}

    def _skip_dir(self, path: str, name: str) -> bool:
        if name.lower() in IGNORE_DIRS_LOWER or name.startswith(".") or path in PSEUDO_FS:  # hidden dirs hold app state, not user files
            return True
        try:
            if os.stat(path).st_file_attributes & 0x2:  # Windows FILE_ATTRIBUTE_HIDDEN (AppData, etc.)
                return True
        except (OSError, AttributeError):
            pass
        if path in (str(db.ROOT / "libs"), str(db.ROOT / "models"), str(db.ROOT / "data"), str(db.ROOT / ".pycache"), str(db.ROOT / ".uv-cache")):
            return True  # this project's bundled libraries, model weights, index and caches
        try:
            return os.path.samefile(path, db.DB_PATH.parent)
        except OSError:
            return False

    def walk(self, start: str | None = None):
        if start and self._skip_dir(os.path.abspath(start), os.path.basename(start)):
            return
        stack = [start or self.root]
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not self._skip_dir(e.path, e.name):
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            yield Entry(e.path, e.name, st.st_size, st.st_mtime)
                    except OSError:
                        continue

    def stat(self, path: str) -> Entry | None:
        try:
            st = os.lstat(path)
        except OSError:
            return None
        import stat as _s
        if _s.S_ISDIR(st.st_mode):
            return Entry(path, os.path.basename(path), 0, st.st_mtime, True)
        if not _s.S_ISREG(st.st_mode):
            return None
        return Entry(path, os.path.basename(path), st.st_size, st.st_mtime)

    @contextmanager
    def fetch(self, path: str):
        """Yield a local filesystem path for the file (remote sources download to a temp file)."""
        yield path


def add_source(conn, kind: str, root: str, name: str = "", config: dict | None = None) -> int:
    row = conn.execute("SELECT id FROM sources WHERE kind=? AND root=?", (kind, root)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO sources(kind,name,root,config) VALUES(?,?,?,?)",
        (kind, name or root, root, json.dumps(config or {})),
    )
    conn.commit()
    return cur.lastrowid


def load(conn, source_id: int):
    r = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    return _build(r) if r else None


def load_all(conn):
    return [_build(r) for r in conn.execute("SELECT * FROM sources ORDER BY id")]


def _build(r):
    cfg = json.loads(r["config"] or "{}")
    if r["kind"] == "local":
        return LocalSource(r["id"], r["root"], r["name"], cfg)
    if r["kind"] == "gdrive":
        from .connectors.gdrive import GDriveSource
        return GDriveSource(r["id"], r["root"], r["name"], cfg)
    raise ValueError(f"unknown source kind {r['kind']}")
