import asyncio
import logging
import pathlib
import time

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
        self._lock = asyncio.Lock()
        self._mic_lock = asyncio.Lock()

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

        audio_cfg = self.config.get("audio", {})
        listeners = audio_cfg.get("listeners", [
            {"snippet_duration": 5, "loop_interval": 10},
        ])

        tasks = []
        for i, listener_cfg in enumerate(listeners):
            duration = listener_cfg.get("snippet_duration", 5)
            interval = listener_cfg.get("loop_interval", 10)
            label = f"listener-{duration}s"
            tasks.append(
                asyncio.create_task(
                    self._listen_loop(
                        label=label,
                        duration=duration,
                        sample_rate=audio_cfg.get("sample_rate", 44100),
                        device=audio_cfg.get("device"),
                        channels=audio_cfg.get("channels", 1),
                        interval=interval,
                    )
                )
            )
            log.info("Started %s (interval=%ds)", label, interval)

        await asyncio.gather(*tasks)

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

    async def _listen_loop(
        self,
        label: str,
        duration: float,
        sample_rate: int,
        device,
        channels: int,
        interval: int,
    ):
        while True:
            loop_start = time.monotonic()
            try:
                log.info("[%s] Waiting for mic...", label)
                async with self._mic_lock:
                    log.info("[%s] Recording %ss audio snippet...", label, duration)
                    rec_start = time.monotonic()
                    audio_bytes = await record_snippet(
                        duration=duration,
                        sample_rate=sample_rate,
                        device=device,
                        channels=channels,
                    )
                    rec_elapsed = time.monotonic() - rec_start
                    log.info(
                        "[%s] Recording done (%.1fs, %d bytes)",
                        label,
                        rec_elapsed,
                        len(audio_bytes),
                    )

                log.info("[%s] Sending to Shazam for recognition...", label)
                recog_start = time.monotonic()
                track = await self.recognizer.identify(audio_bytes)
                recog_elapsed = time.monotonic() - recog_start

                async with self._lock:
                    if track is None:
                        self._miss_count += 1
                        log.info(
                            "[%s] No match (%.1fs, miss %d/%d)",
                            label,
                            recog_elapsed,
                            self._miss_count,
                            MISS_THRESHOLD,
                        )
                        if (
                            self._miss_count >= MISS_THRESHOLD
                            and self.state != DisplayState.IDLE
                        ):
                            log.info("[%s] Miss threshold reached, going idle", label)
                            self.state = DisplayState.IDLE
                            self.current_track = None
                            await self._broadcast(self._build_message())
                        if interval:
                            await asyncio.sleep(interval)
                        continue

                    log.info(
                        "[%s] Recognized: %s - %s (%.1fs)",
                        label,
                        track.artist,
                        track.title,
                        recog_elapsed,
                    )
                    self._miss_count = 0

                    # Same song still playing
                    if (
                        self.current_track
                        and track.display_key == self.current_track.display_key
                    ):
                        log.info("[%s] Same track still playing, skipping update", label)
                        if interval:
                            await asyncio.sleep(interval)
                        continue

                    # Enrich via Discogs
                    if self.discogs:
                        log.info("[%s] Enriching via Discogs...", label)
                        discogs_start = time.monotonic()
                        track = await self.discogs.enrich(track)
                        log.info(
                            "[%s] Discogs done (%.1fs)",
                            label,
                            time.monotonic() - discogs_start,
                        )

                    self.current_track = track
                    self.state = DisplayState.IDENTIFIED
                    log.info("[%s] Now playing: %s - %s", label, track.artist, track.title)
                    await self._broadcast(self._build_message())

            except Exception:
                log.exception("[%s] Error in listen loop", label)

            loop_elapsed = time.monotonic() - loop_start
            if interval:
                log.info(
                    "[%s] Loop cycle took %.1fs, sleeping %ds",
                    label,
                    loop_elapsed,
                    interval,
                )
                await asyncio.sleep(interval)
            else:
                log.info("[%s] Loop cycle took %.1fs, no sleep", label, loop_elapsed)

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
