"""FastAPI app: search, sources, indexing status, stats. Serves the static UI."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import auth, cleanup, db, disks, embed, llm, privacy, reports, saved, sources
from . import search as searchmod
from .indexer import indexer

STATIC = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app):
    db.connect()
    indexer.resume_all()
    reports.start()
    if embed.available():
        import threading
        threading.Thread(target=lambda: (embed._model(embed.TEXT_MODEL), embed._model(embed.IMG_MODEL)), daemon=True).start()  # warm models
    yield
    reports.stop()
    indexer.shutdown()


app = FastAPI(title="ME3 File System Intelligence", lifespan=lifespan)
app.middleware("http")(auth.require_login)
app.include_router(auth.router)


class SourceIn(BaseModel):
    kind: str = "local"
    root: str
    name: str = ""
    config: dict = {}


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/api/search")
def api_search(
    q: str = "", kind: str = "", ext: str = "", min_size: int | None = None, max_size: int | None = None,
    after: float | None = None, before: float | None = None, source_id: int | None = None,
    natural: bool = True, semantic: bool = True, limit: int = Query(50, le=200), offset: int = 0,
):
    flt = {
        "kinds": [k for k in kind.split(",") if k] or None, "exts": [e.lower().lstrip(".") for e in ext.split(",") if e] or None,
        "min_size": min_size, "max_size": max_size, "after": after, "before": before, "source_id": source_id,
    }
    flt = {k: v for k, v in flt.items() if v is not None}
    return searchmod.search(db.get(), q, flt, natural, limit, offset, semantic)


@app.get("/api/stats")
def stats():
    c = db.get()
    by_kind = [dict(r) for r in c.execute("SELECT kind, count(*) AS n, coalesce(sum(size),0) AS bytes FROM files GROUP BY kind ORDER BY bytes DESC")]
    tot = c.execute("SELECT count(*) AS n, coalesce(sum(size),0) AS bytes FROM files").fetchone()
    pend = c.execute("SELECT count(*) FROM files WHERE extracted=0").fetchone()[0]
    return {"files": tot["n"], "bytes": tot["bytes"], "pending_extraction": pend, "by_kind": by_kind,
            "semantic": embed.available(), "embedding": embed.pending_counts(c)}


@app.get("/api/disks")
def disks_report():
    """Physical disks, partitions, free space and what fills each disk (by file type + biggest folders)."""
    return disks.report()


@app.post("/api/disks/rescan")
def disks_rescan():
    return {"started": disks.start_scan()}


@app.get("/api/insights")
def insights(months: int = Query(24, ge=6, le=120), top: int = Query(10, ge=3, le=50)):
    """Aggregates for the Overview charts: activity per month, biggest folders, size buckets, sources, index progress."""
    import os
    import time as _t
    from collections import defaultdict
    c = db.get()
    now = _t.localtime()
    keys = []
    y, m = now.tm_year, now.tm_mon
    for _ in range(months):  # oldest -> newest, every month present even when empty
        keys.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    keys.reverse()
    per = {r[0]: (r[1], r[2]) for r in c.execute(
        "SELECT strftime('%Y-%m', mtime, 'unixepoch', 'localtime') AS ym, count(*), coalesce(sum(size),0) FROM files "
        "WHERE ym >= ? GROUP BY ym", (keys[0],))}
    timeline = [{"month": k, "n": per.get(k, (0, 0))[0], "bytes": per.get(k, (0, 0))[1]} for k in keys]

    roots = {r["id"]: (r["root"] or "/", r["name"], r["kind"]) for r in c.execute("SELECT id, root, name, kind FROM sources")}
    folders: dict = defaultdict(lambda: [0, 0])
    for r in c.execute("SELECT source_id, path, size FROM files"):
        root, name, kind = roots.get(r["source_id"], ("/", "?", "local"))
        rel = os.path.relpath(r["path"], root) if kind == "local" else r["path"].split("#")[0].lstrip("/")
        parts = [p for p in rel.replace("\\", "/").split("/")[:-1] if p]
        key = (name, "/".join(parts[:2]) or "(top level)")
        folders[key][0] += 1
        folders[key][1] += r["size"]
    big = sorted(folders.items(), key=lambda kv: -kv[1][1])[:top]

    edges = [(0, "< 100 KB"), (100 << 10, "100 KB – 1 MB"), (1 << 20, "1 – 10 MB"), (10 << 20, "10 – 100 MB"), (100 << 20, "100 MB – 1 GB"), (1 << 30, "> 1 GB")]
    sizes = []
    for i, (lo, label) in enumerate(edges):
        hi = edges[i + 1][0] if i + 1 < len(edges) else 1 << 62
        n, b = c.execute("SELECT count(*), coalesce(sum(size),0) FROM files WHERE size >= ? AND size < ?", (lo, hi)).fetchone()
        sizes.append({"label": label, "n": n, "bytes": b})

    srcs = [dict(r) for r in c.execute("SELECT s.name, s.kind, count(f.id) AS n, coalesce(sum(f.size),0) AS bytes "
                                        "FROM sources s LEFT JOIN files f ON f.source_id=s.id GROUP BY s.id ORDER BY bytes DESC")]
    p = c.execute("SELECT sum(extracted=1) AS ex, sum(extracted IN (0,1,3)) AS exable, sum(embedded=1) AS em, "
                  "sum(embedded IN (0,1)) AS emable FROM files").fetchone()
    return {"timeline": timeline, "folders": [{"source": k[0], "folder": k[1], "n": v[0], "bytes": v[1]} for k, v in big],
            "sizes": sizes, "sources": srcs,
            "progress": {"text_done": p["ex"] or 0, "text_total": p["exable"] or 0, "meaning_done": p["em"] or 0, "meaning_total": p["emable"] or 0}}


@app.get("/api/sources")
def list_sources():
    c = db.get()
    out = []
    for r in c.execute("SELECT s.*, (SELECT count(*) FROM files WHERE source_id=s.id) AS files FROM sources s ORDER BY id"):
        d = dict(r)
        d.pop("config", None)
        d["index"] = indexer.snapshot().get(r["id"])
        out.append(d)
    return out


@app.post("/api/sources")
def add_source(s: SourceIn):
    if s.kind == "local":
        import os
        if not os.path.isdir(s.root):
            raise HTTPException(400, f"not a directory: {s.root}")
        new = os.path.realpath(s.root)
        sep = os.path.sep  # '\\' on Windows, '/' elsewhere: keep the nesting check separator-safe
        for r in db.get().execute("SELECT name, root FROM sources WHERE kind='local'"):
            old = os.path.realpath(r["root"])
            if new == old or new.startswith(old.rstrip("/\\") + sep):
                raise HTTPException(400, f"already indexed: {new} is inside '{r['name']}' ({old}), so its files are searchable already")
            if old.startswith(new.rstrip("/\\") + sep):
                raise HTTPException(400, f"{new} contains the existing source '{r['name']}' ({old}); remove that one first to avoid duplicate results")
    elif s.kind == "gdrive":
        from .connectors.gdrive import GDriveSource
        probe = GDriveSource(0, s.root or "/", s.name, s.config)
        root = s.root or "/"
        for r in db.get().execute("SELECT name, root FROM sources WHERE kind='gdrive'"):
            if r["root"] == root:
                raise HTTPException(400, f"'{r['name']}' already covers that")
            if "/" in (root, r["root"]):
                raise HTTPException(400, f"'{r['name']}' ({'whole Drive' if r['root'] == '/' else r['root']}) overlaps: remove it first to avoid duplicate results")
        try:
            probe.test()
        except Exception as e:
            raise HTTPException(400, f"cannot connect: {e}") from e
    else:
        raise HTTPException(400, f"unknown source kind {s.kind}")
    sid = sources.add_source(db.get(), s.kind, s.root or "/", s.name, s.config)
    indexer.start_source(sid)
    return {"id": sid}


@app.get("/api/gdrive/status")
def gdrive_status():
    from .connectors import gdrive
    try:
        client = gdrive.find_client_secret().name
    except RuntimeError:
        client = None
    added = db.get().execute("SELECT 1 FROM sources WHERE kind='gdrive'").fetchone() is not None
    return {"client_file": client, "authorised": gdrive.TOKEN.exists(), "added": added, "authorising": _gd_auth.get("running", False),
            "error": _gd_auth.get("error")}


_gd_auth: dict = {}


@app.post("/api/gdrive/authorize")
def gdrive_authorize():
    """Runs the one-time Google consent in the server's own browser (this app is local). Same client_secret file as sign-in."""
    import threading

    from .connectors import gdrive
    if _gd_auth.get("running"):
        return {"ok": True}
    gdrive.find_client_secret()

    def run():
        _gd_auth.update(running=True, error=None)
        try:
            gdrive.authorize(open_browser=True)
        except Exception as e:  # noqa: BLE001
            _gd_auth["error"] = str(e)[:300]
        _gd_auth["running"] = False
    threading.Thread(target=run, daemon=True, name="gdrive-auth").start()
    return {"ok": True}


