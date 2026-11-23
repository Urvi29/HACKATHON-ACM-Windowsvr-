# ME3 File System Intelligence

AI-powered local search, organisation and cleanup for local storage. Private data is detected and redacted **on-device** before any non-sensitive metadata reaches a cloud LLM.

> **For AI agents:** read the "STATUS" and "NEXT STEPS" sections first. After EVERY change, update this file (see `CLAUDE.md`).

---

## STATUS (update every change)

- **Last updated:** 2026-11-23 (Windows port + saved searches + scheduled cleanup reports + OCR verified on Windows + turnkey Google OAuth discovery (`me3 auth-check`); real-world run on `C:\Users\<you>\Downloads`; 39 tests pass / 3 skipped on Windows)
- **Phase:** 2 - all three brief requirements implemented and tested: (1) efficient live index, (2) Google Drive connector (SFTP removed on purpose: no data may leave the machine to arbitrary hosts), (3) search by meaning (text + images) with type/date/size filters; plus hybrid privacy layer, cleanup, AI organisation suggestions, saved searches and scheduled cleanup reports.
- **Run it:** `cd ~/Desktop/AquaFlare/me3-fsi && source env.sh && python -m me3 serve --port 8765` then open http://127.0.0.1:8765 (or `make run`). Small demo: `python scripts/make_demo.py && ME3_DB=$PWD/data/demo.db python -m me3 index data/demo && ME3_DB=$PWD/data/demo.db python -m me3 serve --port 8766`.
  **Windows (PowerShell):** `. .\env.ps1` then `python -m me3 serve --port 8765` (see SETUP COMMANDS for the one-time install). The web UI still needs a Google OAuth Desktop client file (see "WHAT YOU NEED TO DO" / "Cannot work without extra setup").
- **Tests:** `python -m pytest -q tests` -> 39 pass, 3 skipped on Windows (the 3 skipped assert freedesktop.org Trash behaviour, which only exists on Linux/BSD; Windows uses the Recycle Bin instead and has its own test). On Linux the same suite runs via `make test`, with the Recycle-Bin test skipped instead.
- **Working:**
  - Index: incremental crawler, inotify live watcher (polling fallback; remote sources rescan every 120s), low priority threads, text extraction txt/code/pdf/docx/gdoc into FTS5.
  - Search: hybrid = FTS5 keywords + MiniLM text meaning + CLIP image meaning, fused with RRF; local NL parser (`nlq.py`) extracts type/size/date ("last semester", "photos", "larger than 5MB", "in March 2024"); synonym expansion for everyday document words; thumbnails in UI.
  - Connectors: Local, Google Drive (live: OAuth done, source added).
  - Privacy: `privacy.py` (Presidio + PAN/Aadhaar/IFSC/UPI/phone/Luhn cards/secrets), `prepare_payload` builds the only thing that can reach `llm.py`; UI shows the exact redacted payload and requires an explicit send click; works without an API key (on-device rule-based ideas).
  - Disks panel (sidebar): every physical disk with used/free and a bar of what fills it by file type + "Apps & system" (OS folders, swap, hidden home folders) + hatched "Reserved & unreadable" (fs reserve, root-only folders) + free; biggest folders; Trash size. Linux: `lsblk` + statvfs; Windows: Win32 `GetLogicalDrives`/`GetVolumeInformationW` + `shutil.disk_usage`. `disks.py` scans each mounted filesystem in a low-priority thread (nice 10 / idle I/O on Linux), cached in data/disks.json, auto-rescans after 1 h, Rescan button. API: GET /api/disks, POST /api/disks/rescan.
  - Cleanup: duplicates (size -> partial hash -> full hash), large files, stale media, reversible trash (Windows: Recycle Bin via `send2trash`; Linux: freedesktop.org Trash spec, restorable from the file manager), organisation ideas.
  - Saved searches: name a query and its filters ("Save search" in the Search tab) and re-run it with one click; stored in `saved_searches`, so they survive restarts. `saved.py`. API: GET/POST /api/saved_searches, DELETE /api/saved_searches/{id}.
  - Scheduled cleanup report: a background thread (`reports.py`, every `ME3_REPORT_HOURS`, default 24 h) writes a **non-destructive** summary of reclaimable space (duplicate waste, files > 50 MB, old media, idea count) into `reports` and keeps the last 30. The Cleanup tab shows the latest report, the earlier ones, when the next one is due, and a "Run now" button. Nothing is ever deleted or moved. API: GET /api/cleanup/report, POST /api/cleanup/report/run.
