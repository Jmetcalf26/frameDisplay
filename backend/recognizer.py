import logging
import re

import aiohttp
from shazamio import Shazam

from backend.models import TrackInfo

log = logging.getLogger("framedisplay")

APPLE_MUSIC_SUFFIXES = ["cc", "bb", "sr"]


class Recognizer:
    def __init__(self):
        self.shazam = Shazam()

    async def identify(self, audio_bytes: bytes) -> TrackInfo | None:
        """Identify a track from raw WAV bytes. Returns None if no match."""
        result = await self.shazam.recognize(audio_bytes)

        track_data = result.get("track")
        if not track_data:
            return None

        album = self._extract_album(track_data)
        log.info("Shazam metadata - album: %s", album)

        raw_cover = self._extract_cover_url(track_data)
        cover_url = await self.resolve_cover(raw_cover) if raw_cover else None

        return TrackInfo(
            title=track_data.get("title", "Unknown"),
            artist=track_data.get("subtitle", "Unknown"),
            album=album,
            cover_url=cover_url,
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


def _apple_music_candidates(url: str, size: int = 3000) -> list[str]:
    """Generate candidate URLs for each suffix in priority order."""
    candidates = []
    for suffix in APPLE_MUSIC_SUFFIXES:
        result = re.sub(r"\d+x\d+[a-z]{2}", f"{size}x{size}{suffix}", url)
        if result != url:
            candidates.append(result)
    return candidates
