import os

import pytest

import scanner
import patcher
from flacbuild import make_jpeg, write_flac


def test_reads_tags_and_true_dims_ignoring_stored(tmp_path):
    # stored dims lie (0x0) but real image is 1000x1000 -> ok
    p = write_flac(tmp_path / "a.flac", "Big", "Artist", make_jpeg(1000, 1000))
    info = scanner.read_flac_fast(p)
    assert info["album"] == "Big" and info["artist"] == "Artist"
    assert (info["front"]["real_w"], info["front"]["real_h"]) == (1000, 1000)
    assert scanner.status_for(info["front"], 800) == ("ok", 1000, 1000)


def test_inflated_stored_dims_ignored(tmp_path):
    p = write_flac(tmp_path / "s.flac", "S", "A", make_jpeg(300, 300),
                   stored_w=9999, stored_h=9999)
    info = scanner.read_flac_fast(p)
    assert info["front"]["real_w"] == 300
    assert scanner.status_for(info["front"], 800)[0] == "too_small"


def test_no_picture_is_missing(tmp_path):
    p = write_flac(tmp_path / "n.flac", "N", "Nobody", picture=None)
    info = scanner.read_flac_fast(p)
    assert info["front"] is None
    assert scanner.status_for(info["front"], 800)[0] == "missing"


def test_id3_prefix_is_skipped(tmp_path):
    p = write_flac(tmp_path / "id3.flac", "Tagged", "Person",
                   make_jpeg(900, 900), id3_prefix=True)
    info = scanner.read_flac_fast(p)
    assert info["album"] == "Tagged" and info["artist"] == "Person"


def test_non_flac_raises_then_read_flac_returns_error(tmp_path):
    p = tmp_path / "bad.flac"
    p.write_bytes(b"NOTAFLAC" + b"\x00" * 50)
    with pytest.raises(ValueError):
        scanner.read_flac_fast(str(p))
    info = scanner.read_flac(str(p))
    assert info["error"] and info["front"] is None and info["album"] is None


def test_uri_picture_treated_as_missing(tmp_path):
    p = write_flac(tmp_path / "uri.flac", "U", "A",
                   b"http://example.com/cover.jpg", mime=b"-->")
    info = scanner.read_flac_fast(p)
    assert info["front"]["is_uri"] is True
    assert scanner.status_for(info["front"], 800)[0] == "missing"


def test_large_non_image_picture_reads_remaining_bytes(tmp_path):
    # Bigger than the last probe stage: every stage fails, the full read fires,
    # still undecodable -> real dims None -> 'unknown'.
    blob = b"\x00" * (scanner.HEADER_PROBE_STAGES[-1] + 2000)
    p = write_flac(tmp_path / "garbage.flac", "G", "A", blob)
    info = scanner.read_flac_fast(p)
    assert info["front"]["real_w"] is None
    assert scanner.status_for(info["front"], 800)[0] == "unknown"


def test_staged_probe_escalates_to_find_dims(tmp_path):
    # A JPEG whose SOF sits past the first 16KB stage (large COM marker): the
    # first stage can't decode it, the second stage does.
    from io import BytesIO
    from PIL import Image
    out = BytesIO()
    pad = scanner.HEADER_PROBE_STAGES[0] + 8000  # push SOF beyond stage 1
    Image.new("RGB", (1000, 1000), (10, 20, 30)).save(
        out, format="JPEG", comment=b"x" * pad)
    p = write_flac(tmp_path / "bighdr.flac", "B", "A", out.getvalue())
    info = scanner.read_flac_fast(p)
    assert (info["front"]["real_w"], info["front"]["real_h"]) == (1000, 1000)


