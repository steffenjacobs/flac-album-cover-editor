import os

from PIL import Image

import patcher
import scanner
from flacbuild import make_jpeg, write_flac


def _noise_jpeg(w, h):
    from io import BytesIO
    out = BytesIO()
    Image.effect_noise((w, h), 80).convert("RGB").save(out, format="JPEG", quality=95)
    return out.getvalue()


def test_reencode_downscales_and_caps_size():
    enc = patcher.reencode_jpeg(make_jpeg(3000, 3000), max_dim=1200)
    assert enc["w"] <= 1200 and enc["h"] <= 1200
    assert len(enc["bytes"]) < patcher.MAX_EMBED_BYTES


def test_reencode_size_cap_reduces_quality():
    # High-entropy image kept large so the q=88 JPEG exceeds 4 MB and the
    # quality-reduction loop runs (pure noise is incompressible, so the cap is
    # best-effort; what we assert is that quality was stepped down).
    enc = patcher.reencode_jpeg(_noise_jpeg(4000, 4000), max_dim=4000)
    assert enc["quality"] < patcher.EMBED_QUALITY


def test_write_cover_atomic_roundtrip_and_no_backup(tmp_path):
    p = write_flac(tmp_path / "a.flac", "Album", "Artist", picture=None)
    enc = patcher.reencode_jpeg(make_jpeg(1000, 1000))
    patcher.write_cover(p, enc, backup=False)
    assert not os.path.exists(p + ".bak")
    info = scanner.read_flac_fast(p)
    assert (info["front"]["real_w"], info["front"]["real_h"]) == (1000, 1000)
    # no leftover temp files
    assert not any(n.endswith(".flactmp") for n in os.listdir(tmp_path))


def test_write_cover_preserves_existing_bak(tmp_path):
    p = write_flac(tmp_path / "b.flac", "Album", "Artist", picture=None)
    bak = p + ".bak"
    with open(bak, "wb") as f:
        f.write(b"ORIGINAL-SENTINEL")
    patcher.write_cover(p, patcher.reencode_jpeg(make_jpeg(900, 900)), backup=True)
    with open(bak, "rb") as f:
        assert f.read() == b"ORIGINAL-SENTINEL"  # not overwritten


def test_write_cover_cifs_copystat_fallback(tmp_path, monkeypatch):
    p = write_flac(tmp_path / "c.flac", "Album", "Artist", picture=None)
    calls = {"copyfile": 0}
    real_copyfile = patcher.shutil.copyfile

    def boom(src, dst, *a, **k):
        raise OSError("CIFS rejects metadata copy")

    def counting_copyfile(src, dst, *a, **k):
        calls["copyfile"] += 1
        return real_copyfile(src, dst, *a, **k)

    monkeypatch.setattr(patcher.shutil, "copy2", boom)
    monkeypatch.setattr(patcher.shutil, "copyfile", counting_copyfile)
    patcher.write_cover(p, patcher.reencode_jpeg(make_jpeg(900, 900)), backup=True)
    # copyfile used for both the .bak fallback and the atomic temp copy
    assert calls["copyfile"] >= 1
    assert os.path.exists(p + ".bak")


def test_write_cover_cleans_temp_on_failure(tmp_path, monkeypatch):
    p = write_flac(tmp_path / "d.flac", "Album", "Artist", picture=None)

    monkeypatch.setattr(patcher, "FLAC", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        patcher.write_cover(p, patcher.reencode_jpeg(make_jpeg(800, 800)), backup=False)
        assert False, "should have raised"
    except RuntimeError:
        pass
    assert not any(n.endswith(".flactmp") for n in os.listdir(tmp_path))
