import pytest

import art_sources


class FakeResp:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise art_sources.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


def _patch_get(monkeypatch, json_data, capture=None):
    def fake_get(url, params=None, headers=None, timeout=None):
        if capture is not None:
            capture["url"] = url
            capture["params"] = params
        return FakeResp(json_data)
    monkeypatch.setattr(art_sources.requests, "get", fake_get)


def test_term():
    assert art_sources._term("q", "Artist", "Album") == "Artist Album"
    assert art_sources._term("q", None, "Album") == "Album"
    assert art_sources._term("q", None, None) == "q"


def test_itunes_parsing_and_upgrade(monkeypatch):
    cap = {}
    _patch_get(monkeypatch, {"results": [
        {"artworkUrl100": "https://x/100x100bb.jpg",
         "collectionName": "Disc", "artistName": "Band"},
        {"collectionName": "no artwork"},
    ]}, cap)
    out = art_sources.itunes("q", "Band", "Disc", "ua")
    assert len(out) == 1
    assert out[0]["url"] == "https://x/1200x1200bb.jpg"
    assert out[0]["source"] == "iTunes"
    assert cap["params"]["term"] == "Band Disc"


def test_itunes_offset_slicing(monkeypatch):
    cap = {}
    results = [{"artworkUrl100": f"https://x/{i}-100x100bb.jpg"} for i in range(10)]
    _patch_get(monkeypatch, {"results": results}, cap)
    out = art_sources.itunes("q", None, "Disc", "ua", limit=3, offset=5)
    assert len(out) == 3
    assert out[0]["url"].startswith("https://x/5-")
    assert cap["params"]["limit"] == 8  # offset + limit


def test_deezer_cover_fallback_chain(monkeypatch):
    _patch_get(monkeypatch, {"data": [
        {"cover_xl": "xl", "title": "A", "artist": {"name": "X"}},
        {"cover_big": "big", "title": "B"},
        {"title": "no cover"},
        {"cover_medium": "med", "title": "C"},
    ]})
    out = art_sources.deezer("q", None, "Disc", "ua")
    assert [o["url"] for o in out] == ["xl", "big", "med"]
    assert out[0]["artist"] == "X"


def test_coverart_query_branches_and_credit(monkeypatch):
    cap = {}
    _patch_get(monkeypatch, {"release-groups": [
        {"id": "mbid-1", "title": "RG",
         "artist-credit": [{"name": "A", "joinphrase": " & "}, {"name": "B"}]},
        {"title": "no id"},
    ]}, cap)
    out = art_sources.coverart("q", "Radiohead", "OK", "ua")
    assert len(out) == 1
    assert out[0]["url"] == "https://coverartarchive.org/release-group/mbid-1/front-1200"
    assert out[0]["artist"] == "A & B"
    assert 'releasegroup:"OK"' in cap["params"]["query"]

    # raw query branch when artist/album missing
    art_sources.coverart("just a query", None, None, "ua")
    # captured params updated by the second call
    assert cap["params"]["query"] == "just a query"


def test_gather_aggregates_and_swallows_failures(monkeypatch):
    monkeypatch.setattr(art_sources, "itunes",
                        lambda *a, **k: [{"source": "iTunes", "url": "u1"},
                                         {"source": "iTunes", "url": "u2"}])

    def boom(*a, **k):
        raise RuntimeError("deezer down")

    monkeypatch.setattr(art_sources, "deezer", boom)
    seen = {}

    def cover(query, artist, album, ua, limit=5, offset=0):
        seen["offset"] = offset
        return [{"source": "CoverArtArchive", "url": "u3"}]

    monkeypatch.setattr(art_sources, "coverart", cover)
    out = art_sources.gather_candidate_urls("q", "a", "b", "ua", page=2, per_source=5)
    assert len(out) == 3            # deezer failure swallowed
    assert seen["offset"] == 10     # page * per_source
