"""Album cover lookup: query every source in parallel, pick the biggest.

Spotify's Web API caps album art at 640x640, which looks small on a 4K
Frame TV. This module queries every known high-res source, measures the
actual delivered image dimensions, and returns whichever URL resolves
to the largest image.

Sources:
    - iTunes Search API          — Apple Music CDN, upgradable to 3000x3000.
    - MusicBrainz + Cover Art Archive
                                 — user-curated scans (any size the
                                   contributor uploaded; often 1500-3000+).
    - Deezer Search API          — 1000x1000 via cover_xl.
    - Spotify                    — caller's fallback (640x640 max).

All sources are stateless HTTP, no auth. We fetch each candidate image
in parallel, read width/height via Pillow, and return the max-area URL.
"""

import asyncio
import io
import logging

import aiohttp
from PIL import Image, UnidentifiedImageError

log = logging.getLogger("framedisplay")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
MUSICBRAINZ_SEARCH_URL = "https://musicbrainz.org/ws/2/release"
CAA_FRONT_URL = "https://coverartarchive.org/release/{mbid}/front"
DEEZER_SEARCH_URL = "https://api.deezer.com/search/album"

# MusicBrainz requires a descriptive User-Agent with contact info.
MB_USER_AGENT = "FrameDisplay/1.0 ( https://github.com/Jmetcalf26/frameDisplay )"

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)
# Measurement can involve downloading multi-megabyte originals (CAA
# sometimes serves gatefold scans). Give it more room than the metadata
# lookups above.
_MEASURE_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def find_best_cover(
    artist: str,
    album: str,
    spotify_url: str | None,
    resolve_apple_cdn,
) -> str | None:
    """Query every source, measure each candidate, return the max-res URL.

    ``resolve_apple_cdn`` is a coroutine that upgrades an Apple Music CDN
    URL (e.g. ``.../100x100bb.jpg``) to the largest available size. Passed
    in rather than imported so this module stays decoupled from Recognizer.
    """
    if not album:
        return spotify_url

    # Kick off all three third-party lookups in parallel.
    itunes_raw, caa_url, deezer_url = await asyncio.gather(
        _itunes_lookup(artist, album),
        _musicbrainz_caa_lookup(artist, album),
        _deezer_lookup(artist, album),
        return_exceptions=False,
    )

    # iTunes returns a 100x100 URL; upgrade to 3000x3000 via Apple CDN rewrite.
    itunes_url = None
    if itunes_raw:
        itunes_url = await resolve_apple_cdn(itunes_raw)

    candidates: list[tuple[str, str]] = []
    if itunes_url:
        candidates.append(("iTunes", itunes_url))
    if caa_url:
        candidates.append(("MB/CAA", caa_url))
    if deezer_url:
        candidates.append(("Deezer", deezer_url))
    if spotify_url:
        candidates.append(("Spotify", spotify_url))

    if not candidates:
        return None

    # Measure every candidate in parallel.
    async with aiohttp.ClientSession(timeout=_MEASURE_TIMEOUT) as session:
        sizes = await asyncio.gather(
            *[_measure_image(session, url) for _, url in candidates]
        )

    best: tuple[str, str, tuple[int, int]] | None = None
    for (label, url), dims in zip(candidates, sizes):
        if dims is None:
            log.info("Cover candidate %s: measurement failed (%s)", label, url)
            continue
        log.info("Cover candidate %s: %dx%d (%s)", label, dims[0], dims[1], url)
        if best is None or (dims[0] * dims[1]) > (best[2][0] * best[2][1]):
            best = (label, url, dims)

    if best is None:
        # Every measurement failed. Last-resort: prefer Spotify URL if we
        # have one, otherwise the first candidate we found.
        log.info("Cover: no candidate measurable; falling back blind")
        for label, url in candidates:
            if label == "Spotify":
                return url
        return candidates[0][1]

    log.info(
        "Cover winner: %s %dx%d (%s)",
        best[0], best[2][0], best[2][1], best[1],
    )
    return best[1]


async def _measure_image(session: aiohttp.ClientSession, url: str) -> tuple[int, int] | None:
    """Fetch ``url`` and return its (width, height). Returns None on failure."""
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except (UnidentifiedImageError, OSError):
        return None


# ----- individual sources -----


async def _itunes_lookup(artist: str, album: str) -> str | None:
    term = f"{artist} {album}".strip()
    params = {"term": term, "entity": "album", "limit": "5"}
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(ITUNES_SEARCH_URL, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        log.info("iTunes: network error")
        return None

    results = data.get("results", []) or []
    if not results:
        return None

    artist_lc = artist.lower()
    album_lc = album.lower()

    def score(r: dict) -> tuple[int, int]:
        a = (r.get("artistName") or "").lower()
        c = (r.get("collectionName") or "").lower()
        return (
            1 if a == artist_lc else 0,
            1 if c == album_lc else 0,
        )

    best = max(results, key=score)
    if score(best) == (0, 0):
        return None
    return best.get("artworkUrl100")


async def _musicbrainz_caa_lookup(artist: str, album: str) -> str | None:
    """Search MusicBrainz for the release, then probe Cover Art Archive."""
    query = f'artist:"{_mb_escape(artist)}" AND release:"{_mb_escape(album)}"'
    params = {"query": query, "fmt": "json", "limit": "5"}
    headers = {"User-Agent": MB_USER_AGENT}

    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT, headers=headers) as session:
            async with session.get(MUSICBRAINZ_SEARCH_URL, params=params) as resp:
                if resp.status != 200:
                    log.info("MusicBrainz: search failed (%d)", resp.status)
                    return None
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        log.info("MusicBrainz: network error")
        return None

    releases = data.get("releases", []) or []
    if not releases:
        return None

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        for release in releases:
            mbid = release.get("id")
            if not mbid:
                continue
            url = CAA_FRONT_URL.format(mbid=mbid)
            try:
                async with session.head(url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        return url
            except aiohttp.ClientError:
                continue
    return None


async def _deezer_lookup(artist: str, album: str) -> str | None:
    """Deezer has a fielded search syntax: artist:"X" album:"Y"."""
    query = f'artist:"{artist}" album:"{album}"'
    params = {"q": query, "limit": "5"}
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(DEEZER_SEARCH_URL, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        log.info("Deezer: network error")
        return None

    results = data.get("data", []) or []
    if not results:
        return None

    artist_lc = artist.lower()
    album_lc = album.lower()

    def score(r: dict) -> tuple[int, int]:
        a = ((r.get("artist") or {}).get("name") or "").lower()
        c = (r.get("title") or "").lower()
        return (
            1 if a == artist_lc else 0,
            1 if c == album_lc else 0,
        )

    best = max(results, key=score)
    if score(best) == (0, 0):
        return None
    return best.get("cover_xl") or best.get("cover_big")


def _mb_escape(s: str) -> str:
    """Escape characters that are special in MusicBrainz' Lucene query syntax."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
