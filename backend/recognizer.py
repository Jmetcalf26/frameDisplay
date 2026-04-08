from shazamio import Shazam

from backend.models import TrackInfo


class Recognizer:
    def __init__(self):
        self.shazam = Shazam()

    async def identify(self, audio_bytes: bytes) -> TrackInfo | None:
        """Identify a track from raw WAV bytes. Returns None if no match."""
        result = await self.shazam.recognize(audio_bytes)

        track_data = result.get("track")
        if not track_data:
            return None

        return TrackInfo(
            title=track_data.get("title", "Unknown"),
            artist=track_data.get("subtitle", "Unknown"),
            cover_url=self._extract_cover(track_data),
        )

    @staticmethod
    def _extract_cover(track_data: dict) -> str | None:
        images = track_data.get("images", {})
        return images.get("coverarthq") or images.get("coverart")
