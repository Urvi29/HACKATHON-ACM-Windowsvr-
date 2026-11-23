"""Physical disks and what fills them.

Hardware and partitions come from `lsblk`, free/used space from statvfs. A low-priority background scan walks each
mounted filesystem (never crossing into other mounts) and totals the real on-disk size (allocated blocks, hard links
counted once) by file type, plus "Apps & system" (OS folders, swap, app data in hidden home folders). Whatever the scan
cannot read (root-only folders) and filesystem overhead is reported as "Reserved & unreadable", so every disk bar adds
up to the disk's full capacity. Results are cached in data/disks.json.
"""
import ctypes
import json
import os
import platform
import shutil
import subprocess
import threading
import time

from . import db
from .util import PSEUDO_FS, classify, ext_of

CACHE = db.ROOT / "data" / "disks.json"
MAX_AGE = 3600  # rescan in the background when the cached result is older than this
IS_WINDOWS = platform.system() == "Windows"
SYSTEM_TOP = {"usr", "opt", "var", "etc", "boot", "efi", "lib", "lib64", "bin", "sbin", "srv", "root", "snap", "nix", "tmp", "lost+found"}
# Windows: system / app folders that are not the user's own files
WIN_SYSTEM_TOP = {"windows", "program files", "program files (x86)", "programdata", "perflogs", "recovery", "$recycle.bin",
                  "system volume information", "msocache", "installer", "windows.old"}
WIN_APP_DIRS = {"appdata", "application data", "local settings", "cookies", "recent", "templates"}
TRASH_NAMES = {"Trash", ".Trash", "$Recycle.Bin", "$RECYCLE.BIN"}

_lock = threading.Lock()
_state = {"running": False, "started": None, "files": 0, "current": None, "error": None}


def _lower_priority():
    """nice 10 + idle I/O class for the scanning thread only, so the desktop stays responsive (POSIX only)."""
    if IS_WINDOWS:
        return  # os.setpriority / ioprio_set do not exist on Windows; the thread is already non-critical
    tid = threading.get_native_id()
    try:
        os.setpriority(os.PRIO_PROCESS, tid, 10)
    except Exception:
        pass
    if platform.machine() == "x86_64":  # ioprio_set(IOPRIO_WHO_PROCESS, tid, IOPRIO_CLASS_IDLE << 13)
        try:
            ctypes.CDLL(None, use_errno=True).syscall(251, 1, tid, 3 << 13)
        except Exception:
            pass


def disk_usage(mount: str) -> tuple[int, int, int]:
    """(total, used, free) bytes for a mounted path. shutil.disk_usage works on Windows and POSIX."""
    u = shutil.disk_usage(mount)
    return u.total, u.used, u.free


def _windows_hardware() -> list[dict]:
    """Drives enumerated through the Win32 API: type, label, capacity and free space (no scanning)."""
    k32 = ctypes.windll.kernel32
    KINDS = {2: "USB / removable", 3: "Fixed disk", 4: "Network drive", 5: "Optical drive", 6: "RAM disk"}
    mask = k32.GetLogicalDrives()
    disks: list[dict] = []
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        letter = chr(65 + i)
        mount = f"{letter}:\\"
        try:
            dtype = k32.GetDriveTypeW(ctypes.c_wchar_p(mount))
        except Exception:
            continue
        if dtype == 0:
            continue
        try:
            total, used, free = disk_usage(mount)
        except OSError:  # e.g. an empty optical drive: "device not ready"
            continue
        label = ctypes.create_unicode_buffer(261)
        fsname = ctypes.create_unicode_buffer(261)
        try:
            k32.GetVolumeInformationW(ctypes.c_wchar_p(mount), label, 261, None, None, None, fsname, 261)
        except Exception:
            pass
        vol_label = label.value or None
        model = vol_label or ("Local Disk" if dtype == 3 else KINDS.get(dtype, "Drive"))
        part = {"name": letter, "label": vol_label, "fstype": (fsname.value or "").lower() or None, "mount": mount,
                "size": total, "fs_total": total, "used": used, "free": free}
        disks.append({"name": letter, "model": model, "kind": KINDS.get(dtype, "Drive"), "size": total,
                      "system": letter.upper() == "C", "label": vol_label, "partitions": [part]})
    disks.sort(key=lambda x: (not x["system"], x["name"]))
    return disks


