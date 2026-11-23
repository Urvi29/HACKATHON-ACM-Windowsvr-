"""Scheduled cleanup reports: a non-destructive summary of what could be reclaimed, kept as history.

A background thread writes one report every ME3_REPORT_HOURS (default 24 h). A report only counts and sums
things - nothing is ever deleted or moved - so it can be read later in the Cleanup tab to see whether the
situation is getting better or worse. The latest report is stored in the database, so reading it is instant.
"""
import json
import os
import threading
import time

from . import cleanup, db

INTERVAL_H = float(os.environ.get("ME3_REPORT_HOURS", "24"))
KEEP = 30  # reports to keep

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None


def latest(conn) -> dict | None:
    r = conn.execute("SELECT created, data FROM reports WHERE kind='cleanup' ORDER BY created DESC LIMIT 1").fetchone()
    return {"created": r["created"], "data": json.loads(r["data"])} if r else None


def history(conn, limit: int = 10) -> list[dict]:
    return [{"created": r["created"], "data": json.loads(r["data"])}
            for r in conn.execute("SELECT created, data FROM reports WHERE kind='cleanup' ORDER BY created DESC LIMIT ?", (limit,))]


def generate(conn) -> dict:
    """Count and sum everything reclaimable and store the result (never touches the files themselves)."""
    c = conn if conn is not None else db.get()
    s = cleanup.summary(c)
    tot = c.execute("SELECT count(*) AS n, coalesce(sum(size),0) AS bytes FROM files").fetchone()
    data = {**s, "files": tot["n"], "bytes": tot["bytes"], "ideas": len(cleanup.organisation_ideas(c))}
    created = time.time()
    with _lock:
        c.execute("INSERT INTO reports(kind, created, data) VALUES('cleanup', ?, ?)", (created, json.dumps(data)))
        c.execute("DELETE FROM reports WHERE kind='cleanup' AND id NOT IN "
                  "(SELECT id FROM reports WHERE kind='cleanup' ORDER BY created DESC LIMIT ?)", (KEEP,))
        c.commit()
    return {"created": created, "data": data}


def next_due(conn) -> float:
    h = latest(conn)
    return (h["created"] if h else 0) + INTERVAL_H * 3600


def due(conn) -> bool:
    return time.time() >= next_due(conn)


def _loop():
    conn = None
    while not _stop.wait(120):  # give server startup (indexing) the CPU first, then check every 2 min
        try:
            conn = conn or db.connect()
            if due(conn):
                generate(conn)
        except Exception:  # noqa: BLE001  a failed report must never stop the app
            conn = None


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="cleanup-reports")
    _thread.start()


def stop():
    _stop.set()
