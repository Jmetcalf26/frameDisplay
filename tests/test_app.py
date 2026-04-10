import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app import FrameDisplayApp
from backend.models import DisplayState, TrackInfo

MINIMAL_CONFIG = {
    "audio": {
        "device": None,
        "sample_rate": 44100,
        "channels": 1,
        "snippet_duration": 5,
        "loop_interval": 10,
    },
    "discogs": {"enabled": False},
    "display": {"orientation": "landscape", "composed_dir": "cache/composed"},
    "tv": {"enabled": False},
    "preview": {"enabled": False},
    "cache": {"enabled": False},
    "image_cache": {"enabled": False},
    "acoustid": {"enabled": False},
}

CONFIG_WITH_DISCOGS = {
    **MINIMAL_CONFIG,
    "discogs": {
        "enabled": True,
        "consumer_key": "test_key",
        "consumer_secret": "test_secret",
    },
}

CONFIG_WITH_ACOUSTID = {
    **MINIMAL_CONFIG,
    "acoustid": {
        "enabled": True,
        "api_key": "test_acoustid_key",
        "min_score": 0.5,
    },
}


@pytest.fixture
def app():
    with patch("backend.app.Recognizer"), patch("backend.app.Composer"):
        return FrameDisplayApp(MINIMAL_CONFIG)


@pytest.fixture
def app_with_discogs():
    with patch("backend.app.Recognizer"), patch("backend.app.DiscogsClient"), patch(
        "backend.app.Composer"
    ):
        return FrameDisplayApp(CONFIG_WITH_DISCOGS)


@pytest.fixture
def app_with_cache(tmp_path):
    from backend.cache import TrackCache
    cache = TrackCache(path=tmp_path / "cache.json", max_bytes=10_000)
    with patch("backend.app.Recognizer"), patch("backend.app.Composer"):
        return FrameDisplayApp(MINIMAL_CONFIG, cache=cache)


@pytest.fixture
def app_with_acoustid():
    with patch("backend.app.Recognizer"), patch("backend.app.AcoustIDClient"), patch(
        "backend.app.Composer"
    ):
        return FrameDisplayApp(CONFIG_WITH_ACOUSTID)


class TestFrameDisplayAppInit:
    def test_discogs_disabled(self, app):
        assert app.discogs is None

    def test_discogs_enabled(self, app_with_discogs):
        assert app_with_discogs.discogs is not None

    def test_initial_state(self, app):
        assert app.state == DisplayState.LISTENING
        assert app.current_track is None

    def test_discogs_not_created_without_key(self):
        config = {
            **MINIMAL_CONFIG,
            "discogs": {"enabled": True, "consumer_key": "", "consumer_secret": ""},
        }
        with patch("backend.app.Recognizer"), patch("backend.app.Composer"):
            app = FrameDisplayApp(config)
        assert app.discogs is None

    def test_orientation_read_from_config(self, app):
        assert app.orientation == "landscape"

    def test_frame_tv_disabled(self, app):
        assert app.frame_tv is None

    def test_frame_tv_enabled(self, tmp_path):
        config = {
            **MINIMAL_CONFIG,
            "tv": {
                "enabled": True,
                "host": "192.168.1.50",
                "port": 8002,
                "token_file": str(tmp_path / "token.txt"),
            },
        }
        with patch("backend.app.Recognizer"), patch("backend.app.Composer"), patch(
            "backend.app.FrameTV"
        ) as FakeFrameTV:
            app = FrameDisplayApp(config)
        assert app.frame_tv is not None
        FakeFrameTV.assert_called_once()


class TestNoMatchKeepsCurrent:
    @pytest.mark.asyncio
    async def test_no_match_does_not_clear_track(self, app):
        """A failed recognition should leave the current track in place."""
        app.recognizer.identify = AsyncMock(return_value=None)
        app.current_track = TrackInfo(title="Song", artist="Artist")
        app.state = DisplayState.IDENTIFIED

        await app._handle_recognition("full-10s", b"audio", audio_end_time=110.0)

        assert app.current_track is not None
        assert app.current_track.title == "Song"
        assert app.state == DisplayState.IDENTIFIED


class TestRecognizerLock:
    @pytest.mark.asyncio
    async def test_concurrent_recognitions_serialized(self, app):
        """Two simultaneous _handle_recognition calls must not call identify in parallel."""
        in_flight = 0
        max_in_flight = 0

        async def slow_identify(_audio):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return None

        app.recognizer.identify = slow_identify

        await asyncio.gather(
            app._handle_recognition("a", b"x", audio_end_time=1.0),
            app._handle_recognition("b", b"y", audio_end_time=2.0),
            app._handle_recognition("c", b"z", audio_end_time=3.0),
        )

        assert max_in_flight == 1


