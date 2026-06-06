# FLAC Album Cover Editor

A local web tool to find FLAC files that have **no album cover or a cover smaller
than 800×800**, and patch a high-quality cover into them — picked from candidates
fetched from free album-art APIs (iTunes, Deezer, Cover Art Archive).

It scans a folder tree (works directly on an SMB share like
`\\192.168.1.147\Music\Music HQ`), groups the problem files **by folder**, and
lets you give each folder a cover — from the album-art APIs, a custom search
term, or an image you upload yourself — which it embeds as a JPEG front cover
into **every track in that folder**.

![Screenshot of the FLAC Album Cover Editor showing flagged folders with
candidate covers from iTunes/Deezer/Cover Art Archive, custom search, upload, and
patch controls](screenshot.jpg)

## Why it's a "local web app" (not just a web page)

A browser cannot read your filesystem or a network share — it's sandboxed. So
this is a small **Python backend** (FastAPI) that does all the file/FLAC/image
work and also serves the UI on `http://127.0.0.1:8765`. Your browser is just the
front end, talking to the backend on localhost (same origin → no CORS).

## Quick start (Docker — recommended)

The app runs in a container; the SMB share is mounted into it via a CIFS volume
declared in `docker-compose.yml` (Docker's Linux engine performs the mount, so a
Linux container can reach a Windows/NAS share — this works under Docker Desktop
on Windows too).

1. Copy the env template and fill in your share details/credentials:

   ```powershell
   copy .env.example .env
   ```

   `.env` (defaults already match `\\192.168.1.147\Music\Music HQ`):

   ```ini
   SMB_HOST=192.168.1.147
   SMB_SHARE=Music         # the share; mounted at /music in the container
   MUSIC_SUBDIR=Music HQ   # subfolder of the share to scan
   SMB_USER=guest          # or your username
   SMB_PASSWORD=           # password (no commas); empty for guest
   SMB_VERS=3.0            # try 2.1 / 1.0 if the mount fails
   MIN_SIZE=800
   ```

2. Bring it up:

   ```powershell
   docker compose up --build
   ```

3. Open <http://127.0.0.1:8765>. The "Music folder" is pre-set to the mounted
   share, so just click **Scan library**.

The UI port is published to `127.0.0.1` only, so it's reachable from your machine
only. To point at a plain local folder instead of SMB, replace the `music` volume
with a bind mount (`volumes: ["/path/to/music:/music"]`) and set `MUSIC_SUBDIR=`
empty.

> `.env` holds credentials and is git-ignored — never commit it.

## Run locally on Windows (without Docker)

If you'd rather run it natively:

1. **Install Python 3** from <https://www.python.org/downloads/> (tick *"Add
   python.exe to PATH"* in the installer).

2. **Open PowerShell in the project folder** — in File Explorer, Shift+right-click
   the `flac-album-cover-editor` folder and choose *"Open PowerShell window here"*
   (or `cd` into it).

3. **Create a virtual environment** (first time only):

   ```powershell
   py -3 -m venv .venv
   ```

