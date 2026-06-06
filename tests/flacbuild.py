"""Builders for synthetic, spec-correct FLAC files used by the tests.

No external encoder is needed: we hand-assemble a minimal STREAMINFO (valid
enough for mutagen), a VORBIS_COMMENT block (little-endian), and an optional
PICTURE block (big-endian).
"""

import struct
from io import BytesIO

from PIL import Image


def streaminfo_block() -> bytes:
    # 34-byte STREAMINFO for 44100 Hz / 2ch / 16-bit, blocksize 4096, 0 samples.
    body = bytes(
        [0x10, 0x00, 0x10, 0x00,
         0x00, 0x00, 0x00,
         0x00, 0x00, 0x00,
         0x0A, 0xC4, 0x42, 0xF0,
         0x00, 0x00, 0x00, 0x00]
    ) + bytes(16)
    header = bytes([0x00]) + struct.pack(">I", 34)[1:]  # type 0, not last
    return header + body


def vorbis_block(album: str, artist: str, is_last: bool) -> bytes:
    vendor = b"test"
    comments = [f"ALBUM={album}".encode(), f"ARTIST={artist}".encode()]
    body = struct.pack("<I", len(vendor)) + vendor
    body += struct.pack("<I", len(comments))
    for c in comments:
        body += struct.pack("<I", len(c)) + c
    btype = 0x84 if is_last else 0x04  # type 4
    return bytes([btype]) + struct.pack(">I", len(body))[1:] + body


def picture_block(data: bytes, stored_w=0, stored_h=0, ptype=3,
                  mime=b"image/jpeg", is_last=True) -> bytes:
    desc = b"Front cover"
    body = struct.pack(">I", ptype)
    body += struct.pack(">I", len(mime)) + mime
    body += struct.pack(">I", len(desc)) + desc
    body += struct.pack(">IIII", stored_w, stored_h, 24, 0)
    body += struct.pack(">I", len(data)) + data
    flag = 0x86 if is_last else 0x06  # type 6
    return bytes([flag]) + struct.pack(">I", len(body))[1:] + body


def make_jpeg(w: int, h: int, color=(200, 60, 60)) -> bytes:
    out = BytesIO()
    Image.new("RGB", (w, h), color).save(out, format="JPEG", quality=85)
    return out.getvalue()


def make_png(w: int, h: int, color=(20, 120, 200)) -> bytes:
    out = BytesIO()
    Image.new("RGB", (w, h), color).save(out, format="PNG")
    return out.getvalue()


def write_flac(path, album="Album", artist="Artist", picture=None,
               stored_w=0, stored_h=0, ptype=3, mime=b"image/jpeg",
               id3_prefix=False) -> str:
    data = b""
    if id3_prefix:
        data += b"ID3" + bytes([3, 0, 0, 0, 0, 0, 0])  # synchsafe size 0
    data += b"fLaC" + streaminfo_block()
    if picture is None:
        data += vorbis_block(album, artist, is_last=True)
    else:
        data += vorbis_block(album, artist, is_last=False)
        data += picture_block(picture, stored_w, stored_h, ptype, mime)
    with open(path, "wb") as f:
        f.write(data)
    return str(path)