def test_scan_workers_deterministic(tmp_path, monkeypatch):
    for i in range(6):
        d = tmp_path / f"Album{i}"; d.mkdir()
        write_flac(d / "1.flac", f"Album{i}", "Artist", make_jpeg(300, 300))
        write_flac(d / "2.flac", f"Album{i}", "Artist", make_jpeg(1000, 1000))

    def run(workers):
        monkeypatch.setenv("SCAN_WORKERS", str(workers))
        albums = scanner.scan_library(str(tmp_path), 800)
        return [(a["folder"], a["n_problem"],
                 [(f["path"], f["status"]) for f in a["files"]]) for a in albums]

    assert run(1) == run(4)  # parallel result identical to serial


def test_status_for_branches():
    assert scanner.status_for(None, 800)[0] == "missing"
    assert scanner.status_for({"is_uri": True, "data_len": 5}, 800)[0] == "missing"
    assert scanner.status_for({"data_len": 0}, 800)[0] == "missing"
    assert scanner.status_for(
        {"real_w": 1000, "real_h": 1000, "data_len": 1}, 800
    ) == ("ok", 1000, 1000)
    # real dims missing -> fall back to stored dims
    assert scanner.status_for(
        {"real_w": None, "real_h": None, "stored_w": 900, "stored_h": 900,
         "data_len": 1}, 800
    ) == ("ok", 900, 900)
    # real and stored both unknown
    assert scanner.status_for(
        {"real_w": None, "real_h": None, "stored_w": 0, "stored_h": 0,
         "data_len": 1}, 800
    )[0] == "unknown"


def test_img_dims_failure():
    assert scanner._img_dims(b"not an image") == (None, None)


def test_select_front_and_majority():
    assert scanner._select_front([]) is None
    assert scanner._select_front([{"type": 0}])["type"] == 0          # non-icon
    assert scanner._select_front([{"type": 1}])["type"] == 1          # icon fallback
    assert scanner._select_front(
        [{"type": 1}, {"type": 3}]
    )["type"] == 3                                                     # prefers front
    assert scanner._majority([]) is None
    assert scanner._majority(["a", "a", "b"]) == "a"


def test_read_flac_mutagen_and_front_cover_bytes(tmp_path):
    p = write_flac(tmp_path / "m.flac", "Album", "Artist", picture=None)
    patcher.write_cover(p, patcher.reencode_jpeg(make_jpeg(1000, 1000)), backup=False)

    info = scanner.read_flac_mutagen(p)
    assert info["album"] == "Album"
    assert info["front"]["real_w"] == 1000

    data, mime = scanner.read_front_cover_bytes(p)
    assert data and mime == "image/jpeg"

    # no-cover and unreadable -> (None, None)
    p2 = write_flac(tmp_path / "nc.flac", "N", "A", picture=None)
    assert scanner.read_front_cover_bytes(p2) == (None, None)
    bad = tmp_path / "x.flac"
    bad.write_bytes(b"nope")
    assert scanner.read_front_cover_bytes(str(bad)) == (None, None)


def test_scan_library_grouping_skip_and_error(tmp_path):
    a = tmp_path / "A"; a.mkdir()
    write_flac(a / "1.flac", "AlbumA", "Artist", make_jpeg(300, 300))  # problem
    b = tmp_path / "B"; b.mkdir()
    write_flac(b / "1.flac", "AlbumB", "Artist", make_jpeg(1000, 1000))  # ok -> skip
    c = tmp_path / "C"; c.mkdir()
    (c / "broken.flac").write_bytes(b"NOTFLAC")  # error

    calls = []
    albums = scanner.scan_library(str(tmp_path), 800,
                                  progress=lambda *args: calls.append(args))
    names = {al["album"] for al in albums}
    assert "AlbumA" in names and "AlbumB" not in names  # ok folder skipped
    by_folder = {os.path.basename(al["folder"]): al for al in albums}
    assert by_folder["A"]["n_problem"] == 1 and by_folder["A"]["has_cover"] is True
    assert by_folder["C"]["files"][0]["status"] == "error"
    assert calls  # progress was reported
