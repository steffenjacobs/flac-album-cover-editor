"""
Self-contained tests for the FLAC scanner + patcher.

These build *synthetic* but spec-correct FLAC files in a temp dir (no external
encoder needed) so we can verify:
  - the fast metadata parser reads ALBUM/ARTIST (little-endian vorbis comments),
  - it reads TRUE image dimensions from the PICTURE block (big-endian) and does
    NOT trust the stored width/height fields,
  - status classification (ok / too_small / missing),
  - reencode_jpeg downscaling + size cap,
  - write_cover round-trips through mutagen.

Run:  .venv\\Scripts\\python.exe -m pytest -q   (or just run this file directly)
"""

import os
import struct
import sys
import tempfile
from io import BytesIO

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scanner
import patcher


def _streaminfo_block():
    # 34-byte STREAMINFO for 44100 Hz / 2ch / 16-bit, blocksize 4096, 0 samples.
    body = bytes(
        [0x10, 0x00, 0x10, 0x00,            # min/max blocksize = 4096
         0x00, 0x00, 0x00,                  # min framesize
         0x00, 0x00, 0x00,                  # max framesize
         0x0A, 0xC4, 0x42, 0xF0,            # sr=44100, ch=2, bps=16, ...
         0x00, 0x00, 0x00, 0x00]            # ...total samples (36 bits) = 0
    ) + bytes(16)                            # md5 = 0
    assert len(body) == 34
    header = bytes([0x00]) + struct.pack(">I", 34)[1:]  # type 0, not last
    return header + body


def _vorbis_block(album, artist, is_last):
    vendor = b"test"
    comments = [f"ALBUM={album}".encode(), f"ARTIST={artist}".encode()]
    body = struct.pack("<I", len(vendor)) + vendor
    body += struct.pack("<I", len(comments))
    for c in comments:
        body += struct.pack("<I", len(c)) + c
    btype = 0x84 if is_last else 0x04  # type 4
    return bytes([btype]) + struct.pack(">I", len(body))[1:] + body


def _picture_block(jpeg, stored_w, stored_h):
    mime = b"image/jpeg"
    desc = b"Front cover"
    body = struct.pack(">I", 3)                       # type 3 = front cover
    body += struct.pack(">I", len(mime)) + mime
    body += struct.pack(">I", len(desc)) + desc
    body += struct.pack(">IIII", stored_w, stored_h, 24, 0)  # stored dims (lie!)
    body += struct.pack(">I", len(jpeg)) + jpeg
    return bytes([0x86]) + struct.pack(">I", len(body))[1:] + body  # type 6, last


def _make_jpeg(w, h, color=(200, 60, 60)):
    out = BytesIO()
    Image.new("RGB", (w, h), color).save(out, format="JPEG", quality=85)
    return out.getvalue()


def _write_flac(path, album, artist, jpeg=None, stored_w=0, stored_h=0):
    data = b"fLaC" + _streaminfo_block()
    if jpeg is None:
        data += _vorbis_block(album, artist, is_last=True)
    else:
        data += _vorbis_block(album, artist, is_last=False)
        data += _picture_block(jpeg, stored_w, stored_h)
    with open(path, "wb") as f:
        f.write(data)


def run():
    tmp = tempfile.mkdtemp(prefix="flactest_")
    failures = []

    def check(name, cond):
        print(("  OK  " if cond else " FAIL ") + name)
        if not cond:
            failures.append(name)

    # 1) Large cover (1000x1000) but stored dims lie (0x0) -> should be OK
    big = os.path.join(tmp, "big.flac")
    _write_flac(big, "Big Album", "Big Artist", _make_jpeg(1000, 1000),
                stored_w=0, stored_h=0)
    info = scanner.read_flac_fast(big)
    check("reads ALBUM tag", info["album"] == "Big Album")
    check("reads ARTIST tag", info["artist"] == "Big Artist")
    check("real dims decoded ignoring stored zeros",
          info["front"]["real_w"] == 1000 and info["front"]["real_h"] == 1000)
    st, w, h = scanner.status_for(info["front"], 800)
    check("1000px classified ok", st == "ok" and w == 1000)

    # 2) Small cover (300x300) but stored dims lie BIG (9999) -> too_small
    small = os.path.join(tmp, "small.flac")
    _write_flac(small, "Small Album", "Small Artist", _make_jpeg(300, 300),
                stored_w=9999, stored_h=9999)
    info = scanner.read_flac_fast(small)
    check("ignores inflated stored dims (real=300)",
          info["front"]["real_w"] == 300)
    st, _, _ = scanner.status_for(info["front"], 800)
    check("300px classified too_small", st == "too_small")

    # 3) No cover -> missing
    none = os.path.join(tmp, "none.flac")
    _write_flac(none, "No Cover", "Nobody", jpeg=None)
    info = scanner.read_flac_fast(none)
    st, _, _ = scanner.status_for(info["front"], 800)
    check("no picture classified missing", st == "missing" and info["front"] is None)

    # 4) scan_library groups by folder and flags the right folders
    albums = scanner.scan_library(tmp, 800)
    folders = {a["album"]: a for a in albums}
    # all three files are in the same folder (tmp), so one album entry
    check("scan groups by folder", len(albums) == 1)
    a = albums[0]
    check("folder has 2 problem files", a["n_problem"] == 2)
    check("folder reports a usable existing cover", a["has_cover"] is True)

    # 5) reencode_jpeg downscales and caps size
    huge = _make_jpeg(3000, 3000, color=(20, 120, 200))
    enc = patcher.reencode_jpeg(huge, max_dim=1200, quality=88)
    check("reencode downscales to <=1200", enc["w"] <= 1200 and enc["h"] <= 1200)
    check("reencode under 4MB", len(enc["bytes"]) < 4 * 1024 * 1024)

    # 6) write_cover round-trips through mutagen (replaces small cover)
    try:
        patcher.write_cover(small, enc, backup=True)
        info2 = scanner.read_flac_fast(small)
        check("after patch dims match new cover",
              info2["front"]["real_w"] == enc["w"] and
              info2["front"]["real_h"] == enc["h"])
        check("backup .bak created", os.path.exists(small + ".bak"))
        st2, _, _ = scanner.status_for(info2["front"], 800)
        check("patched cover now ok", st2 == "ok")
    except Exception as exc:
        check("write_cover round-trip (mutagen accepted synthetic flac): " + repr(exc), False)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    run()
