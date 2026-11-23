"""File classification helpers."""
import os

KINDS = {
    "document": ["pdf", "doc", "docx", "odt", "rtf", "txt", "md", "rst", "tex", "csv", "tsv", "xls", "xlsx", "ppt", "pptx", "epub", "html", "htm", "log", "gdoc", "gsheet", "gslides"],
    "code": ["py", "js", "ts", "jsx", "tsx", "java", "c", "cpp", "h", "hpp", "rs", "go", "rb", "php", "sh", "sql", "css", "json", "yml", "yaml", "toml", "xml", "ini", "cfg", "lua", "kt", "swift", "cs", "ipynb"],
    "image": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif", "heic", "svg"],
    "video": ["mp4", "mkv", "avi", "mov", "webm", "wmv", "flv"],
    "audio": ["mp3", "wav", "flac", "ogg", "m4a", "aac", "opus"],
    "archive": ["zip", "tar", "gz", "7z", "rar", "xz", "bz2", "zst", "iso"],
}
EXT2KIND = {e: k for k, exts in KINDS.items() for e in exts}

# extensions whose text we can read
TEXT_EXTS = set(["txt", "md", "rst", "tex", "csv", "tsv", "html", "htm", "log", "json", "yml", "yaml", "xml", "ini", "cfg", "toml"]) | set(KINDS["code"]) - {"ipynb"}
EXTRACTABLE = TEXT_EXTS | {"pdf", "docx", "gdoc", "gsheet", "gslides"}

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv", ".uv-cache", ".mypy_cache", ".pytest_cache",
               # credentials / app state / package stores: never index these when scanning a whole home folder
               ".ssh", ".gnupg", ".pki", ".aws", ".kube", ".docker", ".config", ".local", ".var", ".claude", ".mozilla",
               ".npm", ".cargo", ".rustup", ".pycache", "site-packages", "dist-packages", "Trash", ".Trash",
               # Windows equivalents: OS / app data / package stores, so scanning a whole drive stays sane
               "appdata", "application data", "windows", "program files", "program files (x86)", "programdata",
               "$recycle.bin", "system volume information", "perflogs", "recovery", "msocache", "installer",
               "onedrivetemp", "config.msi", "windows.old"}
IGNORE_DIRS_LOWER = {d.lower() for d in IGNORE_DIRS}
PSEUDO_FS = {"/proc", "/sys", "/dev", "/run"}


def ext_of(name: str) -> str:
    return os.path.splitext(name)[1].lstrip(".").lower()


def like_esc(prefix: str, sep: str | None = None) -> tuple[str, str]:
    """LIKE patterns (used with `ESCAPE '\\'`) for everything directly under / nested below `prefix`.
    Returns (direct_children, nested). Works with '/' and '\\' separators so it is safe on Windows too."""
    esc = "\\"
    p = prefix.rstrip("/\\").replace(esc, esc * 2).replace("%", esc + "%").replace("_", esc + "_")
    s = (sep or os.sep).replace(esc, esc * 2)
    base = p + s
    return base + "%", base + "%" + s + "%"


def classify(ext: str) -> str:
    return EXT2KIND.get(ext, "other")


def human_size(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
