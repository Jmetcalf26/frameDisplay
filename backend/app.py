import asyncio
import logging
import pathlib
import time

import aiohttp.web

from backend.audio import record_with_snapshots
from backend.cache import TrackCache
from backend.discogs import DiscogsClient
from backend.image_cache import ImageCache
from backend.models import DisplayState, TrackInfo
from backend.recognizer import Recognizer

log = logging.getLogger("framedisplay")

FRONTEND_DIR = pathlib.Path(__file__).resolve().parent.parent / "frontend"
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


class FrameDisplayApp:
    def __init__(
        self,
        config: dict,
        cache: TrackCache | None = None,
        image_cache: ImageCache | None = None,
    ):
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

        if cache is not None:
            self.cache = cache
        else:
            cache_cfg = config.get("cache", {})
            if cache_cfg.get("enabled", True):
                cache_path = pathlib.Path(cache_cfg.get("path", "cache/tracks.json"))
                if not cache_path.is_absolute():
                    cache_path = PROJECT_ROOT / cache_path
                self.cache = TrackCache(
                    path=cache_path,
                    max_bytes=cache_cfg.get("max_bytes", 524288),  # 512 KB default
                )
                log.info(
                    "Track cache: %s (%d entries, %d / %d bytes)",
                    cache_path,
                    len(self.cache),
                    self.cache.size_bytes(),
                    self.cache.max_bytes,
                )
            else:
                self.cache = None

        if image_cache is not None:
            self.image_cache = image_cache
        else:
            img_cfg = config.get("image_cache", {})
            if img_cfg.get("enabled", True):
                img_dir = pathlib.Path(img_cfg.get("dir", "cache/images"))
                if not img_dir.is_absolute():
                    img_dir = PROJECT_ROOT / img_dir
                self.image_cache = ImageCache(
                    dir=img_dir,
                    max_bytes=img_cfg.get("max_bytes", 100 * 1024 * 1024),  # 100 MB
                )
                log.info(
                    "Image cache: %s (%d entries, %d / %d bytes)",
                    img_dir,
                    len(self.image_cache),
                    self.image_cache.total_bytes(),
                    self.image_cache.max_bytes,
                )
            else:
                self.image_cache = None

        self.ws_clients: set[aiohttp.web.WebSocketResponse] = set()
        self.current_track: TrackInfo | None = None
        self.state: DisplayState = DisplayState.LISTENING
        self._current_audio_end: float = 0.0
        self._current_audio_start: float = 0.0
        # Serialize shazamio calls — concurrent recognize() calls can race / corrupt state
        self._recognize_lock = asyncio.Lock()

    async def start(self):
        app = aiohttp.web.Application()
        app.router.add_get("/ws", self._ws_handler)
        if self.image_cache is not None:
            app.router.add_static(
                ImageCache.URL_PREFIX, self.image_cache.dir, show_index=False,
            )
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

        try:
            await self._listen_loop()
        finally:
            log.info("Shutting down...")
            await runner.cleanup()
            await self.shutdown()

    async def shutdown(self):
        """Release external resources held by the app."""
        if self.discogs is not None:
            await self.discogs.close()
            log.info("Closed Discogs client")

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

    async def _handle_recognition(
        self, label: str, audio_bytes: bytes, audio_end_time: float, audio_start_time: float = 0.0,
    ):
        """Recognize audio and update display if a new track is found."""
        log.info("[%s] Waiting for recognizer lock...", label)
        async with self._recognize_lock:
            log.info("[%s] Sending to Shazam...", label)
            recog_start = time.monotonic()
            track = await self.recognizer.identify(audio_bytes)
            recog_elapsed = time.monotonic() - recog_start

        if track is None:
            log.info("[%s] No match (%.1fs)", label, recog_elapsed)
            return

        log.info(
            "[%s] Recognized: %s - %s (%.1fs)",
            label,
            track.artist,
            track.title,
            recog_elapsed,
        )
        if (
            self.current_track
            and track.display_key == self.current_track.display_key
        ):
            log.info("[%s] Same track still playing, skipping update", label)
            return

        # Reject stale results: if this audio ended before the current track's audio,
        # it's outdated (e.g. a cumulative snapshot finishing after a windowed one already matched)
        # At equal end times, prefer the result with fresher (later-starting) audio —
        # a windowed snapshot beats a cumulative one at the same snapshot point.
        if audio_end_time < self._current_audio_end or (
            audio_end_time == self._current_audio_end
            and audio_start_time < self._current_audio_start
        ):
            log.info(
                "[%s] Stale result (audio %s, current %s-%s), skipping",
                label,
                f"{audio_start_time:.1f}-{audio_end_time:.1f}",
                f"{self._current_audio_start:.1f}",
                f"{self._current_audio_end:.1f}",
            )
            return

        cached = self.cache.get(track.display_key) if self.cache is not None else None
        if cached is not None:
            log.info("[%s] Track cache hit for %s", label, track.display_key)
            track = cached
        else:
            raw_cover = track.cover_url
            if raw_cover:
                cover_start = time.monotonic()
                track.cover_url = await self.recognizer.resolve_cover(raw_cover)
                log.info(
                    "[%s] Cover resolved (%.1fs)", label, time.monotonic() - cover_start,
                )

            if self.discogs:
                log.info("[%s] Enriching via Discogs...", label)
                discogs_start = time.monotonic()
                track = await self.discogs.enrich(track)
                log.info("[%s] Discogs done (%.1fs)", label, time.monotonic() - discogs_start)

            if self.cache is not None:
                self.cache.put(track.display_key, track)
                log.info(
                    "[%s] Cached %s (track cache: %d entries, %d bytes)",
                    label,
                    track.display_key,
                    len(self.cache),
                    self.cache.size_bytes(),
                )

        # Make sure the album image is in the local image cache (downloads on miss).
        # Done in both branches so cache hits still keep their album image fresh.
        if self.image_cache is not None and track.album:
            source = track.cover_url or track.cover_url_hires
            if source and not source.startswith(ImageCache.URL_PREFIX):
                await self.image_cache.ensure(track.artist, track.album, source)

        self.current_track = track
        self._current_audio_end = audio_end_time
        self._current_audio_start = audio_start_time
        self.state = DisplayState.IDENTIFIED
        log.info("[%s] Now playing: %s - %s", label, track.artist, track.title)
        await self._broadcast(self._build_message())

    async def _listen_loop(self):
        audio_cfg = self.config.get("audio", {})
        sample_rate = audio_cfg.get("sample_rate", 44100)
        device = audio_cfg.get("device")
        channels = audio_cfg.get("channels", 1)
        interval = audio_cfg.get("loop_interval", 10)

        listeners = audio_cfg.get("listeners", [{"snippet_duration": 5}])
        durations = sorted(l.get("snippet_duration", 5) for l in listeners)
        total_duration = max(durations)
        # Snapshots are all durations except the longest (which is the full recording)
        snapshot_durations = [d for d in durations if d < total_duration]

        log.info(
            "Listen loop: record %ds, snapshots at %s",
            total_duration,
            snapshot_durations or "none",
        )

        while True:
            loop_start = time.monotonic()
            try:
                log.info("Recording %ds (snapshots at %s)...", total_duration, snapshot_durations)

                async def on_snapshot(label, wav_bytes, audio_start_time, audio_end_time):
                    log.info("[%s] Snapshot ready (%d bytes)", label, len(wav_bytes))
                    await self._handle_recognition(label, wav_bytes, audio_end_time, audio_start_time)

                full_wav = await record_with_snapshots(
                    total_duration=total_duration,
                    snapshot_at=snapshot_durations,
                    sample_rate=sample_rate,
                    device=device,
                    channels=channels,
                    on_snapshot=on_snapshot,
                    record_start_time=loop_start,
                )

                full_audio_end = loop_start + total_duration
                log.info("Full recording ready (%d bytes)", len(full_wav))
                await self._handle_recognition(
                    f"full-{total_duration:.0f}s", full_wav, full_audio_end, loop_start,
                )

            except Exception:
                log.exception("Error in listen loop")

            loop_elapsed = time.monotonic() - loop_start
            if interval:
                log.info("Loop cycle took %.1fs, sleeping %ds", loop_elapsed, interval)
                await asyncio.sleep(interval)
            else:
                log.info("Loop cycle took %.1fs, no sleep", loop_elapsed)

    def _build_message(self) -> dict:
        msg: dict = {"state": self.state.value}
        if self.current_track:
            t = self.current_track
            cover = t.cover_url or t.cover_url_hires
            # Prefer locally cached image when available
            if t.album and self.image_cache is not None:
                local = self.image_cache.local_url_if_present(t.artist, t.album)
                if local:
                    cover = local
            msg["track"] = {
                "title": t.title,
                "artist": t.artist,
                "album": t.album,
                "cover_url": cover,
                "year": t.year,
                "genre": t.genre,
                "label": t.label,
            }
        return msg