class TestDeduplication:
    def test_same_track_detected(self, app):
        track_a = TrackInfo(title="Hey Jude", artist="The Beatles")
        track_b = TrackInfo(title="Hey Jude", artist="The Beatles")

        app.current_track = track_a
        assert track_b.display_key == app.current_track.display_key

    def test_different_track_detected(self, app):
        track_a = TrackInfo(title="Hey Jude", artist="The Beatles")
        track_b = TrackInfo(title="Let It Be", artist="The Beatles")

        app.current_track = track_a
        assert track_b.display_key != app.current_track.display_key


class TestRecencyPriority:
    @pytest.mark.asyncio
    async def test_stale_result_rejected(self, app):
        """A recognition result whose audio ended before the current track's should be rejected."""
        app.recognizer.identify = AsyncMock(
            return_value=TrackInfo(title="Old Song", artist="Old Artist")
        )
        app.current_track = TrackInfo(title="Current Song", artist="Current Artist")
        app.state = DisplayState.IDENTIFIED
        app._current_audio_end = 100.0

        await app._handle_recognition("cumulative-5s", b"audio", audio_end_time=95.0)

        # Should NOT have updated the track
        assert app.current_track.title == "Current Song"

    @pytest.mark.asyncio
    async def test_newer_result_accepted(self, app):
        """A recognition result with newer audio should update the display."""
        new_track = TrackInfo(title="New Song", artist="New Artist")
        app.recognizer.identify = AsyncMock(return_value=new_track)
        app.current_track = TrackInfo(title="Old Song", artist="Old Artist")
        app.state = DisplayState.IDENTIFIED
        app._current_audio_end = 90.0

        await app._handle_recognition("windowed-5s-10s", b"audio", audio_end_time=100.0)

        assert app.current_track.title == "New Song"
        assert app._current_audio_end == 100.0

    @pytest.mark.asyncio
    async def test_cumulative_rejected_when_windowed_already_matched(self, app):
        """At the same snapshot point, cumulative (start=0) loses to windowed (start=5)."""
        app.recognizer.identify = AsyncMock(
            return_value=TrackInfo(title="Old Song", artist="Old Artist")
        )
        # Windowed-5s-10s already set the track with audio_start=105, audio_end=110
        app.current_track = TrackInfo(title="Current Song", artist="Current Artist")
        app.state = DisplayState.IDENTIFIED
        app._current_audio_end = 110.0
        app._current_audio_start = 105.0

        # Cumulative-10s has same end time but starts earlier (100)
        await app._handle_recognition(
            "cumulative-10s", b"audio", audio_end_time=110.0, audio_start_time=100.0,
        )

        assert app.current_track.title == "Current Song"

    @pytest.mark.asyncio
    async def test_windowed_wins_over_cumulative_at_same_endpoint(self, app):
        """At the same snapshot point, windowed (later start) beats cumulative (start=0)."""
        new_track = TrackInfo(title="New Song", artist="New Artist")
        app.recognizer.identify = AsyncMock(return_value=new_track)
        # Cumulative-10s set the track with audio_start=100, audio_end=110
        app.current_track = TrackInfo(title="Old Song", artist="Old Artist")
        app.state = DisplayState.IDENTIFIED
        app._current_audio_end = 110.0
        app._current_audio_start = 100.0

        # Windowed-5s-10s has same end time but starts later (105)
        await app._handle_recognition(
            "windowed-5s-10s", b"audio", audio_end_time=110.0, audio_start_time=105.0,
        )

        assert app.current_track.title == "New Song"
        assert app._current_audio_start == 105.0

    @pytest.mark.asyncio
    async def test_equal_start_and_end_accepted(self, app):
        """Two results with identical audio windows should still be accepted (e.g. first snapshot)."""
        new_track = TrackInfo(title="New Song", artist="New Artist")
        app.recognizer.identify = AsyncMock(return_value=new_track)
        app.current_track = TrackInfo(title="Old Song", artist="Old Artist")
        app.state = DisplayState.IDENTIFIED
        app._current_audio_end = 105.0
        app._current_audio_start = 100.0

        await app._handle_recognition(
            "windowed-0s-5s", b"audio", audio_end_time=105.0, audio_start_time=100.0,
        )

        assert app.current_track.title == "New Song"