- **Benchmarks:** 100k synthetic files (2.1GB): crawl 2.2s, text extraction 62k files 5.3s, keyword search ~5ms, hybrid search 13-30ms warm (first query ~3s model load), duplicate analysis ~8s. Background embedding of the 100k set runs at ~30 files/s (about 30 min for 62k texts) while the app stays usable.
- **Known issues / limits:**
  - Text embedding model is all-MiniLM-L6-v2 (weak on short abstract queries); a synonym lexicon (`nlq.SYNONYMS`) compensates. A stronger model (`BAAI/bge-small-en-v1.5`, set `ME3_TEXT_MODEL`, needs query prefix handling in embed.py) was not tested: download stalled.
  - Image meaning is only verified on synthetic cartoon scenes (`scripts/make_demo.py`), not real photo libraries. Synthetic 100k "images" are random bytes so they fail to embed (expected).
  - Google Drive: connector + OAuth client are complete, and the client JSON is now auto-discovered (project root, Downloads or Desktop; `$ME3_GOOGLE_CLIENT` overrides), but **no client JSON is present on this machine yet**, so sign-in/Drive still need the user to create a Desktop OAuth client (see "WHAT YOU NEED TO DO" 1-2). `python -m me3 auth-check` reports exactly what is missing.
  - Cloud AI: now Gemini by default (`GEMINI_API_KEY` in .env; `gemini-3.5-flash-lite`, fallbacks on 503/404/timeout; override with ME3_GEMINI_MODEL or ME3_LLM_PROVIDER=anthropic). Verified live via /api/suggest. Anthropic path kept but that account has no credits.
  - OCR is wired (extract.py) and turns on automatically once Tesseract is installed - **verified on Windows** (`winget install UB-Mannheim.TesseractOCR` + `oschwartz10612.Poppler`; `extract.py` also probes `C:\Program Files\Tesseract-OCR` because that installer does not add itself to PATH). Linux: `pacman -S tesseract tesseract-data-eng poppler`. Images and scanned PDFs are OCR'd.
  - Real folder test: ~/Pictures (51 screenshots) indexed+embedded in 17s into data/real.db; search works. Cloud LLM path (`llm.py`) untested without an API key.
  - Real `~` scan and inotify watch limits untested on a large real tree.
  - Do NOT use `pkill -f` with text that matches your own command (kills the shell); find the server with `ss -ltnp | grep :8765`.
  - Do not `source` a different venv: this project is venv-free by design.
- **Deadline:** 7-hour build window, started 2026-11-23 (~09:30).

## NEXT STEPS (ordered; the top item is what to do next)

0. [ ] **Check the redesigned UI while signed in**: all 3 themes, search + filters + load more + grid, add/remove source, cleanup trash (now goes to the desktop Trash: confirm it appears in the file manager and Restore works), Disks panel (expand Biggest folders, Rescan), organise send. It was only rendered headless against a mock API (same response shapes) plus JS syntax / id checks.
1. [x] Core index, watcher, extraction, FTS, NL parser, API, UI, tests.
2. [x] Privacy layer + LLM layer (preview-then-send) + cleanup + organisation ideas.
3. [x] Google Drive connector (SFTP removed). 4. [x] Torch, MiniLM + CLIP embeddings, hybrid search, thumbnails.
4. [x] **Windows port**: `env.ps1`, Windows disks (`_windows_hardware`, `shutil.disk_usage`, real `os.stat` for st_dev/st_ino/st_nlink), Recycle Bin trash (`send2trash`), no `nice` on Windows, separator-safe LIKE, Windows ignore dirs; deps + models installed into `./libs`/`./models`; 39 pass / 3 skipped; server + CLI smoke test OK. See "PLATFORM SUPPORT".
5. [~] **Real-world validation**: ~/Pictures done on Linux; ~/Documents and long-running inotify still to try there. **On Windows: DONE** — background-indexed `C:\Users\<you>\Downloads` (15,222 files crawled in 4.2 s, 8,773 extracted in ~8 min) and confirmed CLI search by meaning ("invoice receipt" -> 39 real bills/receipts, "screenshot" -> 922 images); the new `saved_searches`/`reports` tables and `reports.generate()` verified against the live `data/me3.db`.
6. [ ] **Google Drive live test**: no `client_secret_*.json` / `credentials.json` is present yet (and no `token.json`, so no consent has happened). Create a Google Cloud OAuth **Desktop** client (enable the Drive API, add your account as a Test user), then drop the downloaded JSON in the project root **or** leave it in Downloads and run `python -m me3 auth-check` to confirm it is found, followed by `python -m me3 gdrive-auth` and `python -m me3 add-gdrive`. Verify walk/fetch/export of Docs. Consider Drive's Changes API instead of full rescans.
7. [ ] **Cloud LLM live test**: `.env` key is DONE (detected). Remaining: verify `/api/suggest` with `send=true`; consider LLM query-understanding fallback (redacted).
8. [~] Embedding model upgrade experiment (bge-small / mpnet) with eval queries in tests; **OCR wired and verified on Windows** (Tesseract 5.4 + poppler via winget; `extract.py` auto-detects, incl. the default install folder). Remaining: eval the embedding upgrade.
9. [~] Extra: **saved searches** (`saved.py`, API + UI, tests) and **scheduled cleanup reports** (`reports.py`, API + UI, tests) **done** — see "Working"; file preview / open-folder / show-in-folder **done** (see CHANGELOG). Remaining: SMB connector (`smbprotocol`).
10. [ ] Final polish: visually check `docs/ME3_Project_Guide.pptx` once LibreOffice is installed; screenshots, demo script, README walkthrough, `make build` wheel.