@app.delete("/api/sources/{sid}")
def remove_source(sid: int):
    if not sources.load(db.get(), sid):
        raise HTTPException(404)
    if not indexer.remove_source(sid):
        raise HTTPException(409, "this source is being indexed right now; try again when it is idle")
    return {"ok": True}


@app.post("/api/sources/{sid}/reindex")
def reindex(sid: int):
    if not sources.load(db.get(), sid):
        raise HTTPException(404)
    indexer.start_source(sid)
    return {"ok": True}


@app.get("/api/index/status")
def index_status():
    return indexer.snapshot()


# ---------------------------------------------------------------- saved searches
class SavedIn(BaseModel):
    name: str
    query: str = ""
    filters: dict = {}


@app.get("/api/saved_searches")
def list_saved():
    return saved.list_all(db.get())


@app.post("/api/saved_searches")
def add_saved(s: SavedIn):
    try:
        sid = saved.save(db.get(), s.name, s.query, s.filters)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"id": sid}


@app.delete("/api/saved_searches/{sid}")
def remove_saved(sid: int):
    if not saved.delete(db.get(), sid):
        raise HTTPException(404)
    return {"ok": True}


# ---------------------------------------------------------------- cleanup
@app.get("/api/cleanup/summary")
def cleanup_summary():
    return cleanup.summary(db.get())


