import asyncio
import logging
import pathlib

import aiohttp.web

from backend.audio import record_snippet
from backend.discogs import DiscogsClient
from backend.models import DisplayState, TrackInfo
from backend.recognizer import Recognizer

log = logging.getLogger("framedisplay")

FRONTEND_DIR = pathlib.Path(__file__).resolve().parent.parent / "frontend"
MISS_THRESHOLD = 3


class FrameDisplayApp:
    def __init__(self, config: dict):
        self.config = config
        self.recognizer = Recognizer()

        discogs_cfg = config.get("discogs", {})
        if discogs_cfg.get("enabled") and discogs_cfg.get("consumer_key"):
            self.discogs = DiscogsClient(
                discogs_cfg["consumer_key"],
                discogs_cfg["consumer_secret"],
            )
        else:
            self.discogs = None

        self.ws_clients: set[aiohttp.web.WebSocketResponse] = set()
        self.current_track: TrackInfo | None = None
        self.state: DisplayState = DisplayState.IDLE
        self._miss_count = 0

    async def start(self):
        app = aiohttp.web.Application()
        app.router.add_get("/ws", self._ws_handler)
        app.router.add_static("/", FRONTEND_DIR, show_index=True)

        runner = aiohttp.web.AppRunner(app)
        await runner.setup()

        srv_cfg = self.config.get("server", {})
        site = aiohttp.web.TCPSite(
            runner,
            srv_cfg.get("host", "0.0.0.0"),
            srv_cfg.get("port", 8080),
        )
        await site.start()
        log.info("Serving on http://%s:%s", srv_cfg.get("host"), srv_cfg.get("port"))

        await self._listen_loop()

    async def _ws_handler(self, request):
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.add(ws)
        await ws.send_json(self._build_message())
        try:
            async for _ in ws:
                pass
        finally:
            self.ws_clients.discard(ws)
        return ws

    async def _broadcast(self, data: dict):
        dead: set[aiohttp.web.WebSocketResponse] = set()
        for ws in self.ws_clients:
            try:
                await ws.send_json(data)
            except (ConnectionError, ConnectionResetError):
                dead.add(ws)
        self.ws_clients -= dead

    async def _listen_loop(self):
        audio_cfg = self.config.get("audio", {})
        duration = audio_cfg.get("snippet_duration", 5)
        sample_rate = audio_cfg.get("sample_rate", 44100)
        device = audio_cfg.get("device")
        channels = audio_cfg.get("channels", 1)
        interval = audio_cfg.get("loop_interval", 10)

        while True:
            try:
                self.state = DisplayState.LISTENING
                audio_bytes = await record_snippet(
                    duration=duration,
                    sample_rate=sample_rate,
                    device=device,
                    channels=channels,
                )

                track = await self.recognizer.identify(audio_bytes)

                if track is None:
                    self._miss_count += 1
                    if (
                        self._miss_count >= MISS_THRESHOLD
                        and self.state != DisplayState.IDLE
                    ):
                        self.state = DisplayState.IDLE
                        self.current_track = None
                        await self._broadcast(self._build_message())
                    await asyncio.sleep(interval)
                    continue

                self._miss_count = 0

                # Same song still playing
                if (
                    self.current_track
                    and track.display_key == self.current_track.display_key
                ):
                    await asyncio.sleep(interval)
                    continue

                # Enrich via Discogs
                if self.discogs:
                    track = await self.discogs.enrich(track)

                self.current_track = track
                self.state = DisplayState.IDENTIFIED
                log.info("Now playing: %s - %s", track.artist, track.title)
                await self._broadcast(self._build_message())

            except Exception:
                log.exception("Error in listen loop")

            await asyncio.sleep(interval)

    def _build_message(self) -> dict:
        msg: dict = {"state": self.state.value}
        if self.current_track:
            t = self.current_track
            msg["track"] = {
                "title": t.title,
                "artist": t.artist,
                "album": t.album,
                "cover_url": t.cover_url_hires or t.cover_url,
                "year": t.year,
                "genre": t.genre,
                "label": t.label,
            }
        return msg
