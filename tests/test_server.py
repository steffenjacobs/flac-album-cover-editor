import json
import time

import pytest

import server
import patcher
from flacbuild import make_jpeg, write_flac


# ---------------------------------------------------------------- config
def test_load_config_merge_and_env_override(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"music_root": "/from/file", "min_size": 500}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.delenv("MUSIC_ROOT", raising=False)
    monkeypatch.delenv("MIN_SIZE", raising=False)
    cfg = server.load_config()
    assert cfg["music_root"] == "/from/file" and cfg["min_size"] == 500

    monkeypatch.setenv("MUSIC_ROOT", "/from/env")
    monkeypatch.setenv("MIN_SIZE", "999")
    cfg = server.load_config()
    assert cfg["music_root"] == "/from/env" and cfg["min_size"] == 999

    monkeypatch.setenv("MIN_SIZE", "not-an-int")  # ValueError branch ignored
    assert server.load_config()["min_size"] == 500


def test_load_config_handles_bad_json(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{not valid json")
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.delenv("MUSIC_ROOT", raising=False)
    cfg = server.load_config()
    assert cfg == server.DEFAULTS  # defaults survive


def test_save_config_swallows_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path)  # a directory -> OSError
    server.save_config({"x": 1})  # must not raise


# ---------------------------------------------------------------- scan
def test_scan_flow(client, tmp_path, monkeypatch):
    lib = tmp_path / "lib" / "Album"
    lib.mkdir(parents=True)
    write_flac(lib / "1.flac", "AlbumA", "Artist", make_jpeg(300, 300))

    r = client.post("/api/scan", json={"music_root": str(tmp_path / "lib"),
                                       "min_size": 800})
    assert r.status_code == 200

    for _ in range(50):
        s = client.get("/api/scan").json()
        if s["state"] != "scanning":
            break
        time.sleep(0.1)
    assert s["state"] == "done"
    assert len(s["albums"]) == 1 and s["albums"][0]["n_problem"] == 1


def test_scan_folder_not_found(client):
    r = client.post("/api/scan", json={"music_root": "/no/such/folder"})
    assert r.status_code == 400


def test_scan_already_running(client, monkeypatch):
    server.SCAN["state"] = "scanning"
    r = client.post("/api/scan", json={"music_root": "."})
    assert r.status_code == 409


# ---------------------------------------------------------------- covers
def test_current_cover(client, tmp_path):
    p = write_flac(tmp_path / "c.flac", "A", "B", picture=None)
    patcher.write_cover(p, patcher.reencode_jpeg(make_jpeg(900, 900)), backup=False)
    server.ALBUMS["0"] = {"cover_file": p}
    assert client.get("/api/albums/0/current-cover").status_code == 200

    server.ALBUMS["1"] = {"cover_file": None}
    assert client.get("/api/albums/1/current-cover").status_code == 404

    coverless = write_flac(tmp_path / "n.flac", "A", "B", picture=None)
    server.ALBUMS["2"] = {"cover_file": coverless}
    assert client.get("/api/albums/2/current-cover").status_code == 404


# ---------------------------------------------------------------- candidates
def test_candidates_paging_dedup_and_override(client, monkeypatch):
    server.ALBUMS["0"] = {"id": "0", "query": "Q", "artist": "Ar", "album": "Al"}

    pages = {
        0: [{"source": "iTunes", "url": "u1", "title": "t1", "artist": "a1"},
            {"source": "iTunes", "url": "u-bad", "title": "t", "artist": "a"},
            {"source": "iTunes", "url": "u-raise", "title": "t", "artist": "a"}],
        1: [{"source": "Deezer", "url": "u2", "title": "t2", "artist": "a2"},
            {"source": "iTunes", "url": "u1", "title": "dup", "artist": "a"}],
    }
    captured = {}

    def fake_gather(query, artist, album, ua, page=0, per_source=5):
        captured["query"] = query
        return pages[page]

    monkeypatch.setattr(server, "gather_candidate_urls", fake_gather)

    jpeg = make_jpeg(1000, 1000)

    class Resp:
        def __init__(self, status, content):
            self.status_code = status
            self.content = content
            self.headers = {"Content-Type": "image/jpeg"}

    def fake_get(url, headers=None, timeout=None):
        if url == "u-bad":
            return Resp(404, b"")
        if url == "u-raise":
            raise RuntimeError("network")
        return Resp(200, jpeg)

    monkeypatch.setattr(server.requests, "get", fake_get)

    r = client.get("/api/albums/0/candidates").json()
    assert len(r["candidates"]) == 1 and r["candidates"][0]["url"].startswith("/api/candidate/")
    assert r["has_more"] is True and r["page"] == 0

    r2 = client.get("/api/albums/0/candidates?more=1").json()
    # u1 already seen -> only u2 is fresh
    assert len(r2["candidates"]) == 1 and r2["page"] == 1

    r3 = client.get("/api/albums/0/candidates?q=custom term").json()
    assert captured["query"] == "custom term"

    assert client.get("/api/albums/zzz/candidates").status_code == 404


# ---------------------------------------------------------------- candidate img + upload
def test_candidate_image_and_upload(client, monkeypatch):
    assert client.get("/api/candidate/nope").status_code == 404
    server.CACHE["tok"] = {"data": b"abc", "mime": "image/png", "w": 1, "h": 1}
    r = client.get("/api/candidate/tok")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"

    # empty
    assert client.post("/api/upload",
                       files={"file": ("e.jpg", b"", "image/jpeg")}).status_code == 400
    # too large
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 10)
    assert client.post("/api/upload",
                       files={"file": ("b.jpg", b"x" * 20, "image/jpeg")}).status_code == 400
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 30 * 1024 * 1024)
    # not an image
    assert client.post("/api/upload",
                       files={"file": ("n.txt", b"not an image", "text/plain")}).status_code == 400
    # valid
    r = client.post("/api/upload",
                    files={"file": ("c.jpg", make_jpeg(1000, 1000), "image/jpeg")})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "Upload" and body["meets"] is True
    assert body["token"] in server.CACHE