_dup_cache: dict = {}


@app.get("/api/cleanup/duplicates")
def cleanup_duplicates(min_size: int = 1, limit: int = Query(100000, le=100000), offset: int = 0, kind: str = "", fresh: bool = False):
    """All duplicate groups (largest waste first), filterable by file type and paged. The full result is cached
    until the index changes, so paging and switching types never re-hashes."""
    c = db.get()
    sig = tuple(c.execute("SELECT count(*), coalesce(sum(mtime),0), coalesce(sum(size),0) FROM files").fetchone()) + (min_size,)
    if fresh or _dup_cache.get("sig") != sig:
        _dup_cache.update(sig=sig, data=cleanup.find_duplicates(c, min_size=min_size, limit_groups=1_000_000))
    data = _dup_cache["data"]
    counts: dict = {}
    for g in data["groups"]:
        counts[g["kind"]] = counts.get(g["kind"], 0) + 1
    groups = [g for g in data["groups"] if not kind or g["kind"] == kind]
    return {"groups": groups[offset: offset + limit], "total_groups": data["total_groups"], "matching": len(groups),
            "wasted_bytes": data["wasted_bytes"], "kinds": counts}


@app.get("/api/cleanup/large")
def cleanup_large(limit: int = 50, min_mb: int = 50):
    return cleanup.large_files(db.get(), limit, min_mb * 1024 * 1024)


@app.get("/api/cleanup/stale")
def cleanup_stale(days: int = 365, limit: int = 50):
    return cleanup.stale_media(db.get(), days, limit)


@app.get("/api/cleanup/report")
def cleanup_report(limit: int = Query(10, ge=1, le=50)):
    """The latest scheduled cleanup report plus the earlier ones (nothing is ever deleted or moved)."""
    c = db.get()
    return {"latest": reports.latest(c), "history": reports.history(c, limit),
            "interval_hours": reports.INTERVAL_H, "next_due": reports.next_due(c), "due": reports.due(c)}


@app.post("/api/cleanup/report/run")
def cleanup_report_run():
    return reports.generate(db.get())


# ---------------------------------------------------------------- viewing files (so users can check before trashing)
PREVIEW_TEXT = 6000
RAW_MAX_REMOTE = 60 * 1024 * 1024
UNSAFE_INLINE = {"html", "htm", "svg", "xml", "xhtml", "js", "mjs"}  # never render these as active content in our origin


def _file_row(fid: int):
    r = db.get().execute("SELECT f.*, s.kind AS skind, s.name AS source FROM files f JOIN sources s ON s.id=f.source_id WHERE f.id=?", (fid,)).fetchone()
    if not r:
        raise HTTPException(404, "file is no longer in the index")
    return r


