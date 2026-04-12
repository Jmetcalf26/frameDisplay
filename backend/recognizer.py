import asyncio
import logging
import re

import aiohttp
from shazamio import Shazam

from backend.models import TrackInfo

log = logging.getLogger("framedisplay")

APPLE_MUSIC_SUFFIXES = ["cc", "bb", "sr"]
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


class Recognizer:
    def __init__(self):
        self.shazam = Shazam()

    async def identify(self, audio_bytes: bytes) -> TrackInfo | None:
        """Identify a track from raw WAV bytes. Returns None if no match.

        Returns TrackInfo with the raw Shazam cover URL — the caller is
        responsible for resolving it via resolve_cover() if desired.
        """
        result = await self.shazam.recognize(audio_bytes)

        track_data = result.get("track")
        if not track_data:
            return None

        album = self._extract_album(track_data)
        log.info("Shazam metadata - album: %s", album)

        return TrackInfo(
            title=track_data.get("title", "Unknown"),
            artist=track_data.get("subtitle", "Unknown"),
            album=album,
            cover_url=self._extract_cover_url(track_data),
        )

    @staticmethod
    def _extract_album(track_data: dict) -> str | None:
        for section in track_data.get("sections", []):
            for item in section.get("metadata", []):
                if item.get("title", "").lower() == "album":
                    return item.get("text")
        return None

    @staticmethod
    def _extract_cover_url(track_data: dict) -> str | None:
        images = track_data.get("images", {})
        return images.get("coverarthq") or images.get("coverart")

    @staticmethod
    async def resolve_cover(url: str, size: int = 3000) -> str:
        """Try Apple Music CDN suffixes (cc, bb, sr) and return the first that works."""
        candidates = _apple_music_candidates(url, size)
        if not candidates:
            return url

        async with aiohttp.ClientSession() as session:
            for candidate in candidates:
                try:
                    async with session.head(candidate, allow_redirects=True) as resp:
                        if resp.status == 200:
                            log.info("Apple Music cover resolved: %s", candidate)
                            return candidate
                        log.info("Apple Music cover %d: %s", resp.status, candidate)
                except aiohttp.ClientError:
                    log.info("Apple Music cover failed: %s", candidate)

        log.info("All Apple Music variants failed, using original: %s", url)
        return url


async def itunes_lookup_cover(artist: str, album: str) -> str | None:
    """Look up an album on the iTunes Search API and return its raw artwork URL.

    Returns the `artworkUrl100` field from the best-matching album result,
    which is an Apple Music CDN URL that can be passed to resolve_cover()
    to upgrade to a large size (3000x3000). Returns None on miss or error.
    """
    term = f"{artist} {album}".strip()
    if not term:
        return None

    params = {"term": term, "entity": "album", "limit": "5"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ITUNES_SEARCH_URL, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    log.info("iTunes search failed: %d", resp.status)
                    return None
                data = await resp.json(content_type=None)  # iTunes returns text/javascript
    except (aiohttp.ClientError, asyncio.TimeoutError):
        log.info("iTunes search network error")
        return None

    results = data.get("results", []) or []
    if not results:
        log.info("iTunes: no results for '%s'", term)
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
    url = best.get("artworkUrl100")
    if not url:
        return None
    log.info(
        "iTunes match: %s - %s",
        best.get("artistName"),
        best.get("collectionName"),
    )
    return url


def _apple_music_candidates(url: str, size: int = 3000) -> list[str]:
    """Generate candidate URLs for each suffix in priority order."""
    candidates = []
    for suffix in APPLE_MUSIC_SUFFIXES:
        result = re.sub(r"\d+x\d+[a-z]{2}", f"{size}x{size}{suffix}", url)
        if result != url:
            candidates.append(result)
    return candidates
