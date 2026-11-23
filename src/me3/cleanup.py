"""Cleanup insights: duplicates, large files, stale media, and rule-based organisation ideas (all on-device)."""
import hashlib
import os
import shutil
import stat
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from . import crawler

PARTIAL = 64 * 1024
IS_WINDOWS = sys.platform.startswith("win")


def _hash_file(path: str, partial: bool) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(PARTIAL) if partial else b"")
        if not partial:
            f.seek(0)
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


MAX_DUP_BYTES = 1 << 62  # no upper limit: big files are only fully hashed when size AND the first 64 KB already match


def find_duplicates(conn, min_size: int = 1, limit_groups: int = 100, source_id: int | None = None) -> dict:
    """size -> partial hash -> full hash. Only local sources are hashed (remote needs a download)."""
    where = "AND source_id=?" if source_id else ""
    args = [min_size] + ([source_id] if source_id else [])
    cand = conn.execute(
        f"""SELECT f.id,f.path,f.size,f.mtime,f.hash,f.kind,f.ext FROM files f JOIN sources s ON s.id=f.source_id
            WHERE s.kind='local' AND f.size>=? AND f.size<={MAX_DUP_BYTES} {where.replace('source_id', 'f.source_id')}
              AND f.size IN (SELECT size FROM files WHERE size>=? AND size<={MAX_DUP_BYTES} GROUP BY size HAVING count(*)>1)""",
        args + [min_size],
    ).fetchall()
    by_size = defaultdict(list)
    for r in cand:
        by_size[r["size"]].append(r)
    groups = []
    for size, rows in by_size.items():
        buckets = defaultdict(list)
        for r in rows:  # partial hash first (cheap), then full only inside matching buckets
            try:
                buckets[_hash_file(r["path"], True)].append(r)
            except OSError:
                pass
        for rs in buckets.values():
            if len(rs) < 2:
                continue
            full = defaultdict(list)
            for r in rs:
                try:
                    hv = r["hash"] or _hash_file(r["path"], False)
                    if not r["hash"]:
                        conn.execute("UPDATE files SET hash=? WHERE id=?", (hv, r["id"]))
                    full[hv].append(r)
                except OSError:
                    pass
            conn.commit()  # release the write lock between buckets so the indexer is never starved
            for hv, g in full.items():
                if len(g) > 1:
                    g.sort(key=lambda r: r["mtime"])
                    groups.append({"hash": hv, "size": size, "wasted": size * (len(g) - 1), "kind": g[0]["kind"], "ext": g[0]["ext"],
                                   "files": [{"id": r["id"], "path": r["path"], "mtime": r["mtime"]} for r in g]})
    conn.commit()
    # Google Drive: compare the MD5 checksums Drive already provides (no downloads)
    remote = conn.execute(
        """SELECT f.id,f.path,f.size,f.mtime,f.hash,f.kind,f.ext FROM files f JOIN sources s ON s.id=f.source_id
           WHERE s.kind!='local' AND f.hash LIKE 'md5:%' AND f.size>=?""", [min_size]).fetchall()
    by_hash = defaultdict(list)
    for r in remote:
        by_hash[(r["size"], r["hash"])].append(r)
    for (size, hv), g in by_hash.items():
        if len(g) > 1:
            g.sort(key=lambda r: r["mtime"])
            groups.append({"hash": hv[4:], "algo": "MD5 (from Google Drive)", "size": size, "wasted": size * (len(g) - 1), "kind": g[0]["kind"],
                           "ext": g[0]["ext"], "remote": True, "files": [{"id": r["id"], "path": r["path"].split("#")[0], "mtime": r["mtime"]} for r in g]})
    # Google Docs/Sheets/Slides have no checksum: compare their exported text instead (reported as "likely", not identical)
    native = conn.execute(
        """SELECT f.id,f.path,f.size,f.mtime,f.kind,f.ext,t.body FROM files f JOIN sources s ON s.id=f.source_id JOIN fts t ON t.rowid=f.id
           WHERE s.kind!='local' AND f.hash IS NULL AND f.ext IN ('gdoc','gsheet','gslides') AND length(t.body) >= 50""").fetchall()
    by_text = defaultdict(list)
    for r in native:
        by_text[(r["ext"], hashlib.sha1(r["body"].encode()).hexdigest())].append(r)
    for (_, hv), g in by_text.items():
        if len(g) > 1:
            g.sort(key=lambda r: r["mtime"])
            groups.append({"hash": hv, "algo": "same text content", "likely": True, "size": g[0]["size"], "wasted": g[0]["size"] * (len(g) - 1),
                           "kind": g[0]["kind"], "ext": g[0]["ext"], "remote": True,
                           "files": [{"id": r["id"], "path": r["path"].split("#")[0], "mtime": r["mtime"]} for r in g]})
    groups.sort(key=lambda g: (-g["wasted"], g["files"][0]["path"]))
    return {"groups": groups[:limit_groups], "total_groups": len(groups), "wasted_bytes": sum(g["wasted"] for g in groups)}


