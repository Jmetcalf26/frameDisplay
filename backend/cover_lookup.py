"""Album cover lookup chain for Spotify-sourced tracks.

Spotify's Web API caps album art at 640x640, which looks small on a 4K
Frame TV. This module tries a sequence of higher-resolution sources and
returns the first one that matches, falling through to Spotify's URL if
nothing better is found.

Chain order (best quality / coverage first):
    1. iTunes Search API         — 3000x3000 via Apple Music CDN rewrite.
                                   Great for mainstream releases.
    2. MusicBrainz + Cover Art Archive
                                 — user-curated scans, often 1500-3000+.
                                   Best coverage for indie / obscure.
    3. Deezer Search API         — up to 1000x1000. Covers mainstream
                                   misses from iTunes (different catalog).
    4. Spotify's 640x640         — caller's fallback, passed as `spotify_url`.

All lookups are stateless HTTP with short timeouts and no auth.
"""

import asyncio
import logging
import urllib.parse

import aiohttp

log = logging.getLogger("framedisplay")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
MUSICBRAINZ_SEARCH_URL = "https://musicbrainz.org/ws/2/release"
CAA_FRONT_URL = "https://coverartarchive.org/release/{mbid}/front"
DEEZER_SEARCH_URL = "https://api.deezer.com/search/album"

# MusicBrainz requires a descriptive User-Agent with contact info.
MB_USER_AGENT = "FrameDisplay/1.0 ( https://github.com/Jmetcalf26/frameDisplay )"

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=5)


async def find_best_cover(
    artist: str,
    album: str,
    spotify_url: str | None,
    resolve_apple_cdn,
) -> str | None:
    """Try each cover source in turn; return the first URL that matches.

    ``resolve_apple_cdn`` is a coroutine that upgrades an Apple Music CDN
    URL (e.g. ``.../100x100bb.jpg``) to the largest available size. We
    inject it rather than importing Recognizer here to avoid a circular
    import.
    """
    if not album:
        return spotify_url

    # 1. iTunes
    itunes_url = await _itunes_lookup(artist, album)
    if itunes_url:
        upgraded = await resolve_apple_cdn(itunes_url)
        log.info("Cover source: iTunes (%s)", upgraded)
        return upgraded

    # 2. MusicBrainz + Cover Art Archive
    caa_url = await _musicbrainz_caa_lookup(artist, album)
    if caa_url:
        log.info("Cover source: MusicBrainz/CAA (%s)", caa_url)
        return caa_url

    # 3. Deezer
    deezer_url = await _deezer_lookup(artist, album)
    if deezer_url:
        log.info("Cover source: Deezer (%s)", deezer_url)
        return deezer_url

    # 4. Fall back to whatever Spotify gave us.
    if spotify_url:
        log.info("Cover source: Spotify 640 fallback")
    return spotify_url


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
    # Only trust a match if at least the artist or album matches exactly;
    # otherwise iTunes has guessed and we should try the next source.
    if score(best) == (0, 0):
        log.info("iTunes: no confident match for '%s - %s'", artist, album)
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

    # Try releases in MB's relevance order; stop on the first that has art.
    # Limit to top 5 already via ?limit=5. For each: HEAD the CAA front URL.
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
    # Double quotes and backslashes are the important ones for our quoted terms.
    return s.replace("\\", "\\\\").replace('"', '\\"')
