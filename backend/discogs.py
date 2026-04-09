import aiohttp

from backend.models import TrackInfo


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
            return track

        release = results[0]
        track.year = release.get("year")
        track.genre = ", ".join(release.get("genre", []))
        track.label = ", ".join(release.get("label", []))
        if release.get("cover_image"):
            track.cover_url_hires = release["cover_image"]

        return track

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