def large_files(conn, limit: int = 50, min_size: int = 50 * 1024 * 1024) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id,path,name,kind,size,mtime FROM files WHERE size>=? ORDER BY size DESC LIMIT ?", (min_size, limit))]


def stale_media(conn, days: int = 365, limit: int = 50, min_size: int = 1024 * 1024) -> list[dict]:
    cutoff = time.time() - days * 86400
    return [dict(r) for r in conn.execute(
        "SELECT id,path,name,kind,size,mtime FROM files WHERE kind IN ('video','archive','image','audio') AND mtime<? AND size>=? "
        "ORDER BY size DESC LIMIT ?", (cutoff, min_size, limit))]


# Files that programs need where they are: never suggest moving / renaming them, never send them to the AI as "clutter".
PROGRAM_EXTS = {"dll", "exe", "so", "sys", "msi", "drv", "ocx", "cpl", "mui", "cab", "pdb", "lib", "a", "o", "pyd", "node", "dylib",
                "pak", "pck", "blk", "bundle", "assets", "resource", "resS", "bin", "dat", "vdf", "acf", "ini", "reg", "log", "db", "dmp"}
PROGRAM_DIRS = {"drive_c", "windows", "system32", "syswow64", "program files", "program files (x86)", "programdata", "appdata",
                "steamapps", "site-packages", "dist-packages", "node_modules", "lib", "lib64", "bin", "sbin", "usr", "opt", "snap",
                "flatpak", "wineprefix", "proton", "compatdata", "venv", ".venv", "__pycache__", "build", "dist", "target"}
CODE_MARKERS = {"package.json", "requirements.txt", "pyproject.toml", "setup.py", "cargo.toml", "go.mod", "pom.xml", "build.gradle",
                "makefile", "cmakelists.txt", "dockerfile", "readme.md", "main.py", "app.py", "index.js"}
IMG_EXTS = {"jpg", "jpeg", "png", "webp", "bmp", "gif", "tif", "tiff", "heic"}


def is_program_file(path: str, ext: str = "") -> bool:
    ext = (ext or os.path.splitext(path)[1].lstrip(".")).lower()
    parts = {p.lower() for p in path.replace("\\", "/").split("/")}
    return ext in PROGRAM_EXTS or bool(parts & PROGRAM_DIRS)


def folder_role(folder: str, rows) -> str | None:
    """Why a folder must be left alone: 'program' (apps, games, system), 'dataset' (images paired with label files),
    'code' (a software project) - or None for ordinary personal folders."""
    names = [os.path.basename(r["path"]) for r in rows]
    exts = [os.path.splitext(n)[1].lstrip(".").lower() for n in names]
    n = len(names) or 1
    if is_program_file(folder + "/") or sum(e in PROGRAM_EXTS for e in exts) / n >= 0.2:
        return "program"
    stems_img = {os.path.splitext(x)[0] for x, e in zip(names, exts) if e in IMG_EXTS}
    others = [os.path.splitext(x)[0] for x, e in zip(names, exts) if e not in IMG_EXTS]
    if stems_img and others and sum(st in stems_img for st in others) / len(others) >= 0.5:
        return "dataset"  # e.g. YOLO/COCO: img_001.jpg + img_001.txt must stay side by side
    if {x.lower() for x in names} & CODE_MARKERS or sum(r["kind"] == "code" for r in rows) / n >= 0.3:
        return "code"
    return None


def organisation_ideas(conn, source_id: int | None = None, max_ideas: int = 12) -> list[dict]:
    """Rule-based suggestions from metadata only. Works offline; the LLM (llm.py) refines wording on redacted data."""
    q = "SELECT path,kind,mtime,size FROM files" + (" WHERE source_id=?" if source_id else "")
    dirs = defaultdict(list)
    for r in conn.execute(q, [source_id] if source_id else []):
        dirs[os.path.dirname(r["path"])].append(r)
    ideas = []
    for d, rows in dirs.items():
        n = len(rows)
        if n < 20 or folder_role(d, rows):
            continue  # too small, or a program / dataset / code folder that must keep its layout
        kinds = defaultdict(int)
        for r in rows:
            kinds[r["kind"]] += 1
        major = [k for k, c in kinds.items() if c >= 8]
        years = {time.gmtime(r["mtime"]).tm_year for r in rows}
        if len(major) >= 2:
            ideas.append({"folder": d, "files": n, "score": n, "action": "split_by_type",
                          "title": f"Split by type: {', '.join(sorted(major))}",
                          "detail": f"{n} files of mixed types share one folder; move each type to its own sub-folder."})
        elif n >= 50 and len(years) >= 3:
            ideas.append({"folder": d, "files": n, "score": n * 0.8, "action": "group_by_year",
                          "title": f"Group by year ({min(years)}-{max(years)})",
                          "detail": f"{n} files span {len(years)} years; year sub-folders make them browsable."})
        unnamed = sum(1 for r in rows if os.path.basename(r["path"]).lower().startswith(("img_", "dsc", "screenshot", "untitled", "new ")))
        if unnamed >= 20:
            ideas.append({"folder": d, "files": unnamed, "score": unnamed * 0.9, "action": "rename_unnamed",
                          "title": "Give camera/untitled files meaningful names",
                          "detail": f"{unnamed} files have generic names (IMG_/DSC/Screenshot...). Semantic search can find them, but albums by event help."})
    ideas.sort(key=lambda i: -i["score"])
    return ideas[:max_ideas]


