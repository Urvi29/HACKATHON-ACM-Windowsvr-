"""Google Drive source (read-only OAuth). Setup, once:
 1. Google Cloud Console -> APIs & Services -> enable "Google Drive API"
 2. Credentials -> Create OAuth client ID (type: Desktop app) -> download JSON. It is found automatically if it is
    credentials.json / client_secret_*.json in the project root, in Downloads or on the Desktop (> python -m me3 auth-check)
 3. OAuth consent screen: add your Google account as a test user
 4. Run:  python -m me3 gdrive-auth      (opens a browser once; stores <project>/token.json)
 5. Add the source in the UI (kind=gdrive, root="/") or:  python -m me3 add-gdrive
Google-native files (Docs/Sheets/Slides) are exported as text for indexing; other files are downloaded to a temp file on demand.
The `service` argument allows injecting a fake Drive client for tests.
"""
import contextlib
import io
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .. import db
from ..sources import Entry

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CREDS = db.ROOT / "credentials.json"
# folders we look in (the browser usually drops the console download into Downloads)
SEARCH_DIRS = [db.ROOT, Path.home() / "Downloads", Path.home() / "Desktop"]
DESKTOP_HINT = ("Google Cloud Console -> APIs & Services -> Credentials -> Create credentials -> "
                "OAuth client ID -> Desktop app -> download the JSON")


def client_info(path) -> dict:
    """Read a client-secret JSON and check it is a Desktop-app client (the type loopback sign-in requires)."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{p.name} is not readable JSON ({e})") from e
    if isinstance(data.get("web"), dict):
        raise RuntimeError(f"{p.name} is a **Web** OAuth client; sign-in needs a **Desktop app** client ({DESKTOP_HINT}).")
    if not isinstance(data.get("installed"), dict):
        raise RuntimeError(f"{p.name} is not a Google OAuth client file (no 'installed' section).")
    s = data["installed"]
    return {"path": str(p), "kind": "Desktop", "project_id": s.get("project_id", ""), "client_id": s.get("client_id", "")}


def _candidates():
    """Files to try, in order: $ME3_GOOGLE_CLIENT (a file or a folder), project root, then Downloads/Desktop."""
    env = os.environ.get("ME3_GOOGLE_CLIENT")
    if env:
        e = Path(env)
        if e.is_dir():
            yield e / "credentials.json"
            yield from sorted(e.glob("client_secret*.json"))
        else:
            yield e
    yield CREDS
    for d in SEARCH_DIRS:
        yield d / "credentials.json"
        yield from sorted(d.glob("client_secret*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def find_client_secret():
    """First valid Desktop OAuth client file found (see _candidates); raises with a clear fix when none is usable."""
    seen, bad = set(), None
    for p in _candidates():
        try:
            p = p.resolve()
        except OSError:
            continue
        if str(p) in seen or not p.is_file():
            continue
        seen.add(str(p))
        try:
            client_info(p)
            return p
        except RuntimeError as e:  # a wrong file here does not stop us looking at the next candidate
            bad = e
    if bad:
        raise bad
    raise RuntimeError(
        "No Google OAuth client file found. Create one (" + DESKTOP_HINT + ") and save the JSON as credentials.json "
        "in the project root - or just leave it in Downloads. Looked in: " + ", ".join(str(d) for d in SEARCH_DIRS)
    )


TOKEN = db.ROOT / "token.json"
FOLDER = "application/vnd.google-apps.folder"
NATIVE = {  # mime -> (suffix we show, export mime)
    "application/vnd.google-apps.document": (".gdoc", "text/plain"),
    "application/vnd.google-apps.spreadsheet": (".gsheet", "text/csv"),
    "application/vnd.google-apps.presentation": (".gslides", "text/plain"),
}


def authorize(open_browser: bool = True) -> None:
    """Interactive one-time OAuth flow; writes token.json. Prints the URL so it also works when no browser can be launched."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(find_client_secret()), SCOPES)
    try:
        creds = flow.run_local_server(port=0, open_browser=open_browser, authorization_prompt_message="Open this URL to authorise Google Drive:\n{url}\n",
                                      success_message="ME3 is authorised. You can close this tab.")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Google authorisation failed: {e}. Check: Drive API enabled in project, your account listed as a Test user on the OAuth consent screen.") from e
    TOKEN.write_text(creds.to_json())
    os.chmod(TOKEN, 0o600)