class TestCacheIntegration:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_resolve_cover(self, app_with_cache):
        """A cached track should bypass resolve_cover entirely."""
        cached_track = TrackInfo(
            title="Song",
            artist="Artist",
            cover_url="http://cached.jpg",
            year="2020",
        )
        app_with_cache.cache.put("artist:song", cached_track)

        # Shazam returns a fresh track with a raw URL
        fresh_track = TrackInfo(
            title="Song", artist="Artist", cover_url="http://raw.jpg",
        )
        app_with_cache.recognizer.identify = AsyncMock(return_value=fresh_track)
        app_with_cache.recognizer.resolve_cover = AsyncMock()

        await app_with_cache._handle_recognition("test", b"audio", audio_end_time=10.0)

        # resolve_cover should NOT have been called
        app_with_cache.recognizer.resolve_cover.assert_not_called()
        assert app_with_cache.current_track.cover_url == "http://cached.jpg"
        assert app_with_cache.current_track.year == "2020"

    @pytest.mark.asyncio
    async def test_cache_miss_resolves_and_stores(self, app_with_cache):
        """A cache miss should call resolve_cover and store the result."""
        fresh_track = TrackInfo(
            title="NewSong", artist="NewArtist", cover_url="http://raw.jpg",
        )
        app_with_cache.recognizer.identify = AsyncMock(return_value=fresh_track)
        app_with_cache.recognizer.resolve_cover = AsyncMock(
            return_value="http://resolved.jpg",
        )

        await app_with_cache._handle_recognition("test", b"audio", audio_end_time=10.0)

        app_with_cache.recognizer.resolve_cover.assert_called_once_with("http://raw.jpg")
        # Track should now be cached
        assert "newartist:newsong" in app_with_cache.cache
        cached = app_with_cache.cache.get("newartist:newsong")
        assert cached.cover_url == "http://resolved.jpg"

    @pytest.mark.asyncio
    async def test_cache_disabled_works(self, app):
        """When cache is disabled, recognition should still work."""
        assert app.cache is None
        fresh_track = TrackInfo(
            title="Song", artist="Artist", cover_url="http://raw.jpg",
        )
        app.recognizer.identify = AsyncMock(return_value=fresh_track)
        app.recognizer.resolve_cover = AsyncMock(return_value="http://resolved.jpg")

        await app._handle_recognition("test", b"audio", audio_end_time=10.0)

        assert app.current_track is not None
        assert app.current_track.cover_url == "http://resolved.jpg"


class TestAcoustIDFallback:
    def test_acoustid_disabled_by_default(self, app):
        assert app.acoustid is None

    def test_acoustid_enabled(self, app_with_acoustid):
        assert app_with_acoustid.acoustid is not None

    def test_acoustid_not_created_without_key(self):
        config = {
            **MINIMAL_CONFIG,
            "acoustid": {"enabled": True, "api_key": ""},
        }
        with patch("backend.app.Recognizer"), patch("backend.app.Composer"):
            app = FrameDisplayApp(config)
        assert app.acoustid is None

    @pytest.mark.asyncio
    async def test_fallback_fires_when_shazam_misses_full_recording(
        self, app_with_acoustid,
    ):
        app_with_acoustid.recognizer.identify = AsyncMock(return_value=None)
        fallback_track = TrackInfo(title="Found by AcoustID", artist="Obscure")
        app_with_acoustid.acoustid.identify = AsyncMock(return_value=fallback_track)

        await app_with_acoustid._handle_recognition(
            "full-10s", b"audio", audio_end_time=10.0,
        )

        app_with_acoustid.acoustid.identify.assert_awaited_once_with(b"audio")
        assert app_with_acoustid.current_track is not None
        assert app_with_acoustid.current_track.title == "Found by AcoustID"

    @pytest.mark.asyncio
    async def test_fallback_skipped_for_windowed_label(self, app_with_acoustid):
        app_with_acoustid.recognizer.identify = AsyncMock(return_value=None)
        app_with_acoustid.acoustid.identify = AsyncMock()

        await app_with_acoustid._handle_recognition(
            "windowed-5s-10s", b"audio", audio_end_time=10.0,
        )

        app_with_acoustid.acoustid.identify.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_skipped_for_cumulative_label(self, app_with_acoustid):
        app_with_acoustid.recognizer.identify = AsyncMock(return_value=None)
        app_with_acoustid.acoustid.identify = AsyncMock()

        await app_with_acoustid._handle_recognition(
            "cumulative-10s", b"audio", audio_end_time=10.0,
        )

        app_with_acoustid.acoustid.identify.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_shazam_hits(self, app_with_acoustid):
        shazam_track = TrackInfo(title="Found by Shazam", artist="Famous")
        app_with_acoustid.recognizer.identify = AsyncMock(return_value=shazam_track)
        app_with_acoustid.recognizer.resolve_cover = AsyncMock(return_value=None)
        app_with_acoustid.acoustid.identify = AsyncMock()

        await app_with_acoustid._handle_recognition(
            "full-10s", b"audio", audio_end_time=10.0,
        )

        app_with_acoustid.acoustid.identify.assert_not_called()
        assert app_with_acoustid.current_track.title == "Found by Shazam"

    @pytest.mark.asyncio
    async def test_no_crash_when_acoustid_disabled_and_shazam_misses(self, app):
        assert app.acoustid is None
        app.recognizer.identify = AsyncMock(return_value=None)

        # Should simply return without raising
        await app._handle_recognition("full-10s", b"audio", audio_end_time=10.0)

        assert app.current_track is None