def summary(conn) -> dict:
    dup = find_duplicates(conn, limit_groups=0)
    big = large_files(conn, limit=1000)
    stale = stale_media(conn, limit=100000)
    return {"duplicate_groups": dup["total_groups"], "duplicate_wasted_bytes": dup["wasted_bytes"],
            "large_files": len(big), "large_bytes": sum(f["size"] for f in big),
            "stale_media_files": len(stale), "stale_media_bytes": sum(f["size"] for f in stale)}


def _home_trash() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Trash"


def _mount_point(path: Path) -> Path:
    path = path.resolve()
    while not os.path.ismount(path):
        path = path.parent
    return path


def _trash_dir_for(path: str) -> tuple[Path, Path | None]:
    """Pick the freedesktop.org trash for a file: home trash on the same device, else $topdir/.Trash/$uid or
    $topdir/.Trash-$uid. Returns (trash_dir, topdir); topdir is set when .trashinfo Path is relative to it."""
    home = _home_trash()
    home.mkdir(parents=True, exist_ok=True)
    if os.stat(path).st_dev == os.stat(home).st_dev:
        return home, None
    top, uid = _mount_point(Path(path).parent), os.getuid()
    shared = top / ".Trash"
    try:
        st = os.lstat(shared)
        if stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX:
            d = shared / str(uid)
            d.mkdir(mode=0o700, exist_ok=True)
            return d, top
    except OSError:
        pass
    d = top / f".Trash-{uid}"
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        return d, top
    except OSError:
        return home, None  # cannot write on that drive: fall back to a copy into the home trash


def send_to_trash(path: str) -> str:
    """Move one file/folder to the system trash so the file manager can restore it.
    Windows: the Recycle Bin (via send2trash). Linux: the freedesktop.org Trash spec."""
    path = os.path.abspath(path)
    if not os.path.lexists(path):
        raise FileNotFoundError(f"not found on disk: {path}")
    if IS_WINDOWS:
        from send2trash import send2trash as _send2trash

        _send2trash(path)  # Recycle Bin; raises OSError on failure
        return path
    trash, top = _trash_dir_for(path)
    (trash / "files").mkdir(parents=True, exist_ok=True)
    (trash / "info").mkdir(parents=True, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    rel = os.path.relpath(path, top) if top else path
    info_text = f"[Trash Info]\nPath={quote(rel)}\nDeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
    for n in range(10000):
        name = f"{stem}{'.' + str(n) if n else ''}{ext}"
        info = trash / "info" / f"{name}.trashinfo"
        try:  # O_EXCL reserves the name atomically, as the spec requires
            fd = os.open(info, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            f.write(info_text)
        if os.path.lexists(trash / "files" / name):
            os.unlink(info)
            continue
        try:
            shutil.move(path, trash / "files" / name)
        except BaseException:
            os.unlink(info)
            raise
        return str(trash / "files" / name)
    raise OSError("could not find a free name in the trash")


def trash_files(conn, ids: list[int]) -> dict:
    """Reversible delete: move local files to the desktop Trash (restorable from the file manager) and drop them from the index."""
    moved, errors, places = [], [], set()
    for fid in ids:
        r = conn.execute("SELECT f.id,f.path,f.source_id,s.kind FROM files f JOIN sources s ON s.id=f.source_id WHERE f.id=?", (fid,)).fetchone()
        if not r or r["kind"] != "local":
            errors.append({"id": fid, "error": "not a local file"})
            continue
        try:
            dest = send_to_trash(r["path"])
            places.add("Recycle Bin" if IS_WINDOWS else str(Path(dest).parent.parent))
            moved.append(fid)
        except OSError as e:
            errors.append({"id": fid, "error": str(e)})
    crawler.remove_ids(conn, moved)
    conn.commit()
    return {"moved": len(moved), "errors": errors, "trash_dir": ", ".join(sorted(places)) or None}
