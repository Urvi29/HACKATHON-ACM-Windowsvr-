"""Text extraction for the full-text index (txt/code/pdf/docx). Runs after the crawl, in the background."""
import os
import shutil
import subprocess
import tempfile

from . import sources as srcmod

MAX_CHARS = int(os.environ.get("ME3_MAX_CHARS", 20_000))  # per file stored in the index
MAX_BYTES = 80 * 1024 * 1024  # skip reading files larger than this


IMG_OCR = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}
OCR_MAX_PAGES = 5
OCR_MAX_IMG_BYTES = 15 * 1024 * 1024


def ocr_available() -> bool:
    """OCR switches on by itself when the tesseract binary exists (sudo pacman -S tesseract tesseract-data-eng). ME3_OCR=0 disables."""
    return os.environ.get("ME3_OCR", "1") != "0" and shutil.which("tesseract") is not None


def _nice(*cmd: str) -> list[str]:
    """Prefix a command with `nice -n 10` on systems that have it (Linux/macOS); plain command on Windows."""
    return ["nice", "-n", "10", *cmd] if shutil.which("nice") else list(cmd)


def _tesseract(img_path: str) -> str:
    r = subprocess.run(_nice("tesseract", img_path, "-", "-l", "eng"), capture_output=True, text=True, timeout=60)
    return r.stdout if r.returncode == 0 else ""


def ocr_image(path: str) -> str:
    return _tesseract(path)[:MAX_CHARS] if os.path.getsize(path) <= OCR_MAX_IMG_BYTES else ""


def ocr_pdf(path: str) -> str:
    """Scanned PDFs: rasterise first pages with pdftoppm (poppler), then OCR each."""
    if not shutil.which("pdftoppm"):
        return ""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(_nice("pdftoppm", "-r", "150", "-l", str(OCR_MAX_PAGES), "-png", path, os.path.join(d, "p")),
                       capture_output=True, timeout=120)
        return "\n".join(_tesseract(os.path.join(d, f)) for f in sorted(os.listdir(d)))[:MAX_CHARS]


def read_text(path: str, ext: str) -> str:
    if ext in IMG_OCR:
        return ocr_image(path) if ocr_available() else ""
    if ext == "pdf":
        from pypdf import PdfReader

        parts, total = [], 0
        for page in PdfReader(path).pages[:40]:
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total >= MAX_CHARS:
                break
        text = "\n".join(parts)[:MAX_CHARS]
        return text if text.strip() or not ocr_available() else ocr_pdf(path)
    if ext == "docx":
        import docx

        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" ".join(c.text for c in row.cells))
        return "\n".join(parts)[:MAX_CHARS]
    with open(path, "rb") as f:
        raw = f.read(MAX_CHARS * 4)
    if b"\0" in raw[:2048]:
        return ""
    return raw.decode("utf-8", "ignore")[:MAX_CHARS]


def run_pending(conn, progress=None, should_stop=lambda: False, batch: int = 25, limit: int | None = None) -> int:
    """Extract text for files with extracted=0. Returns number processed."""
    cache, done = {}, 0
    while not should_stop():
        rows = conn.execute(
            "SELECT id,source_id,path,ext,size FROM files WHERE extracted=0 LIMIT ?", (batch,)
        ).fetchall()
        if not rows:
            break
        for r in rows:
            if should_stop():
                break
            body, status = "", 2
            if r["size"] <= MAX_BYTES:
                try:
                    src = cache.get(r["source_id"]) or cache.setdefault(r["source_id"], srcmod.load(conn, r["source_id"]))
                    with src.fetch(r["path"]) as local:
                        body = read_text(local, r["ext"])
                    body = body.encode("utf-8", "ignore").decode("utf-8")  # drop lone surrogates (bad PDFs) that crash sqlite
                    status = 1 if body.strip() else 2
                except Exception:
                    status = 3
            if body:
                conn.execute("UPDATE fts SET body=? WHERE rowid=?", (body, r["id"]))
            conn.execute("UPDATE files SET extracted=? WHERE id=?", (status, r["id"]))
            done += 1
        conn.commit()
        if progress:
            progress(done)
        if limit and done >= limit:
            break
    return done
