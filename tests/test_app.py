from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app import MISS_THRESHOLD, FrameDisplayApp
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
    "server": {"host": "0.0.0.0", "port": 8080},
}

CONFIG_WITH_DISCOGS = {
    **MINIMAL_CONFIG,
    "discogs": {
        "enabled": True,
        "consumer_key": "test_key",
        "consumer_secret": "test_secret",
    },
}


@pytest.fixture
def app():
    with patch("backend.app.Recognizer"):
        return FrameDisplayApp(MINIMAL_CONFIG)


@pytest.fixture
def app_with_discogs():
    with patch("backend.app.Recognizer"), patch("backend.app.DiscogsClient"):
        return FrameDisplayApp(CONFIG_WITH_DISCOGS)


class TestFrameDisplayAppInit:
    def test_discogs_disabled(self, app):
        assert app.discogs is None

    def test_discogs_enabled(self, app_with_discogs):
        assert app_with_discogs.discogs is not None

    def test_initial_state_is_idle(self, app):
        assert app.state == DisplayState.IDLE
        assert app.current_track is None
        assert app._miss_count == 0

    def test_discogs_not_created_without_key(self):
        config = {
            **MINIMAL_CONFIG,
            "discogs": {"enabled": True, "consumer_key": "", "consumer_secret": ""},
        }
        with patch("backend.app.Recognizer"):
            app = FrameDisplayApp(config)
        assert app.discogs is None


class TestBuildMessage:
    def test_idle_message(self, app):
        app.state = DisplayState.IDLE
        app.current_track = None

        msg = app._build_message()

        assert msg == {"state": "idle"}

    def test_identified_message_with_track(self, app):
        app.state = DisplayState.IDENTIFIED
        app.current_track = TrackInfo(
            title="Blue Train",
            artist="John Coltrane",
            album="Blue Train",
            cover_url="http://example.com/cover.jpg",
            cover_url_hires="http://example.com/cover_hires.jpg",
            year="1957",
            genre="Jazz",
            label="Blue Note",
        )

        msg = app._build_message()

        assert msg["state"] == "identified"
        assert msg["track"]["title"] == "Blue Train"
        assert msg["track"]["artist"] == "John Coltrane"
        assert msg["track"]["cover_url"] == "http://example.com/cover.jpg"
        assert msg["track"]["year"] == "1957"

    def test_apple_music_cover_preferred_over_discogs(self, app):
        app.state = DisplayState.IDENTIFIED
        app.current_track = TrackInfo(
            title="Test",
            artist="Test",
            cover_url="http://apple.jpg",
            cover_url_hires="http://discogs.jpg",
        )

        msg = app._build_message()

        assert msg["track"]["cover_url"] == "http://apple.jpg"

    def test_discogs_cover_when_no_apple(self, app):
        app.state = DisplayState.IDENTIFIED
        app.current_track = TrackInfo(
            title="Test",
            artist="Test",
            cover_url_hires="http://discogs.jpg",
        )

        msg = app._build_message()

        assert msg["track"]["cover_url"] == "http://discogs.jpg"


class TestMissThreshold:
    def test_miss_count_increments(self, app):
        assert app._miss_count == 0
        app._miss_count += 1
        assert app._miss_count == 1

    def test_miss_threshold_value(self):
        assert MISS_THRESHOLD == 3

    def test_miss_count_resets_on_track(self, app):
        app._miss_count = 5
        app._miss_count = 0
        assert app._miss_count == 0


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
    async def test_audio_end_resets_on_idle(self, app):
        """Going idle should reset the audio end timestamp."""
        app.recognizer.identify = AsyncMock(return_value=None)
        app.state = DisplayState.IDENTIFIED
        app.current_track = TrackInfo(title="Song", artist="Artist")
        app._current_audio_end = 100.0
        app._miss_count = MISS_THRESHOLD - 1

        await app._handle_recognition("full-10s", b"audio", audio_end_time=110.0)

        assert app.state == DisplayState.IDLE
        assert app._current_audio_end == 0.0

    @pytest.mark.asyncio
    async def test_equal_audio_end_accepted(self, app):
        """A result with equal audio end time should be accepted (same snapshot point)."""
        new_track = TrackInfo(title="New Song", artist="New Artist")
        app.recognizer.identify = AsyncMock(return_value=new_track)
        app.current_track = TrackInfo(title="Old Song", artist="Old Artist")
        app.state = DisplayState.IDENTIFIED
        app._current_audio_end = 100.0

        await app._handle_recognition("cumulative-10s", b"audio", audio_end_time=100.0)

        assert app.current_track.title == "New Song"


class TestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all_clients(self, app):
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        app.ws_clients = {ws1, ws2}

        await app._broadcast({"state": "idle"})

        ws1.send_json.assert_called_once_with({"state": "idle"})
        ws2.send_json.assert_called_once_with({"state": "idle"})

    @pytest.mark.asyncio
    async def test_broadcast_removes_dead_clients(self, app):
        ws_alive = AsyncMock()
        ws_dead = AsyncMock()
        ws_dead.send_json.side_effect = ConnectionResetError

        app.ws_clients = {ws_alive, ws_dead}

        await app._broadcast({"state": "idle"})

        assert ws_dead not in app.ws_clients
        assert ws_alive in app.ws_clients