@app.get("/api/files/{fid}")
def file_info(fid: int):
    """Metadata + a text preview (from the index, or read directly for small local text files)."""
    import os
    from datetime import datetime

    from .util import TEXT_EXTS
    r = _file_row(fid)
    body = db.get().execute("SELECT substr(body,1,?) FROM fts WHERE rowid=?", (PREVIEW_TEXT, fid)).fetchone()
    text = (body[0] if body else "") or ""
    if not text and r["skind"] == "local" and r["ext"] in TEXT_EXTS and r["size"] <= 2 * 1024 * 1024:
        try:
            with open(r["path"], encoding="utf-8", errors="replace") as fh:
                text = fh.read(PREVIEW_TEXT)
        except OSError:
            pass
    exists = os.path.exists(r["path"]) if r["skind"] == "local" else True
    return {"id": r["id"], "name": r["name"], "path": r["path"], "ext": r["ext"], "kind": r["kind"], "size": r["size"],
            "modified": datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M"), "source": r["source"], "local": r["skind"] == "local",
            "exists": exists, "hash": r["hash"], "text": text, "inline": r["ext"] not in UNSAFE_INLINE}


@app.get("/api/files/{fid}/raw")
def file_raw(fid: int):
    """The file itself, for the in-app viewer (images, PDF, audio, video). Active formats are sent as plain text."""
    import mimetypes
    import os

    r = _file_row(fid)
    mime = "text/plain; charset=utf-8" if r["ext"] in UNSAFE_INLINE else (mimetypes.guess_type(r["name"])[0] or "application/octet-stream")
    headers = {"X-Content-Type-Options": "nosniff", "Content-Disposition": "inline", "Cache-Control": "private, max-age=60"}
    if not mime.startswith(("image/", "video/", "audio/", "application/pdf")) or mime == "image/svg+xml":
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"  # browsers' PDF/media viewers break under sandbox; others never run
    if r["skind"] == "local":
        if not os.path.isfile(r["path"]):
            raise HTTPException(404, "file not found on disk (moved or deleted?)")
        return FileResponse(r["path"], media_type=mime, headers=headers)
    if r["size"] > RAW_MAX_REMOTE:
        raise HTTPException(413, "too large to preview from Drive; open it in Google Drive instead")
    src = sources.load(db.get(), r["source_id"])
    try:
        with src.fetch(r["path"]) as local, open(local, "rb") as fh:
            data = fh.read()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"could not download from {r['source']}: {e}") from e
    return Response(data, media_type=mime, headers=headers)


KNOWN_TYPES = {
    "dll": "Windows program library (DLL)", "exe": "Windows program", "so": "Linux shared library", "msi": "Windows installer",
    "iso": "Disc image", "bin": "Binary data", "dat": "Data file", "pak": "Game/app data package", "db": "Database", "sqlite": "SQLite database",
    "apk": "Android app package", "deb": "Debian package", "pkg": "Package", "ttf": "Font", "otf": "Font", "woff": "Web font", "woff2": "Web font",
    "pyc": "Compiled Python", "class": "Compiled Java", "o": "Compiled object file", "a": "Static library", "jar": "Java archive",
    "xlsx": "Excel spreadsheet", "pptx": "PowerPoint presentation", "docx": "Word document", "odt": "OpenDocument text", "epub": "E-book",
    "heic": "HEIC photo", "psd": "Photoshop image", "mkv": "Matroska video", "avi": "AVI video", "wmv": "Windows video", "flv": "Flash video",
}
MAGIC = [(b"MZ", "Windows executable / DLL (PE)"), (b"\x7fELF", "Linux executable / library (ELF)"), (b"PK\x03\x04", "ZIP container"),
         (b"%PDF", "PDF document"), (b"\x89PNG", "PNG image"), (b"\xff\xd8\xff", "JPEG image"), (b"GIF8", "GIF image"), (b"SQLite format 3", "SQLite database"),
         (b"\x1f\x8b", "gzip archive"), (b"7z\xbc\xaf", "7-Zip archive"), (b"Rar!", "RAR archive"), (b"OggS", "Ogg media"), (b"RIFF", "RIFF media (WAV/AVI/WebP)"),
         (b"ID3", "MP3 audio"), (b"\x1aE\xdf\xa3", "Matroska/WebM video"), (b"\xd0\xcf\x11\xe0", "Old Office document (OLE)")]


