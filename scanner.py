"""
Lightweight FLAC scanner.

Reads only what is needed from each FLAC file:
  - the VORBIS_COMMENT block (ALBUM / ARTIST / ALBUMARTIST tags)
  - the PICTURE block header + a small probe of the image bytes to get the
    TRUE pixel dimensions of the front cover.

This avoids transferring the entire (often multi-MB) embedded cover over an
SMB share for every track, which matters a lot on a network library.

If the fast parser hits anything unexpected for a given file, it falls back to
the battle-tested `mutagen` reader for that single file.

Cover-dimension truth: RFC 9639 says the width/height fields stored inside the
PICTURE block are *informational only* and are frequently 0 or wrong, so we
never trust them for the "< 800x800" check -- we decode the real image header.
"""

from __future__ import annotations

import os
import struct
import warnings
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

from PIL import Image

# Bound decoded pixels so a crafted image can't exhaust memory, while leaving
# ample headroom for legitimate album art (16k x 16k).
MAX_IMAGE_PIXELS = 256_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

# Block types (RFC 9639 sec 8.1)
BLOCK_VORBIS_COMMENT = 4
BLOCK_PICTURE = 6

# PICTURE content types (RFC 9639 Table 13)
PICTURE_TYPE_FRONT_COVER = 3
PICTURE_TYPE_ICONS = (1, 2)  # 32x32 file icon / other file icon

# Cover dimensions live in the image header (JPEG SOF / PNG IHDR), almost always
# within the first few KB. Read the cover in growing stages so the common case
# transfers ~16 KB over SMB instead of 128 KB, escalating only when needed.
HEADER_PROBE_STAGES = (16 * 1024, 256 * 1024)

# Per-file reads serialize over SMB latency, so scan files concurrently. Threads
# (not processes) suit this blocking, GIL-releasing network I/O. Tune via env.
DEFAULT_SCAN_WORKERS = 8

PROBLEM_STATUSES = {"missing", "too_small", "unknown", "error"}


def _scan_workers() -> int:
    try:
        return max(1, int(os.environ.get("SCAN_WORKERS", DEFAULT_SCAN_WORKERS)))
    except ValueError:
        return DEFAULT_SCAN_WORKERS


def _img_dims(buf: bytes) -> tuple[int | None, int | None]:
    """Return (width, height) from image header bytes, or (None, None)."""
    try:
        im = Image.open(BytesIO(buf))
        w, h = im.size
        if w and h:
            return int(w), int(h)
    except Exception:
        pass
    return None, None


def _parse_vorbis_comment(block: bytes) -> dict[str, list[str]]:
    """Parse a FLAC VORBIS_COMMENT block body. NOTE: vorbis comment integer
    lengths are LITTLE-endian (unlike the rest of FLAC metadata)."""
    tags: dict[str, list[str]] = {}
    try:
        bio = BytesIO(block)
        (vendor_len,) = struct.unpack("<I", bio.read(4))
        bio.seek(vendor_len, os.SEEK_CUR)
        (count,) = struct.unpack("<I", bio.read(4))
        for _ in range(count):
            (clen,) = struct.unpack("<I", bio.read(4))
            comment = bio.read(clen).decode("utf-8", "replace")
            if "=" in comment:
                key, val = comment.split("=", 1)
                tags.setdefault(key.upper(), []).append(val)
    except Exception:
        pass
    return tags


def _read_picture_block(f, length: int) -> dict:
    """Read a PICTURE block (all big-endian). `f` is positioned at the start of
    the block body; returns a dict and leaves the file position anywhere inside
    the block (caller seeks to the block end)."""
    ptype = struct.unpack(">I", f.read(4))[0]
    mlen = struct.unpack(">I", f.read(4))[0]
    mime = f.read(mlen).decode("ascii", "replace")
    dlen = struct.unpack(">I", f.read(4))[0]
    f.read(dlen)  # description (utf-8) -- skipped
    stored_w, stored_h, _depth, _colors = struct.unpack(">IIII", f.read(16))
    datalen = struct.unpack(">I", f.read(4))[0]

    is_uri = mime == "-->"
    real_w = real_h = None
    if not is_uri and datalen > 0:
        buf = b""
        for stage in HEADER_PROBE_STAGES:
            target = min(datalen, stage)
            if target > len(buf):
                buf += f.read(target - len(buf))
            real_w, real_h = _img_dims(buf)
            if real_w is not None or len(buf) >= datalen:
                break
        else:
            if len(buf) < datalen:  # last resort: read the whole picture
                buf += f.read(datalen - len(buf))
                real_w, real_h = _img_dims(buf)

    return {
        "type": ptype,
        "mime": mime,
        "stored_w": stored_w,
        "stored_h": stored_h,
        "data_len": datalen,
        "real_w": real_w,
        "real_h": real_h,
        "is_uri": is_uri,
    }


