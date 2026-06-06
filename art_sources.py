"""
Album-art candidate sources.

All free, no-auth (except none required here), and purpose-built for album art:
  - iTunes Search API   (no auth; upgrade artworkUrl100 -> 1200x1200)
  - Deezer API          (no auth; cover_xl = 1000x1000)
  - Cover Art Archive    (via a MusicBrainz release-group lookup)

Each function returns a list of {source, url, title, artist} dicts. The caller
downloads the URLs (through the backend) and probes their real dimensions.
"""

from __future__ import annotations

import requests

TIMEOUT = 12


def _term(query, artist, album):
    if artist and album:
        return f"{artist} {album}"
    return album or query


def itunes(query, artist, album, ua, limit=5, offset=0):
    # iTunes has no offset param, so request offset+limit and slice the tail.
    total = min(offset + limit, 200)
    r = requests.get(
        "https://itunes.apple.com/search",
        params={
            "term": _term(query, artist, album),
            "media": "music",
            "entity": "album",
            "limit": total,
            "country": "US",
        },
        headers={"User-Agent": ua},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for it in r.json().get("results", [])[offset:offset + limit]:
        art = it.get("artworkUrl100")
        if not art:
            continue
        # Upgrade the thumbnail URL to a high-resolution variant.
        hi = art.replace("100x100bb", "1200x1200bb")
        out.append(
            {
                "source": "iTunes",
                "url": hi,
                "title": it.get("collectionName"),
                "artist": it.get("artistName"),
            }
        )
    return out


def deezer(query, artist, album, ua, limit=5, offset=0):
    r = requests.get(
        "https://api.deezer.com/search/album",
        params={"q": _term(query, artist, album), "limit": limit, "index": offset},
        headers={"User-Agent": ua},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for it in r.json().get("data", []):
        cover = it.get("cover_xl") or it.get("cover_big") or it.get("cover_medium")
        if not cover:
            continue
        out.append(
            {
                "source": "Deezer",
                "url": cover,
                "title": it.get("title"),
                "artist": (it.get("artist") or {}).get("name"),
            }
        )
    return out


def coverart(query, artist, album, ua, limit=5, offset=0):
    """MusicBrainz release-group search -> Cover Art Archive front image.
    MusicBrainz requires a descriptive User-Agent and ~1 req/s (fine: this is
    only called on demand, once per album)."""
    if artist and album:
        mb_query = f'releasegroup:"{album}" AND artist:"{artist}"'
    else:
        mb_query = album or query
    r = requests.get(
        "https://musicbrainz.org/ws/2/release-group/",
        params={"query": mb_query, "fmt": "json", "limit": limit, "offset": offset},
        headers={"User-Agent": ua},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for rg in r.json().get("release-groups", []):
        mbid = rg.get("id")
        if not mbid:
            continue
        credits = rg.get("artist-credit") or []
        rg_artist = "".join(
            (c.get("name", "") + (c.get("joinphrase", "") or ""))
            for c in credits
            if isinstance(c, dict)
        ).strip() or None
        out.append(
            {
                "source": "CoverArtArchive",
                # 307-redirects to the actual image; requests follows redirects.
                "url": f"https://coverartarchive.org/release-group/{mbid}/front-1200",
                "title": rg.get("title"),
                "artist": rg_artist,
            }
        )
    return out


def gather_candidate_urls(query, artist, album, ua, page=0, per_source=5):
    """Query all sources for the given page; ignore individual source failures.
    page is 0-based; each source returns `per_source` results at offset
    page*per_source."""
    offset = page * per_source
    out = []
    for fn in (itunes, deezer, coverart):
        try:
            out.extend(fn(query, artist, album, ua, limit=per_source, offset=offset))
        except Exception:
            pass
    return out