## SETUP COMMANDS

**No virtual environment.** Libraries live in `./libs` (installed with `uv pip install --target`), models in `./models`, bytecode in `./.pycache`, uv cache in `./.uv-cache`. Always `source env.sh` first. Everything stays under `~/Desktop/AquaFlare/me3-fsi`. Python is the system 3.14 (torch 2.15 CPU wheels work).

Already installed (for reproducing on a fresh machine):

```bash
cd ~/Desktop/AquaFlare/me3-fsi && source env.sh
uv pip install --python /usr/bin/python --target ./libs fastapi "uvicorn[standard]" watchdog aiosqlite python-multipart jinja2 python-dotenv rich typer \
  pypdf python-docx python-magic pillow pytesseract sqlite-vec numpy presidio-analyzer presidio-anonymizer spacy \
  google-api-python-client google-auth-oauthlib anthropic pytest ruff
uv pip install --python /usr/bin/python --target ./libs torch --index-url https://download.pytorch.org/whl/cpu
uv pip install --python /usr/bin/python --target ./libs sentence-transformers
uv pip install --python /usr/bin/python --target ./libs https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
# one-time model download (then env.sh sets HF_HUB_OFFLINE=1; unset it only to download new models)
HF_HUB_OFFLINE=0 python -c "from sentence_transformers import SentenceTransformer as S; S('all-MiniLM-L6-v2'); S('clip-ViT-B-32')"
```

Optional system packages: Linux `sudo pacman -S --needed tesseract tesseract-data-eng poppler`; Windows `winget install UB-Mannheim.TesseractOCR` and `winget install oschwartz10612.Poppler`. OCR is wired in (`extract.py`); without the binaries it is simply skipped.

### Windows (PowerShell)

`uv` is not required: this port installs the same libraries into `./libs` with plain `pip --target`. Run these from the project root.

```powershell
python -m pip install --target .\libs --no-warn-script-location fastapi "uvicorn[standard]" watchdog python-multipart jinja2 python-dotenv rich typer `
  pypdf python-docx pillow pytesseract sqlite-vec numpy presidio-analyzer presidio-anonymizer spacy `
  google-api-python-client google-auth-oauthlib anthropic pytest ruff send2trash
python -m pip install --target .\libs --no-warn-script-location torch --index-url https://download.pytorch.org/whl/cpu
python -m pip install --target .\libs --no-warn-script-location sentence-transformers
python -m pip install --target .\libs --no-warn-script-location https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
# one-time model download (env.ps1 sets HF_HUB_OFFLINE=1; turn it off only to download new models)
. .\env.ps1; $env:HF_HUB_OFFLINE="0"
python -c "from sentence_transformers import SentenceTransformer as S; S('all-MiniLM-L6-v2'); S('clip-ViT-B-32')"
```

Then, in every session: `. .\env.ps1` (the Windows equivalent of `source env.sh`) and run the app:

```powershell
python -m me3 serve --port 8765     # http://127.0.0.1:8765
python -m pytest -q tests           # 29 pass, 3 skipped (POSIX-only Trash tests)
```

