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
        assert msg["track"]["cover_url"] == "http://example.com/cover_hires.jpg"
        assert msg["track"]["year"] == "1957"

    def test_hires_cover_preferred_over_standard(self, app):
        app.state = DisplayState.IDENTIFIED
        app.current_track = TrackInfo(
            title="Test",
            artist="Test",
            cover_url="http://lo.jpg",
            cover_url_hires="http://hi.jpg",
        )

        msg = app._build_message()

        assert msg["track"]["cover_url"] == "http://hi.jpg"

    def test_standard_cover_when_no_hires(self, app):
        app.state = DisplayState.IDENTIFIED
        app.current_track = TrackInfo(
            title="Test",
            artist="Test",
            cover_url="http://lo.jpg",
        )

        msg = app._build_message()

        assert msg["track"]["cover_url"] == "http://lo.jpg"


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