def _lsblk() -> list[dict]:
    out = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,LABEL,MODEL,ROTA,TRAN,RM"],
                         capture_output=True, text=True, timeout=10, check=True).stdout
    return json.loads(out).get("blockdevices", [])


def _kind_label(d: dict) -> str:
    tran, rota = (d.get("tran") or "").lower(), d.get("rota")
    if tran == "usb" or d.get("rm"):
        return "USB / removable"
    if tran == "nvme":
        return "NVMe SSD"
    return "Hard disk" if rota in (True, "1", 1) else "SSD"


def _flatten(dev: dict) -> list[dict]:
    out = []
    for c in dev.get("children") or []:
        out.append(c)
        out.extend(_flatten(c))  # LVM / crypt layers
    return out


def hardware() -> list[dict]:
    """Disks with their partitions and live space figures (fast: no scanning)."""
    if IS_WINDOWS:
        return _windows_hardware()
    disks = []
    for d in _lsblk():
        if d.get("type") != "disk" or not d.get("size") or d["name"].startswith(("zram", "loop", "ram")):
            continue
        parts = []
        for p in [d] + _flatten(d):
            mps = [m for m in (p.get("mountpoints") or []) if m]
            if p.get("fstype") == "swap":
                parts.append({"name": p["name"], "label": p.get("label"), "fstype": "swap", "mount": "[SWAP]", "size": p["size"],
                              "used": p["size"], "free": 0})
            elif mps:
                m = min(mps, key=len)
                try:
                    total, used, free = disk_usage(m)
                except OSError:
                    continue
                parts.append({"name": p["name"], "label": p.get("label"), "fstype": p.get("fstype"), "mount": m, "size": p["size"],
                              "fs_total": total, "used": used, "free": free})
        mounts = [p["mount"] for p in parts]
        disks.append({"name": d["name"], "model": (d.get("model") or d["name"]).replace("_", " ").strip(), "kind": _kind_label(d),
                      "size": d["size"], "system": "/" in mounts, "label": next((p["label"] for p in parts if p.get("label") and p["fstype"] != "swap"), None),
                      "partitions": parts})
    disks.sort(key=lambda x: (not x["system"], x["name"]))
    return disks


def _category(rel_parts: tuple[str, ...], name: str, mount: str) -> str:
    if mount in ("/boot", "/efi", "/boot/efi"):
        return "system"
    if mount == "/" and rel_parts and rel_parts[0] in SYSTEM_TOP:
        return "system"
    # hidden folders in a home directory hold app data, caches and settings
    if mount == "/" and len(rel_parts) >= 3 and rel_parts[0] == "home" and rel_parts[2].startswith("."):
        return "system"
    low = tuple(p.lower() for p in rel_parts)
    if low and low[0] in WIN_SYSTEM_TOP:  # Windows: C:\Windows, C:\Program Files, ...
        return "system"
    if len(low) >= 3 and low[0] in ("users", "documents and settings") and low[2] in WIN_APP_DIRS:
        return "system"  # C:\Users\<you>\AppData and friends
    return classify(ext_of(name))


def _folder_key(rel_parts: tuple[str, ...]) -> tuple[str, ...]:
    depth = 3 if rel_parts and rel_parts[0].lower() in ("home", "users") else 2  # /home/<user>/<folder> or C:\Users\<you>\<folder>
    return rel_parts[:depth]


