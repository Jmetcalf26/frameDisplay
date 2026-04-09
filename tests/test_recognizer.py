from unittest.mock import AsyncMock, patch

import pytest

from backend.recognizer import Recognizer


@pytest.fixture
def recognizer():
    with patch("backend.recognizer.Shazam") as MockShazam:
        rec = Recognizer()
        rec.shazam = MockShazam()
        rec.shazam.recognize = AsyncMock()
        yield rec


SHAZAM_RESPONSE_FULL = {
    "track": {
        "title": "Bohemian Rhapsody",
        "subtitle": "Queen",
        "images": {
            "coverart": "http://example.com/cover.jpg",
            "coverarthq": "http://example.com/cover_hq.jpg",
        },
        "sections": [
            {
                "metadata": [
                    {"title": "Album", "text": "A Night at the Opera"},
                    {"title": "Label", "text": "EMI"},
                ]
            }
        ],
    }
}

SHAZAM_RESPONSE_NO_HQ = {
    "track": {
        "title": "Test Song",
        "subtitle": "Test Artist",
        "images": {
            "coverart": "http://example.com/cover.jpg",
        },
    }
}

SHAZAM_RESPONSE_NO_IMAGES = {
    "track": {
        "title": "Test Song",
        "subtitle": "Test Artist",
    }
}

SHAZAM_RESPONSE_NO_MATCH = {}

SHAZAM_RESPONSE_EMPTY_TRACK = {"track": None}


class TestRecognizerIdentify:
    @pytest.mark.asyncio
    async def test_successful_identification(self, recognizer):
        recognizer.shazam.recognize.return_value = SHAZAM_RESPONSE_FULL

        track = await recognizer.identify(b"fake_audio")

        assert track is not None
        assert track.title == "Bohemian Rhapsody"
        assert track.artist == "Queen"
        assert track.cover_url == "http://example.com/cover_hq.jpg"
        assert track.album == "A Night at the Opera"

    @pytest.mark.asyncio
    async def test_no_album_metadata(self, recognizer):
        recognizer.shazam.recognize.return_value = SHAZAM_RESPONSE_NO_IMAGES

        track = await recognizer.identify(b"fake_audio")

        assert track.album is None

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, recognizer):
        recognizer.shazam.recognize.return_value = SHAZAM_RESPONSE_NO_MATCH

        track = await recognizer.identify(b"fake_audio")

        assert track is None

    @pytest.mark.asyncio
    async def test_empty_track_returns_none(self, recognizer):
        recognizer.shazam.recognize.return_value = SHAZAM_RESPONSE_EMPTY_TRACK

        track = await recognizer.identify(b"fake_audio")

        assert track is None

    @pytest.mark.asyncio
    async def test_falls_back_to_coverart_when_no_hq(self, recognizer):
        recognizer.shazam.recognize.return_value = SHAZAM_RESPONSE_NO_HQ

        track = await recognizer.identify(b"fake_audio")

        assert track.cover_url == "http://example.com/cover.jpg"

    @pytest.mark.asyncio
    async def test_no_images_returns_none_cover(self, recognizer):
        recognizer.shazam.recognize.return_value = SHAZAM_RESPONSE_NO_IMAGES

        track = await recognizer.identify(b"fake_audio")

        assert track is not None
        assert track.cover_url is None

    @pytest.mark.asyncio
    async def test_missing_title_defaults_to_unknown(self, recognizer):
        recognizer.shazam.recognize.return_value = {
            "track": {"images": {}}
        }

        track = await recognizer.identify(b"fake_audio")

        assert track.title == "Unknown"
        assert track.artist == "Unknown"


class TestExtractCover:
    def test_prefers_hq(self):
        data = {"images": {"coverart": "lo", "coverarthq": "hq"}}
        assert Recognizer._extract_cover(data) == "hq"

    def test_falls_back_to_coverart(self):
        data = {"images": {"coverart": "lo"}}
        assert Recognizer._extract_cover(data) == "lo"

    def test_no_images_key(self):
        assert Recognizer._extract_cover({}) is None

    def test_empty_images(self):
        assert Recognizer._extract_cover({"images": {}}) is None
