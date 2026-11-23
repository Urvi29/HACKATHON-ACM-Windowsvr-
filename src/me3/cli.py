import time

import typer
from rich import print
from rich.table import Table

from . import crawler, db, embed, extract, sources
from . import search as searchmod
from .util import human_size

app = typer.Typer(add_completion=False, help="ME3 File System Intelligence")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765):
    """Run the web app."""
    import uvicorn
    uvicorn.run("me3.api:app", host=host, port=port)


@app.command()
def index(root: str, name: str = "", no_embed: bool = False):
    """Index a local folder (no server): crawl + extract."""
    c = db.connect()
    sid = sources.add_source(c, "local", root, name)
    src = sources.load(c, sid)
    t = time.time()
    st = crawler.scan(c, src)
    t1 = time.time()
    n = extract.run_pending(c)
    t2 = time.time()
    print(f"[green]crawl[/] {st} in {t1 - t:.1f}s; [green]extracted[/] {n} in {t2 - t1:.1f}s")
    if embed.available() and not no_embed:
        m = embed.run_pending(c)
        print(f"[green]embedded[/] {m} files in {time.time() - t2:.1f}s")


@app.command()
def search(q: str, limit: int = 10):
    """Search the index from the terminal."""
    r = searchmod.search(db.connect(), q, limit=limit)
    print(f"text={r['text']!r} filters={r['filters']} total={r['total']}")
    t = Table()
    for col in ("name", "kind", "size", "modified", "path"):
        t.add_column(col)
    for i in r["results"]:
        t.add_row(i["name"], i["kind"], human_size(i["size"]), i["modified"], i["path"])
    print(t)


@app.command("gdrive-auth")
def gdrive_auth(no_browser: bool = False):
    """One-time Google Drive OAuth (client_secret_*.json or credentials.json in the project root)."""
    from .connectors import gdrive
    try:
        gdrive.authorize(open_browser=not no_browser)
    except RuntimeError as e:
        print(f"[red]{e}[/]")
        raise typer.Exit(1)
    print("[green]authorised[/]; token saved. Now: python -m me3 add-gdrive")


@app.command("auth-check")
def auth_check():
    """Check the Google OAuth client file and token; say exactly what is missing."""
    from .connectors import gdrive

    try:
        info = gdrive.client_info(gdrive.find_client_secret())
    except RuntimeError as e:
        print(f"[red]client file:[/] {e}")
        raise typer.Exit(1)
    print(f"[green]client file[/] {info['path']}")
    print(f"  type: {info['kind']} client (browser sign-in OK)")
    print(f"  project: {info['project_id'] or '(none listed)'}")
    if not gdrive.TOKEN.exists():
        print("[yellow]token:[/] not found -> run `python -m me3 gdrive-auth`")
        return
    print(f"[green]token[/] {gdrive.TOKEN}")
    try:
        gdrive.build_service().about().get(fields="user").execute()
        print("[green]Drive API reachable[/] - token works; run `python -m me3 add-gdrive`")
    except Exception as e:  # noqa: BLE001
        print(f"[red]Drive API check failed[/] {e}")
        raise typer.Exit(1)


@app.command("add-gdrive")
def add_gdrive(name: str = "Google Drive"):
    from .connectors.gdrive import GDriveSource
    GDriveSource(0).test()
    sid = sources.add_source(db.connect(), "gdrive", "/", name)
    print(f"added source {sid}; start `python -m me3 serve` to index it (or use the UI rescan)")