def scan_mount(mount: str, progress=None) -> dict:
    """Walk one filesystem without crossing mounts; sum allocated bytes by category and by top folder."""
    dev = os.lstat(mount).st_dev
    cats: dict[str, list[int]] = {}
    folders: dict[tuple, int] = {}
    seen: set[tuple[int, int]] = set()
    trash = 0
    files = 0
    stack = [(mount, ())]
    while stack:
        path, rel = stack.pop()
        if path in PSEUDO_FS:
            continue
        try:
            it = os.scandir(path)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    # DirEntry.stat() on Windows reports st_dev/st_ino/st_nlink as 0, which would break the
                    # mount check and hard-link de-duplication, so fall back to a real stat there.
                    st = os.stat(e.path, follow_symlinks=False) if IS_WINDOWS else e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_dev != dev:
                    continue  # another mount point: counted with its own disk
                r = rel + (e.name,)
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, r))
                    continue
                if st.st_nlink > 1:
                    k = (st.st_dev, st.st_ino)
                    if k in seen:
                        continue
                    seen.add(k)
                size = getattr(st, "st_blocks", 0) * 512 or st.st_size  # st_blocks is Unix-only; on Windows use the size
                c = cats.setdefault(_category(r, e.name, mount), [0, 0])
                c[0] += size
                c[1] += 1
                fk = _folder_key(r[:-1]) if len(r) > 1 else ()
                if fk:
                    folders[fk] = folders.get(fk, 0) + size
                if any(n in TRASH_NAMES or n.startswith(".Trash-") or n.lower() == "$recycle.bin" for n in r[:-1]):
                    trash += size
                files += 1
                if progress and files % 5000 == 0:
                    progress(files)
    top = sorted(folders.items(), key=lambda kv: -kv[1])[:8]
    return {"mount": mount, "files": files, "trash": trash,
            "by_kind": [{"kind": k, "bytes": v[0], "n": v[1]} for k, v in sorted(cats.items(), key=lambda kv: -kv[1][0])],
            "folders": [{"path": os.path.join(mount, *k), "bytes": b} for k, b in top]}


def _load() -> dict | None:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return None


def _run_scan():
    _lower_priority()
    try:
        results, t0 = {}, time.time()
        for d in hardware():
            for p in d["partitions"]:
                if p["fstype"] == "swap":
                    continue
                with _lock:
                    _state["current"] = p["mount"]
                base = _state["files"]
                results[p["mount"]] = scan_mount(p["mount"], progress=lambda n, b=base: _state.__setitem__("files", b + n))
                with _lock:
                    _state["files"] = base + results[p["mount"]]["files"]
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"finished": time.time(), "seconds": round(time.time() - t0, 1), "mounts": results}))
        tmp.replace(CACHE)
        with _lock:
            _state["error"] = None
    except Exception as e:  # keep the last good result; show the error in the UI
        with _lock:
            _state["error"] = str(e)
    finally:
        with _lock:
            _state.update(running=False, current=None)


def start_scan() -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, started=time.time(), files=0, current=None)
    threading.Thread(target=_run_scan, name="disk-scan", daemon=True).start()
    return True


def report() -> dict:
    """Disks + last scan result merged into one bar per disk. Starts a background scan if none is fresh."""
    disks, cache = hardware(), _load()
    if not _state["running"] and (not cache or time.time() - cache.get("finished", 0) > MAX_AGE):
        start_scan()
    mounts = (cache or {}).get("mounts", {})
    for d in disks:
        kinds: dict[str, dict] = {}
        free = trash = 0
        folders = []
        scanned_parts = 0
        for p in d["partitions"]:
            if p["fstype"] == "swap":
                k = kinds.setdefault("system", {"kind": "system", "bytes": 0, "n": 0})
                k["bytes"] += p["size"]
                continue
            free += p["free"]
            m = mounts.get(p["mount"])
            if not m:
                continue
            scanned_parts += 1
            trash += m["trash"]
            folders += m["folders"]
            for k in m["by_kind"]:
                t = kinds.setdefault(k["kind"], {"kind": k["kind"], "bytes": 0, "n": 0})
                t["bytes"] += k["bytes"]
                t["n"] += k["n"]
        content = sum(k["bytes"] for k in kinds.values())
        used = sum(p["used"] for p in d["partitions"])
        d.update(used=used, free=free, trash=trash, scanned=scanned_parts > 0,
                 by_kind=sorted(kinds.values(), key=lambda k: -k["bytes"]),
                 reserved=max(0, d["size"] - free - content),  # root-only folders, filesystem overhead, unpartitioned space
                 folders=sorted(folders, key=lambda f: -f["bytes"])[:8])
    with _lock:
        scan = dict(_state)
    scan["finished"] = (cache or {}).get("finished")
    scan["seconds"] = (cache or {}).get("seconds")
    return {"disks": disks, "scan": scan}
