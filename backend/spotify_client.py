"""Spotify currently-playing poller.

Uses a long-lived refresh token (captured once via scripts/spotify_auth.py)
to request short-lived access tokens on demand, then calls
/v1/me/player/currently-playing and maps the response to TrackInfo.

Returns None when nothing is actively playing, so the caller can fall
through to audio-based recognition.
"""

import base64
import logging
import pathlib
import time

import aiohttp

from backend.models import TrackInfo

log = logging.getLogger("framedisplay")

TOKEN_URL = "https://accounts.spotify.com/api/token"
CURRENTLY_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_file: pathlib.Path,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self._refresh_token = self._load_refresh_token()
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0
        self._session: aiohttp.ClientSession | None = None

    def _load_refresh_token(self) -> str:
        if not self.token_file.exists():
            raise FileNotFoundError(
                f"Spotify token file not found: {self.token_file}. "
                "Run scripts/spotify_auth.py once to generate it."
            )
        token = self.token_file.read_text().strip()
        if not token:
            raise ValueError(f"Spotify token file is empty: {self.token_file}")
        return token

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "FrameDisplay/1.0"}
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _get_access_token(self) -> str | None:
        # Cache the access token until ~30s before expiry.
        if self._access_token and time.monotonic() < self._access_token_expires_at - 30:
            return self._access_token

        session = await self._get_session()
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {basic}"}
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        try:
            async with session.post(TOKEN_URL, headers=headers, data=data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("Spotify: token refresh failed (%d): %s", resp.status, body)
                    return None
                payload = await resp.json()
        except aiohttp.ClientError as e:
            log.warning("Spotify: token refresh network error: %s", e)
            return None

        self._access_token = payload.get("access_token")
        expires_in = payload.get("expires_in", 3600)
        self._access_token_expires_at = time.monotonic() + expires_in

        # Spotify rotates refresh tokens occasionally; persist if changed.
        new_refresh = payload.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            try:
                self.token_file.write_text(new_refresh)
            except OSError as e:
                log.warning("Spotify: could not persist rotated refresh token: %s", e)

        return self._access_token

    async def get_currently_playing(self) -> TrackInfo | None:
        """Return the current Spotify track, or None if nothing is actively playing."""
        access_token = await self._get_access_token()
        if not access_token:
            return None

        session = await self._get_session()
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            async with session.get(CURRENTLY_PLAYING_URL, headers=headers) as resp:
                if resp.status == 204:
                    return None  # nothing playing
                if resp.status == 401:
                    # Access token rejected — force refresh next call.
                    log.info("Spotify: access token rejected, will refresh")
                    self._access_token = None
                    return None
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(
                        "Spotify: currently-playing failed (%d): %s", resp.status, body
                    )
                    return None
                payload = await resp.json()
        except aiohttp.ClientError as e:
            log.warning("Spotify: currently-playing network error: %s", e)
            return None

        if not payload.get("is_playing"):
            return None

        item = payload.get("item")
        if not item or item.get("type") != "track":
            # Could be a podcast episode or nothing resolvable.
            return None

        return self._to_track_info(item)

    @staticmethod
    def _to_track_info(item: dict) -> TrackInfo:
        title = item.get("name", "Unknown")
        artists = item.get("artists", []) or []
        artist = ", ".join(a.get("name", "") for a in artists if a.get("name")) or "Unknown"
        album_obj = item.get("album") or {}
        album = album_obj.get("name")
        cover_url = _largest_image(album_obj.get("images", []))
        release_date = album_obj.get("release_date") or ""
        year = release_date.split("-")[0] if release_date else None

        return TrackInfo(
            title=title,
            artist=artist,
            album=album,
            cover_url=cover_url,
            year=year,
        )


def _largest_image(images: list) -> str | None:
    if not images:
        return None
    best = max(images, key=lambda i: (i.get("width") or 0) * (i.get("height") or 0))
    return best.get("url")
