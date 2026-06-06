"""
Re-encode a chosen cover image to JPEG and embed it into FLAC files.

Writing uses mutagen (pure-Python, reliable). We:
  - downscale to at most 1200x1200 (never upscale) and re-encode JPEG,
  - keep the result well under 4 MB (a Windows-11 Explorer display bug corrupts
    FLAC metadata above ~4 MB; the hard FLAC block limit is 16 MiB),
  - clear existing PICTURE blocks first (writers append, not replace),
  - set picture type 3 (front cover) with correct width/height (mutagen does NOT
    auto-fill these),
  - write atomically (temp file + os.replace) so an interrupted save can't
    corrupt the user's only copy.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import warnings
from io import BytesIO

from PIL import Image
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

# Bound decoded pixels so a tiny "decompression bomb" can't exhaust memory,
# while leaving ample headroom for legitimate album art (16k x 16k).
MAX_IMAGE_PIXELS = 256_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

try:
    RESAMPLE = Image.Resampling.LANCZOS  # Pillow >= 9.1
except AttributeError:  # pragma: no cover - very old Pillow
    RESAMPLE = Image.LANCZOS

EMBED_MAX_DIM = 1200
EMBED_QUALITY = 88
MAX_EMBED_BYTES = 4 * 1024 * 1024
MIN_EMBED_QUALITY = 55


def reencode_jpeg(data: bytes, max_dim: int = EMBED_MAX_DIM,
                  quality: int = EMBED_QUALITY) -> dict:
    """Return {'bytes', 'w', 'h', 'quality'} of a JPEG suitable for embedding."""
    im = Image.open(BytesIO(data)).convert("RGB")
    im.thumbnail((max_dim, max_dim), RESAMPLE)  # never enlarges

    def encode(q: int) -> bytes:
        out = BytesIO()
        im.save(out, format="JPEG", quality=q, subsampling="4:2:0", optimize=True)
        return out.getvalue()

    q = quality
    blob = encode(q)
    while len(blob) > MAX_EMBED_BYTES and q > MIN_EMBED_QUALITY:
        q -= 8
        blob = encode(q)

    return {"bytes": blob, "w": im.width, "h": im.height, "quality": q}


def _make_picture(jpeg: dict) -> Picture:
    pic = Picture()
    pic.type = PictureType.COVER_FRONT
    pic.mime = "image/jpeg"
    pic.desc = "Front cover"
    pic.data = jpeg["bytes"]
    pic.width = jpeg["w"]
    pic.height = jpeg["h"]
    pic.depth = 24
    pic.colors = 0
    return pic


def write_cover(path: str, jpeg: dict, backup: bool = True) -> None:
    """Embed jpeg['bytes'] as the front cover of the FLAC at `path`.

    The original is patched on a temp copy in the same directory and swapped in
    with an atomic os.replace, so a crash/disconnect mid-write leaves the
    original intact.
    """
    if backup:
        bak = path + ".bak"
        if not os.path.exists(bak):
            try:
                shutil.copy2(path, bak)  # preserves mtime when possible
            except OSError:
                # CIFS/SMB mounts can reject metadata copy (utime/chmod);
                # fall back to a plain content copy so the backup still happens.
                shutil.copyfile(path, bak)

    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(suffix=".flactmp", dir=directory)
    os.close(fd)
    try:
        shutil.copyfile(path, tmp)
        audio = FLAC(tmp)
        audio.clear_pictures()
        audio.add_picture(_make_picture(jpeg))
        audio.save()
        os.replace(tmp, path)  # atomic on the same volume
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