4. **Install the dependencies** (first time only):

   ```powershell
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

5. **Start the server:**

   ```powershell
   .venv\Scripts\python.exe server.py
   ```

6. **Open the UI** at <http://127.0.0.1:8765>.

On later runs you only need steps 5–6.

The default music folder is `\\192.168.1.147\Music\Music HQ` (editable in the
UI). Run it from your own logged-in Windows session so it inherits your access to
the SMB share — don't run it as a Windows service (SMB sessions are per-logon).
You can override the defaults with the `MUSIC_ROOT`, `MIN_SIZE`, `HOST`, `PORT`,
and `SCAN_WORKERS` environment variables, e.g.:

```powershell
$env:MUSIC_ROOT = "D:\Music"; .venv\Scripts\python.exe server.py
```

## Using it

1. Click **Scan library** (the music folder and min size are pre-filled; under
   Docker they come from `.env`). A progress bar shows files scanned. Leave
   **Backup (.bak)** ticked to keep a copy of each file before it's modified.
2. Each flagged folder shows its current cover (if any) and the problem tracks.
   Get a new cover three ways:
   - **Find covers** — fetches candidates from iTunes / Deezer / Cover Art
     Archive using the folder's tags. **Find more covers** pages in additional
     results (deduplicated); **Search again** restarts.
   - **Custom search** — type any term (e.g. a different album/artist) to search
     the same services manually.
   - **Upload image** — pick a cover from your computer via the file chooser.
3. Click a candidate (or your upload) to select it, then **Patch metadata**. The
   image is downscaled to ≤1200×1200 JPEG and embedded into **every FLAC in that
   folder**.

## How it works (key technical points)

- **Cover detection** uses a lightweight FLAC metadata parser (`scanner.py`)
  that reads only block headers + a small image-header probe — so it does **not**
  pull every multi-MB cover over the network just to check sizes. It decodes the
  **real** image dimensions rather than trusting the FLAC PICTURE block's stored
  width/height, which RFC 9639 marks informational-only and which taggers often
  set to 0 or wrong values. (`mutagen` is the automatic fallback per file.)
- **Album art** comes from iTunes (artwork upgraded to 1200px), Deezer
  (`cover_xl`, 1000px), and Cover Art Archive via a MusicBrainz lookup. Queries
  use the FLAC `ARTIST`+`ALBUM` tags (folder name as fallback). Candidates are
  downloaded and dimension-probed by the backend, then proxied to the browser.
- **Patching** (`patcher.py`) re-encodes to JPEG, keeps it under ~4 MB (a
  Windows-11 Explorer display bug corrupts metadata above that; the hard FLAC
  block limit is 16 MiB), clears existing PICTURE blocks (writers *append*), and
  writes a type-3 front cover with correct dimensions via `mutagen`.

## Configuration

`config.json` (created on first scan) stores `music_root` and `min_size`. You can
also change them in the UI; they persist between runs. The `MUSIC_ROOT` and
`MIN_SIZE` environment variables take precedence over `config.json` (this is how
the Docker setup pins the in-container mount path).

**Scan speed:** files are read concurrently to hide per-file SMB round-trip
latency (the scan is latency-bound, not CPU-bound). `SCAN_WORKERS` sets the
number of parallel readers (default 8). On a fast wired LAN you can raise it
(e.g. 16–24) for a further speedup; on Wi-Fi/VPN or a signed SMB share, keep or
lower it. The result is identical regardless of the value.

## Restoring originals

If **Backup (.bak)** was on, each modified file has a sibling `<name>.flac.bak`.
To undo, delete the `.flac` and rename the `.bak` back. To reclaim space once
you're happy, delete the `.bak` files.

## Tests

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing
```

Hermetic pytest suite (no network, no real files touched): synthetic spec-correct
FLACs in temp dirs, mocked album-art HTTP, and FastAPI `TestClient` for every
endpoint. Covers ~97% of the code, including tag reading, true-dimension
detection (ignoring lying stored fields), status classification, JPEG
re-encoding, the atomic write round-trip, and the full API surface.

## Project layout

```
server.py            FastAPI app: API + serves the UI
scanner.py           Fast FLAC metadata/cover parser + library scan
art_sources.py       iTunes / Deezer / Cover Art Archive lookups
patcher.py           JPEG re-encode + embed cover (mutagen)
static/              index.html, app.js, style.css  (the UI)
tests/               pytest suite (scanner / patcher / art_sources / server)
Dockerfile           container image (python:3.13-slim-trixie)
docker-compose.yml   one-command run + CIFS share mount
requirements.txt     runtime deps (pinned)
requirements-dev.txt test/coverage deps
.env.example         template for SMB settings (copy to .env)
```

## Notes / limits

- Grouping is **per folder** (matches typical one-album-per-folder libraries).
  Multi-album folders or "Various Artists" compilations get one query from the
  majority tags.
- Patching writes the chosen cover to **all** tracks in the folder so the album
  is consistent.
- The album-art APIs are free for personal tagging; they don't grant rights to
  redistribute the images.

## License

This project's own code is released under the [MIT License](LICENSE).

Third-party runtime dependencies keep their own licenses: FastAPI/uvicorn/
requests (MIT/BSD) and Pillow (HPND) are permissive, but **mutagen is GPLv2+**.
Using it locally is unrestricted; if you redistribute a built artifact that
bundles mutagen (e.g. the Docker image), the usual GPL obligations apply to that
bundled copy (its source is freely available on PyPI/GitHub).
