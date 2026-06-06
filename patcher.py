"""
Re-encode a chosen cover image to JPEG and embed it into FLAC files.

Writing uses mutagen (pure-Python, reliable). We:
  - downscale to at most 1200x1200 (never upscale) and re-encode JPEG,
  - keep the result well under 4 MB (a Windows-11 Explorer display bug corrupts
    FLAC metadata above ~4 MB; the hard FLAC block limit is 16 MiB),
  - clear existing PICTURE blocks first (writers append, not replace),
  - set picture type 3 (front cover) with correct width/height (mutagen does NOT
    auto-fill these).
"""

from __future__ import annotations

import os
import shutil
import warnings
from io import BytesIO

from PIL import Image
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

Image.MAX_IMAGE_PIXELS = None
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

try:
    RESAMPLE = Image.Resampling.LANCZOS  # Pillow >= 9.1
except AttributeError:  # pragma: no cover - very old Pillow
    RESAMPLE = Image.LANCZOS

MAX_EMBED_BYTES = 4 * 1024 * 1024  # stay under the Windows Explorer threshold


def reencode_jpeg(data: bytes, max_dim: int = 1200, quality: int = 88) -> dict:
    """Return {'bytes', 'w', 'h'} of a JPEG suitable for embedding."""
    im = Image.open(BytesIO(data)).convert("RGB")
    im.thumbnail((max_dim, max_dim), RESAMPLE)  # never enlarges

    def encode(q):
        out = BytesIO()
        im.save(out, format="JPEG", quality=q, subsampling="4:2:0", optimize=True)
        return out.getvalue()

    q = quality
    blob = encode(q)
    while len(blob) > MAX_EMBED_BYTES and q > 55:
        q -= 8
        blob = encode(q)

    return {"bytes": blob, "w": im.width, "h": im.height, "quality": q}


def write_cover(path: str, jpeg: dict, backup: bool = True):
    """Embed jpeg['bytes'] as the front cover of the FLAC at `path`."""
    if backup:
        bak = path + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)

    audio = FLAC(path)
    audio.clear_pictures()  # avoid duplicate covers

    pic = Picture()
    pic.type = PictureType.COVER_FRONT  # == 3
    pic.mime = "image/jpeg"
    pic.desc = "Front cover"
    pic.data = jpeg["bytes"]
    pic.width = jpeg["w"]
    pic.height = jpeg["h"]
    pic.depth = 24
    pic.colors = 0

    audio.add_picture(pic)
    audio.save()
