"""Incremental crawler: only new/changed files are (re)written; vanished files are removed."""
from .sources import Entry
from .util import EXTRACTABLE, classify, ext_of

BATCH = 2000


def _row(sid: int, e: Entry):
    ext = ext_of(e.name)
    return ext, classify(ext), (0 if ext in EXTRACTABLE else 2)


def upsert(conn, sid: int, e: Entry, old=None) -> str:
    """Insert or update one file. Returns 'new' | 'changed' | 'same'."""
    ext, kind, ex = _row(sid, e)
    if old is None:
        cur = conn.execute(
            "INSERT INTO files(source_id,path,name,ext,kind,size,mtime,extracted,hash) VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, e.path, e.name, ext, kind, e.size, e.mtime, ex, e.hash),
        )
        conn.execute("INSERT INTO fts(rowid,name,path,body) VALUES(?,?,?,'')", (cur.lastrowid, e.name, e.path))
        return "new"
    if old["size"] != e.size or abs(old["mtime"] - e.mtime) > 1e-6:
        conn.execute(
            "UPDATE files SET size=?,mtime=?,extracted=?,hash=?,embedded=0 WHERE id=?", (e.size, e.mtime, ex, e.hash, old["id"])
        )
        conn.execute("UPDATE fts SET body='' WHERE rowid=?", (old["id"],))
        return "changed"
    if e.hash and "hash" in old.keys() and old["hash"] != e.hash:  # backfill a free checksum on rows indexed before it existed
        conn.execute("UPDATE files SET hash=? WHERE id=?", (e.hash, old["id"]))
    return "same"


def remove_ids(conn, ids):
    from . import embed

    embed.delete_vectors(conn, ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        q = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM fts WHERE rowid IN ({q})", chunk)
        conn.execute(f"DELETE FROM files WHERE id IN ({q})", chunk)


def scan(conn, src, progress=None, should_stop=lambda: False) -> dict:
    """Full incremental scan of a source. Safe to re-run; cheap when nothing changed."""
    sid = src.id
    existing = {
        r["path"]: r for r in conn.execute("SELECT id,path,size,mtime,hash FROM files WHERE source_id=?", (sid,))
    }
    seen, stats, n = set(), {"new": 0, "changed": 0, "same": 0, "removed": 0, "aborted": False}, 0
    for e in src.walk():
        if should_stop():
            stats["aborted"] = True
            break
        seen.add(e.path)
        stats[upsert(conn, sid, e, existing.get(e.path))] += 1
        n += 1
        if n % BATCH == 0:
            conn.commit()
            if progress:
                progress(n)
    conn.commit()
    if not stats["aborted"]:
        gone = [existing[p]["id"] for p in existing.keys() - seen]
        remove_ids(conn, gone)
        stats["removed"] = len(gone)
        conn.commit()
    if progress:
        progress(n)
    return stats


def index_path(conn, src, path: str) -> None:
    """Apply a single filesystem change (from the watcher): add/update/remove path (file or dir)."""
    e = src.stat(path)
    if e is None:
        remove_path(conn, src.id, path)
    elif e.is_dir:
        for fe in src.walk(path):
            old = conn.execute("SELECT id,size,mtime FROM files WHERE source_id=? AND path=?", (src.id, fe.path)).fetchone()
            upsert(conn, src.id, fe, old)
    else:
        old = conn.execute("SELECT id,size,mtime FROM files WHERE source_id=? AND path=?", (src.id, path)).fetchone()
        upsert(conn, src.id, e, old)
    conn.commit()


def remove_path(conn, sid: int, path: str) -> None:
    from .util import like_esc

    like, _ = like_esc(path)
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM files WHERE source_id=? AND (path=? OR path LIKE ? ESCAPE '\\')", (sid, path, like)
        )
    ]
    remove_ids(conn, ids)
    conn.commit()
