from unittest.mock import AsyncMock, patch

import pytest

from backend.recognizer import Recognizer, _upscale_apple_music_url


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
            "coverart": "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/test/200x200bb.jpg",
            "coverarthq": "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/test/400x400bb.jpg",
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
            "coverart": "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/test/200x200bb.jpg",
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
        assert "3000x3000bb" in track.cover_url
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

        assert "3000x3000bb" in track.cover_url

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
    def test_prefers_hq_and_upscales(self):
        data = {"images": {
            "coverart": "https://mzstatic.com/200x200bb.jpg",
            "coverarthq": "https://mzstatic.com/400x400bb.jpg",
        }}
        result = Recognizer._extract_cover(data)
        assert "3000x3000bb" in result

    def test_falls_back_to_coverart(self):
        data = {"images": {"coverart": "https://mzstatic.com/200x200bb.jpg"}}
        result = Recognizer._extract_cover(data)
        assert "3000x3000bb" in result

    def test_no_images_key(self):
        assert Recognizer._extract_cover({}) is None

    def test_empty_images(self):
        assert Recognizer._extract_cover({"images": {}}) is None


class TestUpscaleAppleMusicUrl:
    def test_rewrites_dimensions(self):
        url = "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/ab/cd/400x400bb.jpg"
        result = _upscale_apple_music_url(url)
        assert result == "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/ab/cd/3000x3000bb.jpg"

    def test_custom_size(self):
        url = "https://mzstatic.com/200x200bb.jpg"
        result = _upscale_apple_music_url(url, size=1200)
        assert "1200x1200bb" in result

    def test_no_match_returns_unchanged(self):
        url = "https://example.com/cover.jpg"
        result = _upscale_apple_music_url(url)
        assert result == url
