import re

import aiohttp
import pytest
from aioresponses import aioresponses

from backend.discogs import DiscogsClient
from backend.models import TrackInfo

DISCOGS_SEARCH_PATTERN = re.compile(r"https://api\.discogs\.com/database/search\?.*")

DISCOGS_RESPONSE_FULL = {
    "results": [
        {
            "year": "1975",
            "genre": ["Rock"],
            "label": ["EMI"],
            "cover_image": "http://example.com/discogs_cover.jpg",
        }
    ]
}

DISCOGS_RESPONSE_MULTI_GENRE_LABEL = {
    "results": [
        {
            "year": "1959",
            "genre": ["Jazz", "Hard Bop"],
            "label": ["Blue Note", "Liberty"],
            "cover_image": "http://example.com/cover.jpg",
        }
    ]
}

DISCOGS_RESPONSE_NO_RESULTS = {"results": []}

DISCOGS_RESPONSE_PARTIAL = {
    "results": [
        {
            "year": "1980",
            "genre": [],
            "label": [],
        }
    ]
}


@pytest.fixture
def client():
    return DiscogsClient("test_key", "test_secret")


@pytest.fixture
def track():
    return TrackInfo(
        title="Bohemian Rhapsody",
        artist="Queen",
        cover_url="http://example.com/shazam_cover.jpg",
    )


class TestDiscogsEnrich:
    @pytest.mark.asyncio
    async def test_successful_enrichment(self, client, track):
        with aioresponses() as mocked:
            mocked.get(DISCOGS_SEARCH_PATTERN, payload=DISCOGS_RESPONSE_FULL)

            result = await client.enrich(track)

        assert result.year == "1975"
        assert result.genre == "Rock"
        assert result.label == "EMI"
        assert result.cover_url_hires == "http://example.com/discogs_cover.jpg"
        assert result.cover_url == "http://example.com/shazam_cover.jpg"
        await client.close()

    @pytest.mark.asyncio
    async def test_multiple_genres_and_labels_joined(self, client, track):
        with aioresponses() as mocked:
            mocked.get(DISCOGS_SEARCH_PATTERN, payload=DISCOGS_RESPONSE_MULTI_GENRE_LABEL)

            result = await client.enrich(track)

        assert result.genre == "Jazz, Hard Bop"
        assert result.label == "Blue Note, Liberty"
        await client.close()

    @pytest.mark.asyncio
    async def test_no_results_returns_track_unchanged(self, client, track):
        with aioresponses() as mocked:
            mocked.get(DISCOGS_SEARCH_PATTERN, payload=DISCOGS_RESPONSE_NO_RESULTS)

            result = await client.enrich(track)

        assert result.year is None
        assert result.genre is None
        assert result.cover_url_hires is None
        await client.close()

    @pytest.mark.asyncio
    async def test_api_error_returns_track_unchanged(self, client, track):
        with aioresponses() as mocked:
            mocked.get(DISCOGS_SEARCH_PATTERN, status=500)

            result = await client.enrich(track)

        assert result.year is None
        assert result.genre is None
        await client.close()

    @pytest.mark.asyncio
    async def test_network_error_returns_track_unchanged(self, client, track):
        with aioresponses() as mocked:
            mocked.get(DISCOGS_SEARCH_PATTERN, exception=aiohttp.ClientError("timeout"))

            result = await client.enrich(track)

        assert result.year is None
        await client.close()

    @pytest.mark.asyncio
    async def test_uses_album_in_query_when_available(self, client):
        track = TrackInfo(
            title="Bohemian Rhapsody",
            artist="Queen",
            album="A Night at the Opera",
        )
        with aioresponses() as mocked:
            mocked.get(DISCOGS_SEARCH_PATTERN, payload=DISCOGS_RESPONSE_FULL)

            await client.enrich(track)

            call = list(mocked.requests.values())[0][0]
            q = call.kwargs["params"]["q"]
            assert "A Night at the Opera" in q
            assert "Bohemian Rhapsody" not in q
        await client.close()

    @pytest.mark.asyncio
    async def test_falls_back_to_title_without_album(self, client):
        track = TrackInfo(title="Bohemian Rhapsody", artist="Queen")
        with aioresponses() as mocked:
            mocked.get(DISCOGS_SEARCH_PATTERN, payload=DISCOGS_RESPONSE_FULL)

            await client.enrich(track)

            call = list(mocked.requests.values())[0][0]
            q = call.kwargs["params"]["q"]
            assert "Bohemian Rhapsody" in q
        await client.close()

    @pytest.mark.asyncio
    async def test_partial_result_no_cover(self, client, track):
        with aioresponses() as mocked:
            mocked.get(DISCOGS_SEARCH_PATTERN, payload=DISCOGS_RESPONSE_PARTIAL)

            result = await client.enrich(track)

        assert result.year == "1980"
        assert result.genre == ""
        assert result.label == ""
        assert result.cover_url_hires is None
        await client.close()