def build_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN.exists():
        raise RuntimeError("Google Drive not authorised: run `python -m me3 gdrive-auth` first (see connectors/gdrive.py)")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:  # noqa: BLE001  (e.g. invalid_grant: token revoked, or expired after 7 days while the app is in Testing mode)
            raise RuntimeError(f"Google token refresh failed ({e}). Run `python -m me3 gdrive-auth` again.") from e
        TOKEN.write_text(creds.to_json())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


class GDriveSource:
    kind = "gdrive"
    watchable = False

    def __init__(self, id: int, root: str = "/", name: str = "", config: dict | None = None, service=None):
        self.id, self.root, self.name, self.config = id, root or "/", name or "Google Drive", config or {}
        self._service = service
        self._ids: dict[str, tuple[str, str]] = {}  # path -> (file id, mime)

    @property
    def svc(self):
        if self._service is None:
            self._service = build_service()
        return self._service

    def test(self) -> None:
        self.svc.about().get(fields="user").execute()
        if self.root != "/":
            folders = list(self._list_all(f"trashed=false and mimeType='{FOLDER}'"))
            if not self._scope_ids(folders):
                raise RuntimeError(f"no Drive folder named or with id '{self.root.strip('/')}'")

    def _scope_ids(self, folders) -> set[str]:
        """Folder ids selected by root: '/' = whole Drive (empty set means no restriction), else folder id or exact name."""
        key = self.root.strip("/")
        return {f["id"] for f in folders if f["id"] == key or f["name"].lower() == key.lower()}

    def _list_all(self, q: str = "trashed=false"):
        token = None
        while True:
            r = self.svc.files().list(
                q=q, pageSize=1000, pageToken=token,
                fields="nextPageToken, files(id,name,mimeType,size,modifiedTime,parents,md5Checksum)",
            ).execute()
            yield from r.get("files", [])
            token = r.get("nextPageToken")
            if not token:
                break

    def walk(self, start: str | None = None):
        files = list(self._list_all())
        by_id = {f["id"]: f for f in files}

        def folder_path(fid, depth=0):
            f = by_id.get(fid)
            if not f or depth > 30:
                return ""
            par = (f.get("parents") or [None])[0]
            return folder_path(par, depth + 1) + "/" + f["name"] if par else "/" + f["name"]

        scope = self._scope_ids([f for f in files if f["mimeType"] == FOLDER]) if self.root != "/" else set()
        if self.root != "/" and not scope:
            raise RuntimeError(f"Drive folder '{self.root.strip('/')}' not found")  # never return an empty scan: it would wipe the index

        def in_scope(f) -> bool:
            if not scope:
                return True
            fid, depth = (f.get("parents") or [None])[0], 0
            while fid and depth < 30:
                if fid in scope:
                    return True
                fid, depth = ((by_id.get(fid) or {}).get("parents") or [None])[0], depth + 1
            return False

        self._ids.clear()
        for f in files:
            if f["mimeType"] == FOLDER or not in_scope(f):
                continue
            native = NATIVE.get(f["mimeType"])
            if f["mimeType"].startswith("application/vnd.google-apps.") and not native:
                continue  # forms, drawings, shortcuts...
            par = (f.get("parents") or [None])[0]
            name = f["name"] + (native[0] if native else "")
            path = (folder_path(par) if par else "") + "/" + name + f"#{f['id']}"
            self._ids[path] = (f["id"], f["mimeType"])
            md5 = f.get("md5Checksum")  # Drive computes this for uploaded files (not for Docs/Sheets/Slides)
            yield Entry(path, name, int(f.get("size") or 0), _ts(f.get("modifiedTime", "")), hash=f"md5:{md5}" if md5 else None)

    def stat(self, path: str):
        return None  # not used (no watcher); periodic rescans call walk()

    @contextlib.contextmanager
    def fetch(self, path: str):
        fid, mime = self._ids.get(path) or (path.rsplit("#", 1)[-1], "")
        if not mime:
            base = path.rsplit("#", 1)[0]
            mime = next((m for m, (suf, _) in NATIVE.items() if base.endswith(suf)), "")
        suffix = "_" + os.path.basename(path.rsplit("#", 1)[0])
        fd, tmp = tempfile.mkstemp(suffix=suffix, dir=os.environ.get("ME3_TMP"))
        os.close(fd)
        try:
            from googleapiclient.http import MediaIoBaseDownload

            if mime in NATIVE:
                req = self.svc.files().export_media(fileId=fid, mimeType=NATIVE[mime][1])
            else:
                req = self.svc.files().get_media(fileId=fid)
            buf = io.FileIO(tmp, "wb")
            dl = MediaIoBaseDownload(buf, req, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
            buf.close()
            yield tmp
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