Notes:
- `env.ps1` puts `./libs` and `./src` on `PYTHONPATH`, caches models in `./models` and bytecode in `./.pycache`. It does **not** need Developer Mode.
- OCR on Windows: `winget install UB-Mannheim.TesseractOCR` (add `winget install oschwartz10612.Poppler` for scanned PDFs). `extract.py` finds `tesseract.exe` on `PATH` or in the default `C:\Program Files\Tesseract-OCR` folder, so no PATH edit is needed (no `nice` prefix - that is Linux/macOS only).
- The `Makefile` uses `/bin/bash` and `uv`, so it is Linux/macOS only. On Windows run the `python -m ...` commands directly (above).

Cloud LLM (optional): create `.env` with `ANTHROPIC_API_KEY=...` (gitignored). Model default `claude-haiku-5-5`, override `ME3_LLM_MODEL`.

Google Drive: see docstring of `src/me3/connectors/gdrive.py` (Cloud Console -> Drive API -> OAuth Desktop client -> `credentials.json` in project root -> `python -m me3 gdrive-auth` -> `python -m me3 add-gdrive`).

## WHAT YOU NEED TO DO (user-side setup)

The pip steps above install the Python libraries only. These four things are yours to do; only the first is needed to use the browser UI, the rest are optional (the app degrades gracefully without them).

1. **Google sign-in for the web UI — required for the browser UI.**
   - Google Cloud Console -> APIs & Services -> Credentials -> Create credentials -> OAuth client ID -> application type **Desktop app**.
   - Download the JSON and put it in the project root. Any of these names work: `credentials.json`, the auto-downloaded `client_secret_*.json` (found automatically), or point at it with `ME3_GOOGLE_CLIENT`.
   - It is gitignored — **never commit it.**
   - Start the app, open http://127.0.0.1:8765, sign in. The **first** Google account to sign in becomes the owner; to restrict access instead, set `ME3_ALLOWED_EMAILS=a@example.com,b@example.com` before first sign-in.
2. **Google Drive connector — optional.** Same OAuth client file, then a one-time browser consent:
   `python -m me3 gdrive-auth` followed by `python -m me3 add-gdrive`.
3. **Cloud AI — optional.** Create `.env` in the project root with `GEMINI_API_KEY=...` (default provider) or `ANTHROPIC_API_KEY=...`; override the provider with `ME3_LLM_PROVIDER`. Without a key the app still works fully offline (rule-based organisation ideas).
4. **OCR of scanned PDFs / images - optional.** Windows: `winget install UB-Mannheim.TesseractOCR` (and `winget install oschwartz10612.Poppler` for scanned PDFs). Linux: `sudo pacman -S --needed tesseract tesseract-data-eng poppler`. `extract.py` turns OCR on automatically (it checks `PATH` and the default Windows install folder), so no PATH edit is needed.

The complete list of things that cannot work without extra setup is in the next section.

## PLATFORM SUPPORT: Windows vs Linux

**Works on Windows** (verified): incremental index + live watcher (watchdog uses `ReadDirectoryChangesW`), text/PDF/DOCX extraction, MiniLM text + CLIP image embeddings, hybrid FTS5 + vector search, privacy redaction, cleanup (duplicates/large/stale/organise) and the Disks panel (Win32 API). End-to-end CLI smoke test on Windows: `index` -> 2 files extracted + embedded -> `search "something to cook for dinner"` returned the recipe file via meaning.

**POSIX-only features** (they degrade gracefully on Windows, nothing crashes):
| Feature | Linux / macOS | Windows behaviour |
| --- | --- | --- |
| Delete / "Move to trash" | freedesktop.org Trash (`~/.local/share/Trash`, restorable from the file manager) | Windows **Recycle Bin** via `send2trash` (restorable from Explorer) |
| Background thread priority | `os.setpriority` + `ioprio_set` idle I/O class | skipped (`os.setpriority` does not exist) — threads run at normal priority |
| Low-priority OCR subprocess | `nice -n 10 tesseract ...` | plain `tesseract ...` (`nice` is not present) |
| On-disk size accounting | `st_blocks * 512` (allocated bytes, matches `df`) | no `st_blocks` on Windows -> logical file size is used, so the Disks numbers are approximate; hard-link de-duplication still works (via `st_ino`) |
| `Makefile` targets (`make test/run/build`) | `bash` + `uv` | run the `python -m ...` commands directly (see above) |