@app.get("/api/files/{fid}/peek")
def file_peek(fid: int):
    """For files the browser cannot display: what it is, what is inside (archives, Office files) and its first bytes."""
    import hashlib
    import re
    import tarfile
    import zipfile

    r = _file_row(fid)
    src = sources.load(db.get(), r["source_id"])
    if r["skind"] != "local" and r["size"] > RAW_MAX_REMOTE:
        raise HTTPException(413, "too large to inspect from Drive")
    out = {"type": KNOWN_TYPES.get(r["ext"], f".{r['ext']} file" if r["ext"] else "File without an extension"),
           "detected": None, "entries": None, "entries_total": 0, "text": "", "hex": "", "strings": [], "sha1": r["hash"]}
    try:
        with src.fetch(r["path"]) as local:
            with open(local, "rb") as fh:
                head = fh.read(4096)
            out["detected"] = next((d for sig, d in MAGIC if head.startswith(sig)), None)
            if zipfile.is_zipfile(local):
                with zipfile.ZipFile(local) as z:
                    infos = z.infolist()
                    out["entries_total"] = len(infos)
                    out["entries"] = [{"name": i.filename, "size": i.file_size} for i in infos[:300]]
                    parts = [n for n in z.namelist() if re.match(r"(xl/sharedStrings|ppt/slides/slide\d+|word/document|content)\.xml$", n)]
                    for n in sorted(parts, key=lambda x: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", x)])[:60]:
                        xml = z.read(n)[:400_000].decode("utf-8", "replace")
                        out["text"] += re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml)).strip() + "\n\n"
                    out["text"] = out["text"][:PREVIEW_TEXT]
            elif r["ext"] in ("tar", "gz", "tgz", "bz2", "xz") and tarfile.is_tarfile(local):
                with tarfile.open(local) as t:
                    members = []
                    for m in t:
                        members.append({"name": m.name, "size": m.size})
                        if len(members) >= 300:
                            break
                    out["entries"], out["entries_total"] = members, len(members)
            if r["skind"] == "local" and not out["sha1"] and r["size"] <= 512 * 1024 * 1024:
                h = hashlib.sha1()
                with open(local, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                out["sha1"] = h.hexdigest()
    except OSError as e:
        raise HTTPException(404, f"cannot read the file: {e}") from e
    except Exception as e:  # noqa: BLE001  (corrupt archive etc.: still show the bytes)
        out["error"] = str(e)[:200]
    out["hex"] = "\n".join(f"{i:08x}  {head[i:i + 16].hex(' '):<47}  {''.join(chr(b) if 32 <= b < 127 else '.' for b in head[i:i + 16])}"
                           for i in range(0, min(len(head), 512), 16))
    out["strings"] = [m.decode() for m in re.findall(rb"[ -~]{6,}", head)][:40]
    return out


class OpenIn(BaseModel):
    reveal: bool = False  # True = show the file in its folder instead of opening it


@app.post("/api/files/{fid}/open")
def file_open(fid: int, o: OpenIn):
    """Open a local file with the computer's default app, or show it in the file manager (this app runs on the user's own machine)."""
    import os
    import subprocess
    import sys

    r = _file_row(fid)
    if r["skind"] != "local":
        raise HTTPException(400, "only files on this computer can be opened here")
    path = r["path"]
    if not os.path.exists(path):
        raise HTTPException(404, "file not found on disk (moved or deleted?)")
    try:
        if sys.platform.startswith("win"):
            if o.reveal:
                subprocess.Popen(["explorer", "/select,", path])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if o.reveal else ["open", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) if o.reveal else path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"could not open: {e}") from e
    return {"ok": True}


