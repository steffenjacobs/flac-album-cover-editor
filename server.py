"""
FLAC Album Cover Editor -- local web app backend.

Serves a JSON API + the static UI on http://127.0.0.1:8765. Run it from your
own logged-in Windows session so it inherits access to the SMB share.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import requests
from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from art_sources import gather_candidate_urls
from patcher import reencode_jpeg, write_cover
from scanner import read_front_cover_bytes, scan_library

BASE = Path(__file__).resolve().parent
STATIC_DIR = BASE / "static"
CONFIG_PATH = BASE / "config.json"

DEFAULTS = {"music_root": r"\\192.168.1.147\Music\Music HQ", "min_size": 800}
UA = "FlacCoverTool/1.0 (personal album-art tagging tool)"

# ---------------------------------------------------------------- config
def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    # Environment variables win over config.json (used by the Docker setup so
    # the in-container mount path is always authoritative).
    if os.environ.get("MUSIC_ROOT"):
        cfg["music_root"] = os.environ["MUSIC_ROOT"]
    if os.environ.get("MIN_SIZE"):
        try:
            cfg["min_size"] = int(os.environ["MIN_SIZE"])
        except ValueError:
            pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


CONFIG = load_config()

# ---------------------------------------------------------------- state
SCAN = {"state": "idle", "scanned": 0, "total": 0, "error": None, "message": ""}
SCAN_LOCK = threading.Lock()
ALBUMS: dict[str, dict] = {}          # album id -> album dict
CACHE: dict[str, dict] = {}           # candidate token -> {data, mime, w, h}

app = FastAPI(title="FLAC Album Cover Editor")


# ---------------------------------------------------------------- scan
def _run_scan(root: str, min_size: int):
    global ALBUMS
    try:
        def progress(scanned, total, message=""):
            with SCAN_LOCK:
                SCAN["scanned"] = scanned
                SCAN["total"] = total
                if message:
                    SCAN["message"] = message

        albums = scan_library(root, min_size, progress)
        new = {}
        for i, a in enumerate(albums):
            a["id"] = str(i)
            new[str(i)] = a
        ALBUMS = new
        with SCAN_LOCK:
            SCAN["state"] = "done"
            SCAN["message"] = f"{len(albums)} folder(s) need attention"
    except Exception as exc:
        traceback.print_exc()
        with SCAN_LOCK:
            SCAN["state"] = "error"
            SCAN["error"] = str(exc)
            SCAN["message"] = str(exc)


def _album_summary(a: dict):
    return {
        "id": a["id"],
        "folder": a["folder"],
        "album": a["album"],
        "artist": a["artist"],
        "query": a["query"],
        "n_files": len(a["files"]),
        "n_problem": a["n_problem"],
        "has_cover": a["has_cover"],
        "files": [
            {
                "name": os.path.basename(f["path"]),
                "status": f["status"],
                "w": f["w"],
                "h": f["h"],
            }
            for f in a["files"]
        ],
    }


@app.post("/api/scan")
def start_scan(payload: dict = Body(default={})):
    with SCAN_LOCK:
        if SCAN["state"] == "scanning":
            raise HTTPException(409, "A scan is already running")
        root = (payload.get("music_root") or CONFIG["music_root"]).strip()
        min_size = int(payload.get("min_size") or CONFIG["min_size"])
        CONFIG["music_root"] = root
        CONFIG["min_size"] = min_size
        save_config(CONFIG)
        if not os.path.isdir(root):
            raise HTTPException(400, f"Folder not found or inaccessible: {root}")
        SCAN.update(
            {"state": "scanning", "scanned": 0, "total": 0, "error": None,
             "message": "Scanning..."}
        )
    threading.Thread(target=_run_scan, args=(root, min_size), daemon=True).start()
    return {"ok": True}


@app.get("/api/scan")
def scan_status():
    with SCAN_LOCK:
        s = dict(SCAN)
    if s["state"] == "done":
        s["albums"] = [_album_summary(a) for a in ALBUMS.values()]
    return s


# ---------------------------------------------------------------- covers
@app.get("/api/albums/{aid}/current-cover")
def current_cover(aid: str):
    a = ALBUMS.get(aid)
    if not a or not a.get("cover_file"):
        raise HTTPException(404, "no embedded cover")
    data, mime = read_front_cover_bytes(a["cover_file"])
    if data is None:
        raise HTTPException(404, "no embedded cover")
    return Response(content=data, media_type=mime or "image/jpeg")


def _fetch_candidate(c: dict):
    try:
        resp = requests.get(c["url"], headers={"User-Agent": UA}, timeout=15)
        if resp.status_code != 200 or not resp.content:
            return None
        data = resp.content
        im = Image.open(BytesIO(data))
        w, h = im.size
        token = uuid.uuid4().hex
        CACHE[token] = {
            "data": data,
            "mime": resp.headers.get("Content-Type", "image/jpeg"),
            "w": w,
            "h": h,
        }
        return {
            "token": token,
            "source": c["source"],
            "title": c.get("title"),
            "artist": c.get("artist"),
            "width": w,
            "height": h,
            "url": f"/api/candidate/{token}",
            "meets": (w >= CONFIG["min_size"] and h >= CONFIG["min_size"]),
        }
    except Exception:
        return None


MAX_CANDIDATE_PAGE = 5  # safety cap on "find more" paging


@app.get("/api/albums/{aid}/candidates")
def candidates(aid: str, more: bool = False, q: str | None = None):
    a = ALBUMS.get(aid)
    if not a:
        raise HTTPException(404, "unknown album")

    # `more=False` starts fresh (page 0, clears the seen set); `more=True`
    # advances to the next page and only returns URLs not already shown.
    # `q` is an optional free-text override; when given it replaces the
    # tag-derived query (artist/album are dropped so the raw term is searched).
    # The active search context is remembered so `more=True` keeps paging it.
    if more:
        a["cand_page"] = a.get("cand_page", 0) + 1
    else:
        a["cand_page"] = 0
        a["seen_urls"] = set()
        if q and q.strip():
            a["active"] = {"query": q.strip(), "artist": None, "album": None}
        else:
            a["active"] = {"query": a["query"], "artist": a.get("artist"),
                           "album": a.get("album")}
    page = a["cand_page"]
    seen = a.setdefault("seen_urls", set())
    act = a.get("active") or {"query": a["query"], "artist": a.get("artist"),
                              "album": a.get("album")}

    urls = gather_candidate_urls(
        act["query"], act["artist"], act["album"], UA, page=page
    )
    fresh = []
    for c in urls:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        fresh.append(c)

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_fetch_candidate, fresh[:15]):
            if r:
                results.append(r)
    # Candidates meeting the size bar first, then largest first.
    results.sort(key=lambda r: (not r["meets"], -(r["width"] * r["height"])))

    has_more = bool(results) and page < MAX_CANDIDATE_PAGE
    return {"candidates": results, "query": act["query"], "page": page,
            "has_more": has_more}


@app.get("/api/candidate/{token}")
def candidate_image(token: str):
    c = CACHE.get(token)
    if not c:
        raise HTTPException(404, "candidate expired")
    return Response(content=c["data"], media_type=c["mime"])


MAX_UPLOAD_BYTES = 30 * 1024 * 1024


@app.post("/api/upload")
async def upload_cover(file: UploadFile = File(...)):
    """Accept a user-supplied image, validate it, and stash it in the candidate
    cache so it can be selected and patched like any fetched candidate."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Image too large (max 30 MB)")
    try:
        im = Image.open(BytesIO(data))
        w, h = im.size
    except Exception:
        raise HTTPException(400, "That file is not a readable image")

    token = uuid.uuid4().hex
    CACHE[token] = {
        "data": data,
        "mime": file.content_type or "image/jpeg",
        "w": w,
        "h": h,
    }
    return {
        "token": token,
        "source": "Upload",
        "title": file.filename,
        "artist": None,
        "width": w,
        "height": h,
        "url": f"/api/candidate/{token}",
        "meets": (w >= CONFIG["min_size"] and h >= CONFIG["min_size"]),
    }


