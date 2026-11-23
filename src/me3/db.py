"""SQLite storage: files table + FTS5 full-text index (+ sqlite-vec later)."""
import os
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("ME3_DB", ROOT / "data" / "me3.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,            -- local | gdrive
  name TEXT NOT NULL,
  root TEXT NOT NULL,
  config TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  name TEXT NOT NULL,
  ext TEXT NOT NULL,
  kind TEXT NOT NULL,            -- document|code|image|video|audio|archive|other
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  extracted INTEGER NOT NULL DEFAULT 0,  -- 0 pending, 1 done, 2 not applicable, 3 failed
  hash TEXT,
  UNIQUE(source_id, path)
);
CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext);
CREATE INDEX IF NOT EXISTS idx_files_extracted ON files(extracted);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  name, path, body, tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS saved_searches(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  query TEXT NOT NULL DEFAULT '',
  filters TEXT NOT NULL DEFAULT '{}',  -- JSON: the UI filter values (kind, sizes, dates)
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                  -- 'cleanup'
  created REAL NOT NULL,
  data TEXT NOT NULL                   -- JSON summary produced by reports.generate()
);
"""

_init_done: set[str] = set()
_init_lock = threading.Lock()
_local = threading.local()


def connect(path=None) -> sqlite3.Connection:
    """New connection (one per thread). Creates schema on first use of a path."""
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=60, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA temp_store=MEMORY")
    with _init_lock:
        if str(p) not in _init_done:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(files)")}
            if "embedded" not in cols:  # 0 pending, 1 embedded, 2 skipped/failed
                c.execute("ALTER TABLE files ADD COLUMN embedded INTEGER NOT NULL DEFAULT 0")
            c.execute("CREATE INDEX IF NOT EXISTS idx_files_embedded ON files(embedded)")
            c.commit()
            _init_done.add(str(p))
    return c


def get() -> sqlite3.Connection:
    """Thread-local cached connection for the default DB (used by API handlers)."""
    c = getattr(_local, "c", None)
    if c is None:
        c = _local.c = connect()
    return c
