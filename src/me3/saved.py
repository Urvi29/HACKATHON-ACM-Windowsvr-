"""Saved searches: keep a query and its filters under a name so they can be re-run with one click."""
import json
import time


def _row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "query": r["query"],
            "filters": json.loads(r["filters"] or "{}"), "created": r["created"]}


def list_all(conn) -> list[dict]:
    return [_row(r) for r in conn.execute("SELECT id, name, query, filters, created FROM saved_searches ORDER BY name COLLATE NOCASE")]


def save(conn, name: str, query: str = "", filters: dict | None = None) -> int:
    """Add a saved search, or update the query/filters of the one with the same name (re-saving keeps one entry)."""
    name = name.strip()
    if not name:
        raise ValueError("give the search a name")
    payload = json.dumps(filters or {})
    row = conn.execute("SELECT id FROM saved_searches WHERE name=?", (name,)).fetchone()
    if row:
        conn.execute("UPDATE saved_searches SET query=?, filters=? WHERE id=?", (query, payload, row["id"]))
        sid = row["id"]
    else:
        sid = conn.execute("INSERT INTO saved_searches(name, query, filters, created) VALUES(?,?,?,?)", (name, query, payload, time.time())).lastrowid
    conn.commit()
    return sid


def delete(conn, sid: int) -> bool:
    cur = conn.execute("DELETE FROM saved_searches WHERE id=?", (sid,))
    conn.commit()
    return cur.rowcount > 0
