import logging

from shazamio import Shazam

from backend.models import TrackInfo

log = logging.getLogger("framedisplay")


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

        return TrackInfo(
            title=track_data.get("title", "Unknown"),
            artist=track_data.get("subtitle", "Unknown"),
            album=album,
            cover_url=self._extract_cover(track_data),
        )

    @staticmethod
    def _extract_album(track_data: dict) -> str | None:
        for section in track_data.get("sections", []):
            for item in section.get("metadata", []):
                if item.get("title", "").lower() == "album":
                    return item.get("text")
        return None

    @staticmethod
    def _extract_cover(track_data: dict) -> str | None:
        images = track_data.get("images", {})
        return images.get("coverarthq") or images.get("coverart")