def _select_front(pictures: list[dict]) -> dict | None:
    """Pick the front cover: prefer picture type 3, else the first non-icon
    picture (some taggers store the cover as type 0 'Other')."""
    if not pictures:
        return None
    for p in pictures:
        if p["type"] == PICTURE_TYPE_FRONT_COVER:
            return p
    for p in pictures:
        if p["type"] not in PICTURE_TYPE_ICONS:
            return p
    return pictures[0]


def read_flac_fast(path: str) -> dict:
    """Fast path: parse only the metadata we need."""
    with open(path, "rb", buffering=64 * 1024) as f:
        head = f.read(10)
        start = 0
        if head[:3] == b"ID3":  # rare ID3 tag prepended before fLaC
            size = (
                (head[6] & 0x7F) << 21
                | (head[7] & 0x7F) << 14
                | (head[8] & 0x7F) << 7
                | (head[9] & 0x7F)
            )
            start = 10 + size
        f.seek(start)
        if f.read(4) != b"fLaC":
            raise ValueError("not a FLAC file")

        tags: dict[str, list[str]] = {}
        pictures: list[dict] = []
        while True:
            header = f.read(4)
            if len(header) < 4:
                break
            is_last = bool(header[0] & 0x80)
            btype = header[0] & 0x7F
            length = (header[1] << 16) | (header[2] << 8) | header[3]
            block_start = f.tell()

            if btype == BLOCK_VORBIS_COMMENT:
                tags = _parse_vorbis_comment(f.read(length))
            elif btype == BLOCK_PICTURE:
                pictures.append(_read_picture_block(f, length))

            # Always realign to the exact end of the block -- bulletproof
            # against any miscount and skips audio frames entirely.
            f.seek(block_start + length)
            if is_last:
                break

    def first(*keys):
        for k in keys:
            if tags.get(k):
                return tags[k][0]
        return None

    return {
        "album": first("ALBUM"),
        "artist": first("ARTIST", "ALBUMARTIST"),
        "albumartist": first("ALBUMARTIST", "ARTIST"),
        "front": _select_front(pictures),
    }


def read_flac_mutagen(path: str) -> dict:
    """Robust fallback using mutagen (reads the whole picture, slower)."""
    from mutagen.flac import FLAC

    audio = FLAC(path)
    pics = []
    for p in audio.pictures:
        w, h = _img_dims(bytes(p.data)) if p.data else (None, None)
        pics.append(
            {
                "type": int(p.type),
                "mime": p.mime or "",
                "stored_w": p.width,
                "stored_h": p.height,
                "data_len": len(p.data) if p.data else 0,
                "real_w": w,
                "real_h": h,
                "is_uri": (p.mime == "-->"),
            }
        )

    def first(*keys):
        for k in keys:
            v = audio.tags.get(k) if audio.tags else None
            if v:
                return v[0]
        return None

    return {
        "album": first("ALBUM"),
        "artist": first("ARTIST", "ALBUMARTIST"),
        "albumartist": first("ALBUMARTIST", "ARTIST"),
        "front": _select_front(pics),
    }


def read_flac(path: str) -> dict:
    try:
        return read_flac_fast(path)
    except Exception:
        try:
            return read_flac_mutagen(path)
        except Exception as exc:  # truly unreadable
            return {"album": None, "artist": None, "albumartist": None,
                    "front": None, "error": str(exc)}