# ---------------------------------------------------------------- patch
@app.post("/api/albums/{aid}/patch")
def patch(aid: str, payload: dict = Body(...)):
    a = ALBUMS.get(aid)
    if not a:
        raise HTTPException(404, "unknown album")
    token = payload.get("token")
    backup = bool(payload.get("backup", True))
    c = CACHE.get(token)
    if not c:
        raise HTTPException(400, "Candidate image not found -- re-fetch covers")

    jpeg = reencode_jpeg(c["data"], max_dim=1200, quality=88)
    results = []
    for f in a["files"]:
        try:
            write_cover(f["path"], jpeg, backup=backup)
            results.append({"name": os.path.basename(f["path"]), "ok": True})
        except Exception as exc:
            results.append(
                {"name": os.path.basename(f["path"]), "ok": False, "error": str(exc)}
            )

    ok = all(r["ok"] for r in results)
    if ok:
        a["n_problem"] = 0
        a["has_cover"] = True
        for f in a["files"]:
            f["status"] = "ok"
            f["w"], f["h"] = jpeg["w"], jpeg["h"]
    return {
        "ok": ok,
        "results": results,
        "cover": {"w": jpeg["w"], "h": jpeg["h"], "bytes": len(jpeg["bytes"]),
                  "quality": jpeg["quality"]},
    }


# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def get_settings():
    return CONFIG


@app.post("/api/settings")
def set_settings(payload: dict = Body(...)):
    if "music_root" in payload:
        CONFIG["music_root"] = payload["music_root"].strip()
    if "min_size" in payload:
        CONFIG["min_size"] = int(payload["min_size"])
    save_config(CONFIG)
    return CONFIG


# ---------------------------------------------------------------- static UI
# Mount LAST so /api/* routes take precedence. html=True serves index.html.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="ui")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    print(f"FLAC Album Cover Editor  ->  http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