class TestDisplayTrack:
    @pytest.mark.asyncio
    async def test_display_track_skipped_without_image_cache(self, app):
        """With image_cache disabled, _display_track should log and return."""
        assert app.image_cache is None
        track = TrackInfo(title="T", artist="A", album="Album")
        # Should not raise even though composer is a MagicMock and frame_tv is None
        await app._display_track(track, "test")
        app.composer.compose.assert_not_called()

    @pytest.mark.asyncio
    async def test_display_track_skipped_when_cover_missing(self, tmp_path):
        """If the cover file isn't on disk, _display_track skips the compose."""
        from backend.image_cache import ImageCache
        img_cache = ImageCache(dir=tmp_path / "images", max_bytes=1_000_000)
        with patch("backend.app.Recognizer"), patch("backend.app.Composer"):
            app = FrameDisplayApp(MINIMAL_CONFIG, image_cache=img_cache)
        track = TrackInfo(title="T", artist="A", album="NotCached")
        await app._display_track(track, "test")
        app.composer.compose.assert_not_called()

    @pytest.mark.asyncio
    async def test_display_track_composes_and_uploads(self, tmp_path):
        """With cover present and TV enabled, compose + upload should both fire."""
        from backend.image_cache import ImageCache
        img_cache = ImageCache(dir=tmp_path / "images", max_bytes=1_000_000)
        # Drop a fake cover into the cache at the expected path
        key = ImageCache.album_key("A", "Album")
        img_cache._entries[key] = {"size": 4}
        img_cache.file_path(key).write_bytes(b"fake")

        config = {
            **MINIMAL_CONFIG,
            "tv": {
                "enabled": True,
                "host": "1.2.3.4",
                "port": 8002,
                "token_file": str(tmp_path / "token.txt"),
            },
        }
        with patch("backend.app.Recognizer"), patch("backend.app.Composer"), patch(
            "backend.app.FrameTV"
        ) as FakeFrameTV:
            fake_tv = FakeFrameTV.return_value
            fake_tv.upload_and_display = AsyncMock()
            app = FrameDisplayApp(config, image_cache=img_cache)
            composed = tmp_path / "composed.jpg"
            composed.write_bytes(b"x")
            app.composer.compose = MagicMock(return_value=composed)

            track = TrackInfo(title="T", artist="A", album="Album")
            await app._display_track(track, "test")

        app.composer.compose.assert_called_once()
        fake_tv.upload_and_display.assert_awaited_once_with(composed)
        assert app._current_composed_path == composed


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_closes_discogs(self, app_with_discogs):
        app_with_discogs.discogs.close = AsyncMock()
        await app_with_discogs.shutdown()
        app_with_discogs.discogs.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_no_discogs(self, app):
        # Should not raise when discogs is None
        assert app.discogs is None
        await app.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_closes_frame_tv(self, tmp_path):
        config = {
            **MINIMAL_CONFIG,
            "tv": {
                "enabled": True,
                "host": "1.2.3.4",
                "port": 8002,
                "token_file": str(tmp_path / "token.txt"),
            },
        }
        with patch("backend.app.Recognizer"), patch("backend.app.Composer"), patch(
            "backend.app.FrameTV"
        ) as FakeFrameTV:
            fake_tv = FakeFrameTV.return_value
            fake_tv.close = AsyncMock()
            app = FrameDisplayApp(config)
            await app.shutdown()
        fake_tv.close.assert_awaited_once()
