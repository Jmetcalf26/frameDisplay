import asyncio
import logging
import pathlib
import time

import aiohttp.web

from backend.acoustid_client import AcoustIDClient
from backend.audio import record_with_snapshots
from backend.cache import TrackCache
from backend.composer import Composer
from backend.discogs import DiscogsClient
from backend.frame_tv import FrameTV
from backend.image_cache import ImageCache
from backend.models import DisplayState, TrackInfo
from backend.recognizer import Recognizer
from backend.spotify_client import SpotifyClient

log = logging.getLogger("framedisplay")

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Tiny inline HTML for the debug preview page — displays /current.jpg and
# auto-refreshes every few seconds. Kept inline so there's no frontend dir.
_PREVIEW_HTML = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>frameDisplay preview</title></head>
<body style="margin:0;background:#000;display:flex;align-items:center;justify-content:center;height:100vh">
  <img id="img" style="max-width:100vw;max-height:100vh;object-fit:contain">
  <script>
    const img = document.getElementById("img");
    function refresh() { img.src = "/current.jpg?t=" + Date.now(); }
    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""


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

        acoustid_cfg = config.get("acoustid", {})
        if acoustid_cfg.get("enabled") and acoustid_cfg.get("api_key"):
            self.acoustid = AcoustIDClient(
                acoustid_cfg["api_key"],
                min_score=acoustid_cfg.get("min_score", 0.5),
            )
            log.info("AcoustID fallback enabled (min_score=%.2f)", self.acoustid.min_score)
        else:
            self.acoustid = None

        spotify_cfg = config.get("spotify", {})
        self.spotify_poll_interval = spotify_cfg.get("poll_interval", 5)
        if (
            spotify_cfg.get("enabled")
            and spotify_cfg.get("client_id")
            and spotify_cfg.get("client_secret")
        ):
            token_file = pathlib.Path(spotify_cfg.get("token_file", "cache/spotify-token.txt"))
            if not token_file.is_absolute():
                token_file = PROJECT_ROOT / token_file
            self.spotify: SpotifyClient | None = SpotifyClient(
                client_id=spotify_cfg["client_id"],
                client_secret=spotify_cfg["client_secret"],
                token_file=token_file,
            )
            log.info("Spotify poller enabled (poll_interval=%ds)", self.spotify_poll_interval)
        else:
            self.spotify = None

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

        display_cfg = config.get("display", {})
        self.orientation = display_cfg.get("orientation", "landscape")
        composed_dir = pathlib.Path(display_cfg.get("composed_dir", "cache/composed"))
        if not composed_dir.is_absolute():
            composed_dir = PROJECT_ROOT / composed_dir
        background = display_cfg.get("background", "black")
        font = display_cfg.get("font", "sans")
        genre_font = display_cfg.get("genre_font", False)
        if genre_font and self.discogs is None:
            raise ValueError(
                "display.genre_font requires Discogs to be enabled "
                "(set discogs.enabled: true with valid credentials)"
            )
        layout = display_cfg.get("layout", "standard")
        self.composer = Composer(
            orientation=self.orientation,
            output_dir=composed_dir,
            background=background,
            font=font,
            genre_font=genre_font,
            layout=layout,
        )
        log.info(
            "Composer: %s (%s, layout=%s, background=%s, font=%s, genre_font=%s)",
            composed_dir,
            self.orientation,
            layout,
            background,
            font,
            genre_font,
        )

        tv_cfg = config.get("tv", {})
        if tv_cfg.get("enabled"):
            token_file = pathlib.Path(tv_cfg.get("token_file", "cache/tv-token.txt"))
            if not token_file.is_absolute():
                token_file = PROJECT_ROOT / token_file
            self.frame_tv: FrameTV | None = FrameTV(
                host=tv_cfg["host"],
                port=tv_cfg.get("port", 8002),
                token_file=token_file,
                matte=tv_cfg.get("matte", "none"),
                portrait_matte=tv_cfg.get("portrait_matte", "none"),
            )
            log.info("Frame TV: %s:%s (token=%s)", tv_cfg["host"], tv_cfg.get("port", 8002), token_file)
        else:
            self.frame_tv = None
            log.info("Frame TV disabled")

        self.current_track: TrackInfo | None = None
        self.state: DisplayState = DisplayState.LISTENING
        self._current_composed_path: pathlib.Path | None = None
        self._current_audio_end: float = 0.0
        self._current_audio_start: float = 0.0
        # Serialize shazamio calls — concurrent recognize() calls can race / corrupt state
        self._recognize_lock = asyncio.Lock()

    async def start(self):
        preview_cfg = self.config.get("preview", {})
        runner: aiohttp.web.AppRunner | None = None

        if preview_cfg.get("enabled", True):
            app = aiohttp.web.Application()
            app.router.add_get("/", self._preview_index)
            app.router.add_get("/current.jpg", self._preview_current)

            runner = aiohttp.web.AppRunner(app)
            await runner.setup()

            site = aiohttp.web.TCPSite(
                runner,
                preview_cfg.get("host", "0.0.0.0"),
                preview_cfg.get("port", 8080),
            )
            await site.start()
            log.info(
                "Preview server on http://%s:%s",
                preview_cfg.get("host", "0.0.0.0"),
                preview_cfg.get("port", 8080),
            )
        else:
            log.info("Preview server disabled")

        try:
            await self._listen_loop()
        finally:
            log.info("Shutting down...")
            if runner is not None:
                try:
                    await asyncio.wait_for(runner.cleanup(), timeout=5.0)
                except asyncio.TimeoutError:
                    log.warning("runner.cleanup() timed out, forcing exit")
            await self.shutdown()

    async def shutdown(self):
        """Release external resources held by the app."""
        if self.discogs is not None:
            await self.discogs.close()
            log.info("Closed Discogs client")
        if self.frame_tv is not None:
            await self.frame_tv.close()
            log.info("Closed Frame TV client")
        if self.spotify is not None:
            await self.spotify.close()
            log.info("Closed Spotify client")

    async def _preview_index(self, _request):
        return aiohttp.web.Response(text=_PREVIEW_HTML, content_type="text/html")

    async def _preview_current(self, _request):
        if self._current_composed_path and self._current_composed_path.exists():
            return aiohttp.web.FileResponse(self._current_composed_path)
        return aiohttp.web.Response(status=404, text="no current image yet")

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

            if (
                track is None
                and self.acoustid is not None
                and label.startswith("full-")
            ):
                log.info("[%s] Shazam miss, trying AcoustID...", label)
                acoustid_start = time.monotonic()
                track = await self.acoustid.identify(audio_bytes)
                log.info(
                    "[%s] AcoustID done (%.1fs)",
                    label,
                    time.monotonic() - acoustid_start,
                )

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

            # Dedup + stale checks must run inside the recognizer lock so that
            # two concurrent snapshots (cumulative + windowed) of the same
            # audio window don't both pass and double-fire the display path.
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

            # Claim the track NOW, before releasing the lock, so the next
            # concurrent recognition sees us in the dedup check above and bails.
            # We re-assign self.current_track to the enriched copy at the end.
            self.current_track = track
            self._current_audio_end = audio_end_time
            self._current_audio_start = audio_start_time

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
        await self._display_track(track, label)

    async def _handle_spotify_track(self, track: TrackInfo) -> None:
        """Commit a track reported by Spotify. Reuses the enrich + display pipeline.

        Spotify is treated as always-fresh: we bump the audio-end timestamp to
        now so that any in-flight Shazam result with older audio is correctly
        rejected as stale by _handle_recognition.
        """
        label = "spotify"
        now = time.monotonic()

        async with self._recognize_lock:
            if (
                self.current_track
                and track.display_key == self.current_track.display_key
            ):
                # Already showing this track — bump freshness so late Shazam
                # results from the previous track's audio get rejected as stale.
                self._current_audio_end = now
                self._current_audio_start = now
                return

            log.info("[%s] Now playing via Spotify: %s - %s", label, track.artist, track.title)
            self.current_track = track
            self._current_audio_end = now
            self._current_audio_start = now

        cached = self.cache.get(track.display_key) if self.cache is not None else None
        if cached is not None:
            log.info("[%s] Track cache hit for %s", label, track.display_key)
            track = cached
        else:
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

        if self.image_cache is not None and track.album:
            source = track.cover_url or track.cover_url_hires
            if source and not source.startswith(ImageCache.URL_PREFIX):
                await self.image_cache.ensure(track.artist, track.album, source)

        self.current_track = track
        self._current_audio_end = now
        self._current_audio_start = now
        self.state = DisplayState.IDENTIFIED
        await self._display_track(track, label)

    async def _display_track(self, track: TrackInfo, label: str) -> None:
        """Compose the TV image for the track and push it to the Frame."""
        if self.image_cache is None or not track.album:
            log.warning("[%s] No album / image cache; skipping display", label)
            return

        key = ImageCache.album_key(track.artist, track.album)
        cover_path = self.image_cache.file_path(key)
        if not cover_path.exists():
            log.warning("[%s] Cover not in image cache (%s); skipping display", label, cover_path)
            return

        try:
            composed_path = await asyncio.to_thread(self.composer.compose, track, cover_path)
        except Exception:
            log.exception("[%s] Composer failed", label)
            return

        self._current_composed_path = composed_path
        log.info("[%s] Composed image: %s", label, composed_path)

        if self.frame_tv is not None:
            await self.frame_tv.upload_and_display(composed_path)

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

            if self.spotify is not None:
                try:
                    spotify_track = await self.spotify.get_currently_playing()
                except Exception:
                    log.exception("Spotify poll crashed")
                    spotify_track = None
                if spotify_track is not None:
                    try:
                        await self._handle_spotify_track(spotify_track)
                    except Exception:
                        log.exception("Spotify track handling crashed")
                    await asyncio.sleep(self.spotify_poll_interval)
                    continue

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
