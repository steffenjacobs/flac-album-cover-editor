# FLAC Album Cover Editor

A local web tool to find FLAC files that have **no album cover or a cover smaller
than 800×800**, and patch a high-quality cover into them — picked from candidates
fetched from free album-art APIs (iTunes, Deezer, Cover Art Archive).

It scans a folder tree (works directly on an SMB share like
`\\192.168.1.147\Music\Music HQ`), groups the problem files **by folder**, shows
~5–12 candidate covers per folder, and on "Patch metadata" embeds the chosen
image as a JPEG front cover into **every track in that folder**.

## Why it's a "local web app" (not just a web page)

A browser cannot read your filesystem or a network share — it's sandboxed. So
this is a small **Python backend** (FastAPI) that does all the file/FLAC/image
work and also serves the UI on `http://127.0.0.1:8765`. Your browser is just the
front end, talking to the backend on localhost (same origin → no CORS).

> Run it from your own logged-in Windows session so it inherits your access to
> the SMB share. Don't run it as a Windows service (SMB sessions are per-logon).

## Quick start

Double-click **`start.bat`** (first run creates a venv and installs deps), then
the UI opens at <http://127.0.0.1:8765>.

Manual:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe server.py
```

Then open <http://127.0.0.1:8765>.

## Using it

1. Set the **Music folder** (defaults to `\\192.168.1.147\Music\Music HQ`) and
   the **Min size** (default 800 px). Leave **Backup (.bak)** ticked to keep a
   copy of each file before it's modified.
2. Click **Scan library**. A progress bar shows files scanned.
3. Each flagged folder shows its current cover (if any), the problem tracks, and
   a **Find covers** button.
4. Click **Find covers**, pick one of the candidate images, then **Patch
   metadata**. The chosen cover is downscaled to ≤1200×1200 JPEG and embedded
   into every FLAC in that folder. If none fit, click **Find more covers** to
   page in additional results from all three sources (deduplicated), or
   **Search again** to start over.

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
also change them in the UI; they persist between runs.

## Restoring originals

If **Backup (.bak)** was on, each modified file has a sibling `<name>.flac.bak`.
To undo, delete the `.flac` and rename the `.bak` back. To reclaim space once
you're happy, delete the `.bak` files.

## Tests

```powershell
.venv\Scripts\python.exe tests\test_core.py
```

Builds synthetic spec-correct FLAC files and verifies tag reading, true-dimension
detection (ignoring lying stored fields), status classification, JPEG
re-encoding, and the write round-trip.

## Project layout

```
server.py        FastAPI app: API + serves the UI
scanner.py       Fast FLAC metadata/cover parser + library scan
art_sources.py   iTunes / Deezer / Cover Art Archive lookups
patcher.py       JPEG re-encode + embed cover (mutagen)
static/          index.html, app.js, style.css  (the UI)
tests/           self-contained core tests
start.bat        Windows launcher (venv + run)
```

## Notes / limits

- Grouping is **per folder** (matches typical one-album-per-folder libraries).
  Multi-album folders or "Various Artists" compilations get one query from the
  majority tags.
- Patching writes the chosen cover to **all** tracks in the folder so the album
  is consistent.
- The album-art APIs are free for personal tagging; they don't grant rights to
  redistribute the images.