@app.get("/api/folder")
def folder_files(path: str, limit: int = Query(200, le=1000)):
    """Files directly inside one folder (used by Organise -> View files)."""
    from datetime import datetime

    from .util import like_esc

    pre = path.rstrip("/\\")
    direct, nested = like_esc(path)
    rows = db.get().execute(
        "SELECT id,name,path,kind,ext,size,mtime FROM files WHERE path LIKE ? ESCAPE '\\' AND path NOT LIKE ? ESCAPE '\\' ORDER BY kind, name LIMIT ?",
        (direct, nested, limit)).fetchall()
    total = db.get().execute("SELECT count(*) FROM files WHERE path LIKE ? ESCAPE '\\' AND path NOT LIKE ? ESCAPE '\\'", (direct, nested)).fetchone()[0]
    return {"folder": pre, "total": total, "files": [dict(r) | {"modified": datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")} for r in rows]}


class TrashIn(BaseModel):
    ids: list[int]


@app.post("/api/cleanup/trash")
def cleanup_trash(t: TrashIn):
    """Reversible: files go to the desktop Trash (freedesktop.org spec), restorable from the file manager."""
    return cleanup.trash_files(db.get(), t.ids)


# ---------------------------------------------------------------- privacy + AI suggestions
class RedactIn(BaseModel):
    text: str


@app.post("/api/privacy/redact")
def privacy_redact(r: RedactIn):
    out = privacy.redact(r.text)
    return {"redacted": out.text, "counts": out.counts}


class SuggestIn(BaseModel):
    source_id: int | None = None
    send: bool = False  # False = preview only; nothing leaves the machine
    sample: int = 60


@app.post("/api/suggest")
def suggest(s: SuggestIn):
    """Organisation suggestions. Always returns rule-based ideas (on-device) and the exact redacted payload.
    The payload is sent to the cloud LLM only when send=true AND an API key is configured."""
    c = db.get()
    ideas = cleanup.organisation_ideas(c, s.source_id)
    rows = []
    from .util import like_esc

    for i in ideas[:4]:  # sample from the messiest folders, then pad with random files
        rows += c.execute("SELECT * FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY random() LIMIT 12",
                          (like_esc(i["folder"])[0],)).fetchall()
    pool = c.execute("SELECT * FROM files ORDER BY random() LIMIT ?", (s.sample * 8,)).fetchall()
    rows += [r for r in pool if not cleanup.is_program_file(r["path"], r["ext"])][: max(0, s.sample - len(rows))]
    rows = [r for r in rows if not cleanup.is_program_file(r["path"], r["ext"])]  # apps/games/system files are not the user's clutter
    from datetime import datetime
    items = [{"path": r["path"], "name": r["name"], "kind": r["kind"], "ext": r["ext"], "size": r["size"],
              "modified": datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")} for r in rows]
    payload = privacy.prepare_payload(items)
    out = {"ideas": ideas, "payload": payload, "llm_available": llm.available(), "local": llm.is_local(), "sent": False, "ai": None}
    if s.send:
        if not llm.available():
            raise HTTPException(400, "No AI model available: start Ollama with the gemma4:e4b model (or set ME3_LLM_PROVIDER + a key in .env). Showing on-device suggestions only.")
        try:
            out["ai"], out["sent"] = llm.organise(payload), True
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            who = "The on-device AI (Ollama)" if llm.is_local() else "The cloud AI"
            if isinstance(e, llm.LLMError) and llm.is_local():
                raise HTTPException(502, f"{who} failed: {e}") from e
            if isinstance(e, llm.LLMError):
                code = {400: 400, 401: 401, 403: 401, 404: 400, 429: 429}.get(e.status, 502)
                hint = {401: "Gemini rejected the key: create one at aistudio.google.com/apikey.",
                        429: "Gemini free-tier rate limit hit: wait a minute and retry."}.get(code, f"Gemini request failed: {e}")
                raise HTTPException(code, hint) from e
            if name == "AuthenticationError":
                raise HTTPException(401, "Anthropic rejected the key. It must be a standard API key (sk-ant-api...) from console.anthropic.com, not an sk-ant-usr key.") from e
            if "credit balance" in str(e):
                raise HTTPException(402, "Anthropic API key is valid but the account has no credits: add credits at console.anthropic.com -> Plans & Billing.") from e
            if name in ("NotFoundError", "BadRequestError"):
                raise HTTPException(400, f"Anthropic request failed ({name}): {e}. Check ME3_LLM_MODEL (default {llm.MODEL}).") from e
            raise HTTPException(502, f"{who} failed ({name}): {e}") from e
    return out


@app.get("/api/llm/status")
def llm_status():
    return {"cloud_available": llm.available() and not llm.is_local(), "ai_available": llm.available(), "local": llm.is_local(), "provider": llm.provider(), "model": llm.model_name()}


@app.get("/api/thumb/{fid}")
def thumb(fid: int, size: int = Query(256, ge=64, le=1280)):
    """JPEG thumbnail (default 256px; the UI preview asks for a larger one) for an image file (local or remote); generated on the fly, never leaves the machine."""
    import io


    c = db.get()
    r = c.execute("SELECT source_id,path,kind,ext FROM files WHERE id=?", (fid,)).fetchone()
    if not r or r["kind"] != "image" or r["ext"] not in embed.IMG_EXTS:
        raise HTTPException(404)
    src = sources.load(c, r["source_id"])
    try:
        with src.fetch(r["path"]) as local:
            im = embed._load_image(local)
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
    except Exception as e:
        raise HTTPException(404, str(e)) from e
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})
