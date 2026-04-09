from unittest.mock import patch

import acoustid
import pytest

from backend.acoustid_client import CAA_URL_TEMPLATE, AcoustIDClient


@pytest.fixture
def client():
    return AcoustIDClient(api_key="test_key", min_score=0.5)


LOOKUP_RESPONSE_FULL = {
    "status": "ok",
    "results": [
        {
            "id": "result-uuid",
            "score": 0.95,
            "recordings": [
                {
                    "id": "recording-mbid",
                    "title": "Blue Train",
                    "artists": [
                        {"id": "a1", "name": "John Coltrane", "joinphrase": ""},
                    ],
                    "releases": [
                        {
                            "id": "release-mbid-123",
                            "title": "Blue Train",
                            "date": {"year": 1957, "month": 9, "day": 15},
                        }
                    ],
                }
            ],
        }
    ],
}

LOOKUP_RESPONSE_LOW_SCORE = {
    "status": "ok",
    "results": [
        {
            "id": "result-uuid",
            "score": 0.30,
            "recordings": [
                {"id": "r1", "title": "Maybe", "artists": [{"name": "Someone"}]}
            ],
        }
    ],
}

LOOKUP_RESPONSE_EMPTY = {"status": "ok", "results": []}

LOOKUP_RESPONSE_NO_RECORDINGS = {
    "status": "ok",
    "results": [{"id": "result-uuid", "score": 0.9, "recordings": []}],
}

LOOKUP_RESPONSE_FEAT = {
    "status": "ok",
    "results": [
        {
            "id": "result-uuid",
            "score": 0.9,
            "recordings": [
                {
                    "id": "r1",
                    "title": "Song",
                    "artists": [
                        {"name": "Artist A", "joinphrase": " feat. "},
                        {"name": "Artist B", "joinphrase": ""},
                    ],
                    "releases": [
                        {
                            "id": "rel-1",
                            "title": "Album",
                            "date": {"year": 2020},
                        }
                    ],
                }
            ],
        }
    ],
}


class TestParseResponse:
    @pytest.mark.asyncio
    async def test_successful_identification(self, client):
        with patch(
            "backend.acoustid_client.acoustid.fingerprint_file",
            return_value=(10.0, "fp"),
        ), patch(
            "backend.acoustid_client.acoustid.lookup",
            return_value=LOOKUP_RESPONSE_FULL,
        ):
            track = await client.identify(b"fake_wav_bytes")

        assert track is not None
        assert track.title == "Blue Train"
        assert track.artist == "John Coltrane"
        assert track.album == "Blue Train"
        assert track.year == "1957"
        assert track.cover_url == CAA_URL_TEMPLATE.format(mbid="release-mbid-123")

    @pytest.mark.asyncio
    async def test_below_threshold_returns_none(self, client):
        with patch(
            "backend.acoustid_client.acoustid.fingerprint_file",
            return_value=(10.0, "fp"),
        ), patch(
            "backend.acoustid_client.acoustid.lookup",
            return_value=LOOKUP_RESPONSE_LOW_SCORE,
        ):
            track = await client.identify(b"fake_wav_bytes")
        assert track is None

    @pytest.mark.asyncio
    async def test_empty_results_returns_none(self, client):
        with patch(
            "backend.acoustid_client.acoustid.fingerprint_file",
            return_value=(10.0, "fp"),
        ), patch(
            "backend.acoustid_client.acoustid.lookup",
            return_value=LOOKUP_RESPONSE_EMPTY,
        ):
            track = await client.identify(b"fake_wav_bytes")
        assert track is None

    @pytest.mark.asyncio
    async def test_no_recordings_returns_none(self, client):
        with patch(
            "backend.acoustid_client.acoustid.fingerprint_file",
            return_value=(10.0, "fp"),
        ), patch(
            "backend.acoustid_client.acoustid.lookup",
            return_value=LOOKUP_RESPONSE_NO_RECORDINGS,
        ):
            track = await client.identify(b"fake_wav_bytes")
        assert track is None

    @pytest.mark.asyncio
    async def test_joins_multiple_artists(self, client):
        with patch(
            "backend.acoustid_client.acoustid.fingerprint_file",
            return_value=(10.0, "fp"),
        ), patch(
            "backend.acoustid_client.acoustid.lookup",
            return_value=LOOKUP_RESPONSE_FEAT,
        ):
            track = await client.identify(b"fake_wav_bytes")
        assert track is not None
        assert track.artist == "Artist A feat. Artist B"

    @pytest.mark.asyncio
    async def test_no_release_metadata(self, client):
        response = {
            "status": "ok",
            "results": [
                {
                    "id": "r-uuid",
                    "score": 0.9,
                    "recordings": [
                        {
                            "id": "rec-1",
                            "title": "Lonely Track",
                            "artists": [{"name": "Nobody"}],
                            "releases": [],
                        }
                    ],
                }
            ],
        }
        with patch(
            "backend.acoustid_client.acoustid.fingerprint_file",
            return_value=(10.0, "fp"),
        ), patch(
            "backend.acoustid_client.acoustid.lookup",
            return_value=response,
        ):
            track = await client.identify(b"fake_wav_bytes")
        assert track is not None
        assert track.title == "Lonely Track"
        assert track.album is None
        assert track.year is None
        assert track.cover_url is None

    @pytest.mark.asyncio
    async def test_non_ok_status_returns_none(self, client):
        with patch(
            "backend.acoustid_client.acoustid.fingerprint_file",
            return_value=(10.0, "fp"),
        ), patch(
            "backend.acoustid_client.acoustid.lookup",
            return_value={"status": "error", "error": {"message": "bad key"}},
        ):
            track = await client.identify(b"fake_wav_bytes")
        assert track is None


class TestErrors:
    @pytest.mark.asyncio
    async def test_no_backend_returns_none(self, client):
        with patch(
            "backend.acoustid_client.acoustid.fingerprint_file",
            side_effect=acoustid.NoBackendError("fpcalc missing"),
        ):
            track = await client.identify(b"fake_wav_bytes")
        assert track is None

    @pytest.mark.asyncio
    async def test_fingerprint_error_returns_none(self, client):
        with patch(
            "backend.acoustid_client.acoustid.fingerprint_file",
            side_effect=acoustid.FingerprintGenerationError("bad audio"),
        ):
            track = await client.identify(b"fake_wav_bytes")
        assert track is None

    @pytest.mark.asyncio
    async def test_web_service_error_returns_none(self, client):
        with patch(
            "backend.acoustid_client.acoustid.fingerprint_file",
            return_value=(10.0, "fp"),
        ), patch(
            "backend.acoustid_client.acoustid.lookup",
            side_effect=acoustid.WebServiceError("network down"),
        ):
            track = await client.identify(b"fake_wav_bytes")
        assert track is None


class TestPickRelease:
    def test_prefers_release_with_year(self):
        releases = [
            {"id": "a", "title": "Album A"},  # no year
            {"id": "b", "title": "Album B", "date": {"year": 1973}},
        ]
        title, year, mbid = AcoustIDClient._pick_release(releases)
        assert title == "Album B"
        assert year == "1973"
        assert mbid == "b"

    def test_empty_returns_none_tuple(self):
        assert AcoustIDClient._pick_release([]) == (None, None, None)
