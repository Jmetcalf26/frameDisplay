from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses

from backend.recognizer import Recognizer, _apple_music_candidates


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
        # identify() returns the raw Shazam URL — caller resolves it
        assert track.cover_url == SHAZAM_RESPONSE_FULL["track"]["images"]["coverarthq"]
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


class TestExtractCoverUrl:
    def test_prefers_hq(self):
        data = {"images": {
            "coverart": "https://mzstatic.com/200x200bb.jpg",
            "coverarthq": "https://mzstatic.com/400x400bb.jpg",
        }}
        result = Recognizer._extract_cover_url(data)
        assert result == "https://mzstatic.com/400x400bb.jpg"

    def test_falls_back_to_coverart(self):
        data = {"images": {"coverart": "https://mzstatic.com/200x200bb.jpg"}}
        result = Recognizer._extract_cover_url(data)
        assert result == "https://mzstatic.com/200x200bb.jpg"

    def test_no_images_key(self):
        assert Recognizer._extract_cover_url({}) is None

    def test_empty_images(self):
        assert Recognizer._extract_cover_url({"images": {}}) is None


class TestAppleMusicCandidates:
    def test_generates_three_candidates(self):
        url = "https://mzstatic.com/400x400bb.jpg"
        candidates = _apple_music_candidates(url)
        assert len(candidates) == 3
        assert "3000x3000cc" in candidates[0]
        assert "3000x3000bb" in candidates[1]
        assert "3000x3000sr" in candidates[2]

    def test_custom_size(self):
        url = "https://mzstatic.com/400x400bb.jpg"
        candidates = _apple_music_candidates(url, size=1200)
        assert "1200x1200cc" in candidates[0]

    def test_no_match_returns_empty(self):
        url = "https://example.com/cover.jpg"
        candidates = _apple_music_candidates(url)
        assert candidates == []


class TestResolveCover:
    @pytest.mark.asyncio
    async def test_returns_first_successful_suffix(self):
        url = "https://mzstatic.com/400x400bb.jpg"
        with aioresponses() as mocked:
            mocked.head("https://mzstatic.com/3000x3000cc.jpg", status=403)
            mocked.head("https://mzstatic.com/3000x3000bb.jpg", status=200)
            result = await Recognizer.resolve_cover(url)
        assert "3000x3000bb" in result

    @pytest.mark.asyncio
    async def test_prefers_cc_over_bb(self):
        url = "https://mzstatic.com/400x400bb.jpg"
        with aioresponses() as mocked:
            mocked.head("https://mzstatic.com/3000x3000cc.jpg", status=200)
            result = await Recognizer.resolve_cover(url)
        assert "3000x3000cc" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_original_when_all_fail(self):
        url = "https://mzstatic.com/400x400bb.jpg"
        with aioresponses() as mocked:
            mocked.head("https://mzstatic.com/3000x3000cc.jpg", status=403)
            mocked.head("https://mzstatic.com/3000x3000bb.jpg", status=403)
            mocked.head("https://mzstatic.com/3000x3000sr.jpg", status=403)
            result = await Recognizer.resolve_cover(url)
        assert result == url

    @pytest.mark.asyncio
    async def test_non_apple_url_returned_as_is(self):
        url = "https://example.com/cover.jpg"
        result = await Recognizer.resolve_cover(url)
        assert result == url