def read_front_cover_bytes(path: str) -> tuple[bytes | None, str | None]:
    """Return (data, mime) of the front cover for displaying a thumbnail, or
    (None, None). Uses mutagen because we need the full image bytes here."""
    try:
        from mutagen.flac import FLAC

        audio = FLAC(path)
        if not audio.pictures:
            return None, None
        front = next(
            (p for p in audio.pictures if p.type == PICTURE_TYPE_FRONT_COVER),
            audio.pictures[0],
        )
        if not front.data or front.mime == "-->":
            return None, None
        return bytes(front.data), (front.mime or "image/jpeg")
    except Exception:
        return None, None


def status_for(front: dict | None, min_size: int) -> tuple[str, int | None, int | None]:
    """Classify a file's front cover."""
    if front is None:
        return "missing", None, None
    if front.get("is_uri") or front.get("data_len", 0) == 0:
        return "missing", None, None

    w, h = front.get("real_w"), front.get("real_h")
    if not w or not h:
        # could not decode the image header; fall back to stored fields
        w, h = front.get("stored_w") or 0, front.get("stored_h") or 0
        if not w or not h:
            return "unknown", None, None

    if w < min_size or h < min_size:
        return "too_small", w, h
    return "ok", w, h


def _majority(values) -> str | None:
    vals = [v for v in values if v]
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


def _scan_one(path: str, min_size: int) -> dict:
    """Read and classify a single file (pure: safe to run on a worker thread)."""
    info = read_flac(path)
    if info.get("error"):
        status, w, h = "error", None, None
    else:
        status, w, h = status_for(info.get("front"), min_size)
    return {
        "path": path,
        "album": info.get("album"),
        "artist": info.get("artist"),
        "albumartist": info.get("albumartist"),
        "front": info.get("front"),
        "status": status,
        "w": w,
        "h": h,
    }


def scan_library(
    root: str, min_size: int, progress: Callable[..., None] | None = None
) -> list[dict]:
    """Walk `root`, group FLAC files by their containing folder, and return a
    list of album dicts for every folder that has at least one file with a
    missing or too-small front cover."""
    flac_paths = []
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda e: None):
        for fn in filenames:
            if fn.lower().endswith(".flac"):
                flac_paths.append(os.path.join(dirpath, fn))

    total = len(flac_paths)
    if progress:
        progress(0, total, f"Found {total} FLAC files")

    folders: dict[str, list[dict]] = {}
    workers = max(1, min(_scan_workers(), total))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_scan_one, p, min_size): p for p in flac_paths}
        for fut in as_completed(futures):
            try:
                rec = fut.result()
            except Exception:  # read_flac normally swallows; stay defensive
                p = futures[fut]
                rec = {"path": p, "album": None, "artist": None,
                       "albumartist": None, "front": None,
                       "status": "error", "w": None, "h": None}
            folders.setdefault(os.path.dirname(rec["path"]), []).append(rec)
            done += 1
            if progress and (done % 25 == 0 or done == total):
                progress(done, total)
    if progress:
        progress(total, total)

    # Sort each folder's files so cover_file selection and the displayed file
    # list are deterministic regardless of completion order (SCAN_WORKERS=1
    # then reproduces the old sequential output exactly).
    for files in folders.values():
        files.sort(key=lambda f: f["path"])

    albums = []
    for folder, files in sorted(folders.items()):
        n_problem = sum(1 for f in files if f["status"] in PROBLEM_STATUSES)
        if n_problem == 0:
            continue
        album = _majority([f["album"] for f in files]) or os.path.basename(folder)
        artist = _majority(
            [f["albumartist"] or f["artist"] for f in files]
        )
        query = f"{artist} {album}".strip() if artist else album
        cover_file = next(
            (
                f["path"]
                for f in files
                if f["front"]
                and not f["front"].get("is_uri")
                and f["front"].get("data_len", 0) > 0
            ),
            None,
        )
        albums.append(
            {
                "folder": folder,
                "album": album,
                "artist": artist,
                "query": query,
                "files": files,
                "n_problem": n_problem,
                "has_cover": cover_file is not None,
                "cover_file": cover_file,
            }
        )
    return albums