**Cannot work without extra setup** (true on every OS, not a Windows limitation):
1. **Web UI sign-in:** requires a Google Cloud OAuth **Desktop** client JSON placed in the project root (`credentials.json` or `client_secret_*.json`). Without it `/login` cannot start the consent flow, so the browser UI is unusable (the CLI and the Python API still work).
2. **Google Drive connector:** same OAuth client file plus a one-time browser consent (`python -m me3 gdrive-auth`).
3. **OCR of scanned PDFs / images:** needs the external Tesseract binary (and poppler's `pdftoppm` for scanned PDFs). Not installed by the pip steps; install it separately (`winget install UB-Mannheim.TesseractOCR` / `oschwartz10612.Poppler` on Windows; `pacman -S tesseract tesseract-data-eng poppler` on Linux). Without it OCR is simply skipped.
4. **Cloud LLM (optional):** needs an API key in `.env` (`ANTHROPIC_API_KEY` / `GEMINI_API_KEY`). Not required — the app works fully offline.
5. **SFTP connector:** removed from the project on purpose (privacy); only Local + Google Drive remain.

## NOTES

Presidio with the small model (no downloads):

```python
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
nlp = NlpEngineProvider(nlp_configuration={"nlp_engine_name":"spacy","models":[{"lang_code":"en","model_name":"en_core_web_sm"}]}).create_engine()
analyzer = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
```

`uv` cache lives in `./.uv-cache` (set in env.sh). sqlite-vec: `db.enable_load_extension(True); sqlite_vec.load(db)`.

## ARCHITECTURE OVERVIEW

Modular monolith, one Python process (FastAPI) with background worker threads, one SQLite file (`data/me3.db`).

```
 Web UI (HTML/JS) --HTTP--> FastAPI (api.py)
                               |
     +-------------+-----------+-------------+----------------+
     | Search      | Cleanup   | Privacy gate| Source registry |
     | FTS5+vector | dupes,    | Presidio +  | Local |      |  |
     | + filters   | large,    | regex redact| GDrive (same    |
     |             | org. ideas| -> LLM      | Source interface)|
     +------+------+-----+-----+------+------+--------+--------+
            |            |            |               |
            +------ Indexing pipeline (background, low priority) ------+
              crawl/watch -> queue -> extract -> embed -> SQLite (files, fts5, sqlite-vec)
```

Key decisions:
- Hybrid privacy: raw content never leaves the device; only redacted metadata/snippets reach the cloud LLM (`privacy.py` is the only path to `llm.py`).
- Connectors implement one `Source` interface (`list`, `stat`, `open`, `watch`), so the indexer is source-agnostic.
- Tiered indexing: metadata + filename first (fast, covers all 100k files), then text extraction, then embeddings lazily in the background.
- Hybrid search: FTS5 keyword score + sqlite-vec cosine similarity, merged by rank fusion, then filtered by type/date/size.
- Local-first embeddings (MiniLM for text, CLIP for images); the LLM only parses queries ("last semester" -> date range) and suggests organisation, on redacted data.

## MODULE LAYOUT (all built)

```
src/me3/
  db.py          SQLite schema (sources, files, fts5); thread-local connections
  crawler.py     incremental scan
  watcher.py     watchdog live updates
  extract.py     text/OCR extraction
  embed.py       text + CLIP embeddings
  search.py      hybrid FTS + vector search, filters
  privacy.py     PII detection/redaction before cloud calls
  llm.py         cloud LLM client (receives redacted data only)
  cleanup.py     duplicates, large files, organisation suggestions
  connectors/    local.py, gdrive.py (common Source interface)
  api.py         FastAPI app + static UI
scripts/         gen_data.py, benchmark.py
tests/
```

## CHANGELOG (newest first)
- 2026-11-23: **Turnkey Google OAuth client discovery.** `gdrive.find_client_secret()` now scans `$ME3_GOOGLE_CLIENT` (a file *or* a folder), the project root, **Downloads** and **Desktop** for `credentials.json` / `client_secret_*.json`, validates each is a **Desktop-app** client (a **Web** client is rejected with a clear fix), and reports every location it looked in when it finds nothing. New `python -m me3 auth-check` prints the client file it found, its project id, and whether the stored token + Drive API work. So downloading the JSON from the console and leaving it in Downloads is enough - no copying needed. 4 new tests in `tests/test_connectors.py` (39 pass, 3 skipped; ruff clean).
- 2026-11-23: **OCR fixed on Windows.** Cause: the UB-Mannheim Tesseract installer does not add `tesseract.exe` to `PATH`, so `shutil.which("tesseract")` failed and OCR silently stayed off. Fix: `extract.py` now resolves external tools via a `_bin()` helper (`$ME3_TESSERACT` -> `PATH` -> `C:\Program Files\Tesseract-OCR\tesseract.exe`), so a plain `winget install UB-Mannheim.TesseractOCR` is enough (no PATH edit). Installed Tesseract 5.4 + poppler 26.09 via `winget` and verified: a generated "INVOICE 42" image OCRs correctly. README's Arch-only `sudo pacman` OCR lines replaced with Windows `winget` commands.
- 2026-11-23: **Saved searches + scheduled cleanup reports (Windows real-world run).** `saved.py`: name a query + its filters, re-run with one click; table `saved_searches`, API `GET/POST /api/saved_searches`, `DELETE /api/saved_searches/{id}`, UI "Save search" row in the Search tab, `tests/test_saved.py`. `reports.py`: a background thread writes a **non-destructive** cleanup summary (duplicate waste, files > 50 MB, stale media, idea count) every `ME3_REPORT_HOURS` (default 24 h) into `reports` (keeps the last 30); Cleanup tab shows latest/earlier/next-due + "Run now"; API `GET /api/cleanup/report`, `POST /api/cleanup/report/run`; nothing is ever deleted or moved, `tests/test_reports.py`. `db.py` schema extended; `api.py` starts/stops the report thread in `lifespan`. README gained the "WHAT YOU NEED TO DO" user-side setup section. Verified end-to-end on Windows against the live index of `C:\Users\<you>\Downloads` (15,222 files; CLI search "invoice receipt" -> 39, "screenshot" -> 922; both new tables + `reports.generate()` confirmed in `data/me3.db`). 39 pass, 3 skipped; ruff clean.
- 2026-11-23: **Windows port.** New `env.ps1` (PowerShell `source env.sh`). `disks.py`: Windows hardware via Win32 `GetLogicalDrives`/`GetDriveTypeW`/`GetVolumeInformationW`, `shutil.disk_usage` for free space, `$Recycle.Bin` trash, real `os.stat` instead of `DirEntry.stat` (which reports st_dev/st_ino/st_nlink as 0 on Windows, breaking the mount check and hard-link de-dup), `st_blocks` fallback to size, `_lower_priority` no-op, `os.path.join` folder paths. `cleanup.py`: Recycle Bin via `send2trash` (Linux keeps the freedesktop Trash spec). `extract.py`: no `nice` prefix when `nice` is absent. `util.py`: Windows ignore dirs + separator-safe `like_esc` (used in `crawler.py`, `api.py`). `pyproject.toml`: add `send2trash`. Tests: trash tests split POSIX/Windows, disks + duplicates tests made separator/symlink/hardlink safe. Deps + MiniLM/CLIP models installed into `./libs`/`./models`; `pytest` 29 pass, 3 skipped; server + end-to-end CLI index/search verified. `Makefile` remains Linux/macOS-only (bash+uv).
- 2026-11-23: Disks panel: all disks + partitions, used/free, what fills each disk by type (new 'system' kind colour --k-system, validated vs every type colour: CVD dE>=8, normal dE>=15, contrast>=3:1 on dark/royal/light), biggest folders, Trash size; src/me3/disks.py, /api/disks, tests/test_disks.py. Rendered and checked in all 3 themes (headless Firefox).
- 2026-11-23: "Move to trash" now uses the real desktop Trash (~/.local/share/Trash, or $topdir/.Trash-$uid on other drives) with .trashinfo files, so trashed files appear in the file manager and Restore works; was a private data/trash/<time>/ folder the user could not see. UI toasts now report files that could not be moved. Old data/trash/ (2 files from earlier) left in place. Test: tests/test_trash.py.
- 2026-11-23: Organise fixes: (1) AI reply parsing is robust: Ollama uses a JSON schema (structured output), replies cut off mid-list keep their complete suggestions, one automatic retry, clear error naming the on-device AI instead of 'Cloud AI call failed (JSONDecodeError)'; (2) no harmful advice: rule ideas skip program/game/system folders, ML datasets (images paired with label files) and code projects (cleanup.folder_role); program files are left out of the AI sample and the prompt forbids moving them; (3) step-2 badge readable on the blue button; AI card shows target folder + example files. Tests added.
- 2026-11-23: duplicates: every group is listed (no paging/top-N); files under 1 KB and over 512 MB are no longer skipped (931 groups on the real index, was 901); Google Drive duplicates found via Drive's md5Checksum (stored as hash 'md5:...', backfilled on rescan) and Google Docs compared by their text (shown as 'likely'); Drive copies can't be trashed (read-only), the UI says to delete them in Drive. Empty (0-byte) files are still excluded. Test: tests/test_duplicates.py.
- 2026-11-23: Cleanup duplicates: all groups are reachable now (was top 20 by size only, so small image duplicates ranked #125/#459 were hidden): type filter pills with counts, Show more paging, image thumbnails per copy; result cached until the index changes (GET /api/cleanup/duplicates?kind=&offset=&fresh=). Viewer: any file type can be inspected (GET /api/files/{id}/peek): what it is, detected format, text inside Office files (xlsx/pptx/docx/odt), archive contents, SHA-1, readable strings and a hex view.
- 2026-11-23: charts: new Overview tab (now the landing tab) with KPI tiles, storage-by-type bar (click a type to search it), monthly activity columns (files/size toggle + table view), biggest folders, file-size ranges, sources and index-health meters; sidebar 'space by type' bar; Cleanup compares reclaimable space with the whole index. Plain HTML/SVG, no chart library (works offline). Type colours switched to a colour-blind-checked palette (validated light/dark/royal). New endpoint GET /api/insights (~40 ms on 8.5k files).
- 2026-11-23: look-before-you-trash: file viewer (images, PDF, video, audio, text) with Open / Show in folder / Copy path; Cleanup 'Compare copies' shows each duplicate side by side with the SHA-1 match and lets you pick which copy to keep; View buttons on every cleanup row, search result and Organise idea ('View files' lists that folder). New endpoints: GET /api/files/{id}, /api/files/{id}/raw (HTML/SVG/JS sent as plain text, sandbox CSP), POST /api/files/{id}/open, GET /api/folder.
- 2026-11-23: privacy: password detection widened ("my password is x", "pass: x", "PIN 1234", "OTP is x", "password for gmail is x", Google/Slack/GitHub tokens); only the value is replaced so the keyword stays readable; normal sentences untouched; test added.
- 2026-11-23: UI redesign (static/index.html rewritten, login.html themed): light/dark/royal themes (saved in localStorage, follows OS on first visit); type pills + collapsible filters with removable chips; example queries; stale-response guard; Load more paging; list/grid view; image preview (/api/thumb?size=); copy path; toasts + confirm dialogs instead of alert/confirm; Add-source panel; live privacy redaction; tab kept in URL hash; '/' focuses search. Fixed: Add source threw after success (referenced removed SFTP fields); Organise note claimed on-device even with a cloud model. All API calls unchanged.
- 2026-11-23: REMOVED SFTP entirely (connector, UI tab, API branch, add-sftp CLI, tests, paramiko dep): user decision, security/privacy. Local + Google Drive only.
- 2026-11-23: Drive tab now lets you index a specific Drive folder (name or ID; blank = whole Drive); multiple Drive sources allowed, overlap with whole-Drive rejected; unknown folder fails instead of wiping the index.
- 2026-11-23: Drive tab hides the name box and Add button unless Drive is authorised but not yet added.
- 2026-11-23: Drive tab status reworded (was mistaken for an error); Add disabled while Drive is already connected.
- 2026-11-23: SFTP: unreadable root now raises (an empty scan used to wipe the source index); ssh-agent/default keys used when no password/key given; ~ expanded in key path + missing-key error; same folder on different hosts no longer collapses into one source; UI validation + clearer placeholders. Live sshd tests pass (3).
- 2026-11-23: Drive tab no longer shows the stale "needs credentials.json" note: /api/gdrive/status reports client file/authorised/added, and a Connect button runs the Google consent in-UI using the same client_secret_*.json.
- 2026-11-23: Sources panel: reject local folders nested in / containing an existing source (duplicate results); added remove source (DELETE /api/sources/{id}, index only, files untouched) + rescan/remove feedback.
- 2026-11-23: removed the AI vision button, /api/search/vision and llm.vision_match (unused; normal search + filters already find images by meaning).
- 2026-11-23: AI vision button is hidden when a non-image type is selected and labelled "in images"; result header says images only.
- 2026-11-23: AI vision search now honours the size/date filters from the UI (was ignoring them: showed 125KB screenshots under a >=100MB filter). UI sends lastFilters; api.py merges them over parsed ones. Needs server restart.
- 2026-11-23: search filters verified against the index (type/size/date/ext, NL + explicit, 0 violations); fixed UI date filters using UTC midnight instead of local (missed files modified 00:00-05:30 IST, chip dates shifted).

- 2026-11-23: Web UI now requires Google sign-in (src/me3/auth.py, static/login.html; signed cookie; first account to sign in is owner, or ME3_ALLOWED_EMAILS). Gemini LLM provider added. Replaced synthetic 100k + SFTP test data with real sources: home folder (credential/config dirs excluded via util.IGNORE_DIRS) and Google Drive. DB backup of the pre-cleanup state: data/me3.db.bak-before-cleanup (delete when happy). Regenerate fake data with scripts/gen_data.py if needed. 20 tests pass.
- 2026-11-23: Fixed/hardened: gdrive finds client_secret_*.json + prints consent URL + clear token errors; /api/suggest maps Anthropic errors (401 key type, 402 no credits); optional OCR (images + scanned PDFs) auto-enabled when tesseract exists; test added (18 pass); validated real ~/Pictures indexing.
- 2026-11-23: Recorded in STATUS that Google auth is not working (user report); needs debugging with the actual error.
- 2026-11-23: Added docs/ME3_Project_Presentation.pptx (17 slides: project, architecture, libraries, algorithms, user manual); built with pptxgenjs from a scratchpad script outside the project. Not visually rendered (no LibreOffice installed).
- 2026-11-23: Added `docs/ME3_Project_Guide.pptx` (22 slides: project, architecture, libraries, algorithms, user manual) generated by `scripts/build_deck.js` (pptxgenjs; needs `npm install pptxgenjs react-icons react react-dom sharp` outside the project, run with NODE_PATH, args: output path). Schema-validated and text-fit checked; NOT visually rendered (no LibreOffice installed: `sudo pacman -S libreoffice-fresh` then convert to PDF and eyeball).
- 2026-11-23: Ran the project: server already up on :8765 (pid from 10:29), UI 200, stats/index/search/llm-status OK (100k synthetic files indexed, watcher inotify), 17 tests pass. User created `.env` with ANTHROPIC_API_KEY (gitignored, chmod 600); `/api/llm/status` reports cloud_available=true. Live `/api/suggest send=true` NOT yet run. Google `client_secret_*.json` (Desktop app, project aquaflare) sits untracked in project root; never commit it.
- 2026-11-23: Fixed lint error; restarted server and smoke-tested cleanup/privacy/suggest/llm-status endpoints (all respond); added tests/test_sftp.py (in-process paramiko server: scan+extract+search over SFTP passes). GDrive untested (needs OAuth).
- 2026-11-23: Made project buildable: pyproject.toml (setuptools, src layout, `me3` entry point, extras), static UI moved to src/me3/static (packaged), connectors/ is a package, Makefile (build/test/lint/run/clean), ruff config, lint clean; wheel verified to import standalone. Live watcher create/rename/delete re-verified.
- 2026-11-23: Phase 2: privacy.py, llm.py, cleanup.py, connectors (sftp, gdrive), embed.py (MiniLM+CLIP, sqlite-vec), hybrid RRF search, synonym lexicon, thumbnails, new UI (search/cleanup/organise/privacy tabs), make_demo.py, 17 tests. env.sh: bytecode cache in ./.pycache (DONTWRITEBYTECODE made torch imports take minutes), HF_HUB_OFFLINE=1.
- 2026-11-23: Phase 1 slice built: db, sources, crawler, extract, watcher, indexer, nlq, search, api, cli, static UI, gen_data, tests; benchmarked on 100k files; fixed watcher ignoring paths under data/.
- 2026-11-23: Project moved to ~/Desktop/AquaFlare/me3-fsi (all files must live here).
- 2026-11-23: Installed all non-torch deps into ./libs + en_core_web_sm; verified imports, sqlite-vec, FTS5, Presidio.
- 2026-11-23: Added env.sh.
- 2026-11-23: Dropped venv; deps go to ./libs via --target, models to ./models.
- 2026-11-23: Moved project to ~/Desktop/AquaFlare/me3-fsi (all files must live here).
- 2026-11-23: Created project, README.md, CLAUDE.md.