# ---------------------------------------------------------------- patch
def test_patch_success_partial_and_errors(client, tmp_path, monkeypatch):
    f1 = write_flac(tmp_path / "1.flac", "A", "B", picture=None)
    f2 = write_flac(tmp_path / "2.flac", "A", "B", picture=None)
    server.ALBUMS["0"] = {
        "id": "0", "n_problem": 2, "has_cover": False,
        "files": [{"path": f1, "status": "missing", "w": None, "h": None},
                  {"path": f2, "status": "missing", "w": None, "h": None}],
    }
    server.CACHE["tok"] = {"data": make_jpeg(1000, 1000), "mime": "image/jpeg",
                           "w": 1000, "h": 1000}

    r = client.post("/api/albums/0/patch", json={"token": "tok", "backup": False})
    body = r.json()
    assert body["ok"] is True and all(x["ok"] for x in body["results"])
    assert server.ALBUMS["0"]["n_problem"] == 0

    # partial failure: write_cover raises for f2
    server.ALBUMS["0"]["files"] = [{"path": f1}, {"path": f2}]
    real = patcher.write_cover

    def flaky(path, jpeg, backup=True):
        if path.endswith("2.flac"):
            raise OSError("disk full")
        return real(path, jpeg, backup=backup)

    monkeypatch.setattr(server, "write_cover", flaky)
    body = client.post("/api/albums/0/patch", json={"token": "tok"}).json()
    assert body["ok"] is False
    assert sum(1 for x in body["results"] if not x["ok"]) == 1

    assert client.post("/api/albums/zzz/patch", json={"token": "tok"}).status_code == 404
    assert client.post("/api/albums/0/patch",
                       json={"token": "missing"}).status_code == 400


# ---------------------------------------------------------------- settings + static
def test_settings_and_static_index(client):
    assert "music_root" in client.get("/api/settings").json()
    r = client.post("/api/settings", json={"music_root": "/x", "min_size": 600})
    assert r.json()["music_root"] == "/x" and r.json()["min_size"] == 600

    index = client.get("/")
    assert index.status_code == 200 and "FLAC Album Cover Editor" in index.text


def test_rejects_untrusted_host():
    from fastapi.testclient import TestClient
    with TestClient(server.app, base_url="http://evil.example.com") as c:
        assert c.get("/api/settings").status_code == 400
