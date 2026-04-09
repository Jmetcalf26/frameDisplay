import json
import logging

import aiohttp

from backend.models import TrackInfo

log = logging.getLogger("framedisplay")


class DiscogsClient:
    BASE_URL = "https://api.discogs.com"

    def __init__(self, consumer_key: str, consumer_secret: str):
        self._auth_params = {
            "key": consumer_key,
            "secret": consumer_secret,
        }
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "FrameDisplay/1.0"}
            )
        return self._session

    async def enrich(self, track: TrackInfo) -> TrackInfo:
        """Search Discogs for the track and add label, year, genre, and hi-res art."""
        session = await self._get_session()
        query_parts = [track.artist]
        if track.album:
            query_parts.append(track.album)
        else:
            query_parts.append(track.title)
        params = {
            **self._auth_params,
            "q": " ".join(query_parts),
            "type": "release",
            "per_page": "1",
        }

        try:
            async with session.get(
                f"{self.BASE_URL}/database/search", params=params
            ) as resp:
                if resp.status != 200:
                    return track
                data = await resp.json()
        except aiohttp.ClientError:
            return track

        results = data.get("results", [])
        if not results:
            log.info("Discogs: no results for query")
            return track

        release = results[0]
        log.info("Discogs search result:\n%s", json.dumps(release, indent=2))
        track.year = release.get("year")
        track.genre = ", ".join(release.get("genre", []))
        track.label = ", ".join(release.get("label", []))

        # Fetch full release details for high-res artwork
        release_id = release.get("id")
        if release_id:
            hires_url = await self._get_primary_image(session, release_id)
            if hires_url:
                track.cover_url_hires = hires_url
                log.info("Discogs hi-res image: %s", hires_url)

        # Fall back to search result cover_image
        if not track.cover_url_hires and release.get("cover_image"):
            track.cover_url_hires = release["cover_image"]

        return track

    async def _get_primary_image(self, session, release_id: int) -> str | None:
        """Fetch the primary full-res image from a release's detail endpoint."""
        try:
            async with session.get(
                f"{self.BASE_URL}/releases/{release_id}",
                params=self._auth_params,
            ) as resp:
                if resp.status != 200:
                    log.info("Discogs release fetch failed: %d", resp.status)
                    return None
                data = await resp.json()
        except aiohttp.ClientError:
            return None

        images = data.get("images", [])
        if not images:
            return None

        # Prefer the primary image, fall back to first available
        for img in images:
            if img.get("type") == "primary":
                log.info("Discogs primary image: %dx%d", img.get("width", 0), img.get("height", 0))
                return img.get("uri")

        return images[0].get("uri")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
