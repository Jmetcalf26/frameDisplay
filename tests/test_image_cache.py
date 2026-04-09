import json

import aiohttp
import pytest
from aioresponses import aioresponses

from backend.image_cache import ImageCache

IMG_BYTES = b"\xff\xd8\xff\xe0" + b"x" * 200  # 204 bytes of fake JPEG


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "images"


@pytest.fixture
def cache(cache_dir):
    return ImageCache(dir=cache_dir, max_bytes=10_000)


class TestAlbumKeying:
    def test_same_album_same_key(self):
        k1 = ImageCache.album_key("Pink Floyd", "Dark Side of the Moon")
        k2 = ImageCache.album_key("Pink Floyd", "Dark Side of the Moon")
        assert k1 == k2

    def test_case_insensitive(self):
        k1 = ImageCache.album_key("Pink Floyd", "Dark Side of the Moon")
        k2 = ImageCache.album_key("pink floyd", "DARK SIDE OF THE MOON")
        assert k1 == k2

    def test_different_albums_different_keys(self):
        k1 = ImageCache.album_key("Pink Floyd", "Dark Side of the Moon")
        k2 = ImageCache.album_key("Pink Floyd", "The Wall")
        assert k1 != k2

    def test_different_artists_different_keys(self):
        k1 = ImageCache.album_key("Pink Floyd", "The Wall")
        k2 = ImageCache.album_key("Roger Waters", "The Wall")
        assert k1 != k2


class TestEnsureMissAndHit:
    @pytest.mark.asyncio
    async def test_miss_downloads_and_stores(self, cache):
        url = "https://example.com/cover.jpg"
        with aioresponses() as mocked:
            mocked.get(url, body=IMG_BYTES)
            local = await cache.ensure("Artist", "Album", url)

        assert local is not None
        assert local.startswith("/cache/images/")
        assert local.endswith(".jpg")

        key = ImageCache.album_key("Artist", "Album")
        assert key in cache
        assert cache.file_path(key).read_bytes() == IMG_BYTES

    @pytest.mark.asyncio
    async def test_hit_does_not_redownload(self, cache):
        url = "https://example.com/cover.jpg"
        with aioresponses() as mocked:
            mocked.get(url, body=IMG_BYTES)
            await cache.ensure("Artist", "Album", url)

        # Second call: no mock registered, would 404 if it downloaded
        local = await cache.ensure("Artist", "Album", url)
        assert local is not None
        assert local.endswith(".jpg")

    @pytest.mark.asyncio
    async def test_two_songs_same_album_share_entry(self, cache):
        url = "https://example.com/cover.jpg"
        with aioresponses() as mocked:
            mocked.get(url, body=IMG_BYTES)
            url1 = await cache.ensure("Artist", "Album", url)
            # Second song from same album — no second download mocked
            url2 = await cache.ensure("Artist", "Album", "https://different.com/x.jpg")

        assert url1 == url2
        assert len(cache) == 1

    @pytest.mark.asyncio
    async def test_download_failure_returns_none(self, cache):
        url = "https://example.com/cover.jpg"
        with aioresponses() as mocked:
            mocked.get(url, exception=aiohttp.ClientError("network down"))
            local = await cache.ensure("Artist", "Album", url)

        assert local is None
        assert len(cache) == 0

    @pytest.mark.asyncio
    async def test_non_http_url_returns_none(self, cache):
        local = await cache.ensure("Artist", "Album", "/cache/images/foo.jpg")
        assert local is None

    @pytest.mark.asyncio
    async def test_empty_url_returns_none(self, cache):
        assert await cache.ensure("Artist", "Album", "") is None
        assert await cache.ensure("Artist", "Album", None) is None


class TestLocalUrlIfPresent:
    @pytest.mark.asyncio
    async def test_returns_url_when_cached(self, cache):
        url = "https://example.com/cover.jpg"
        with aioresponses() as mocked:
            mocked.get(url, body=IMG_BYTES)
            await cache.ensure("Artist", "Album", url)

        local = cache.local_url_if_present("Artist", "Album")
        assert local is not None
        assert local.startswith("/cache/images/")

    def test_returns_none_when_not_cached(self, cache):
        assert cache.local_url_if_present("Artist", "Album") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_file_missing(self, cache):
        url = "https://example.com/cover.jpg"
        with aioresponses() as mocked:
            mocked.get(url, body=IMG_BYTES)
            await cache.ensure("Artist", "Album", url)

        # Externally delete the file
        key = ImageCache.album_key("Artist", "Album")
        cache.file_path(key).unlink()

        assert cache.local_url_if_present("Artist", "Album") is None


class TestSizeEviction:
    @pytest.mark.asyncio
    async def test_oldest_evicted_when_exceeded(self, cache_dir):
        # Each image is 204 bytes; cap at 500 means 2 fit, 3 do not.
        cache = ImageCache(dir=cache_dir, max_bytes=500)

        with aioresponses() as mocked:
            for i in range(3):
                url = f"https://example.com/{i}.jpg"
                mocked.get(url, body=IMG_BYTES)
                await cache.ensure("Artist", f"Album{i}", url)

        key0 = ImageCache.album_key("Artist", "Album0")
        key1 = ImageCache.album_key("Artist", "Album1")
        key2 = ImageCache.album_key("Artist", "Album2")

        assert key0 not in cache
        assert key1 in cache
        assert key2 in cache
        # Evicted file should also be deleted from disk
        assert not cache.file_path(key0).exists()
        assert cache.file_path(key1).exists()
        assert cache.file_path(key2).exists()

    @pytest.mark.asyncio
    async def test_lru_promotion_protects_recent(self, cache_dir):
        cache = ImageCache(dir=cache_dir, max_bytes=500)

        with aioresponses() as mocked:
            for i in range(2):
                url = f"https://example.com/{i}.jpg"
                mocked.get(url, body=IMG_BYTES)
                await cache.ensure("Artist", f"Album{i}", url)

            # Touch Album0 to promote it
            await cache.ensure("Artist", "Album0", "https://example.com/0.jpg")

            url2 = "https://example.com/2.jpg"
            mocked.get(url2, body=IMG_BYTES)
            await cache.ensure("Artist", "Album2", url2)

        # Album1 should be the one evicted, not Album0
        assert ImageCache.album_key("Artist", "Album0") in cache
        assert ImageCache.album_key("Artist", "Album1") not in cache
        assert ImageCache.album_key("Artist", "Album2") in cache

    @pytest.mark.asyncio
    async def test_keeps_at_least_one_entry(self, cache_dir):
        # Cap smaller than a single image
        cache = ImageCache(dir=cache_dir, max_bytes=10)

        url = "https://example.com/big.jpg"
        with aioresponses() as mocked:
            mocked.get(url, body=IMG_BYTES)
            local = await cache.ensure("Artist", "Album", url)

        assert local is not None
        assert len(cache) == 1


class TestPersistence:
    @pytest.mark.asyncio
    async def test_save_then_reload(self, cache_dir):
        cache1 = ImageCache(dir=cache_dir, max_bytes=10_000)
        url = "https://example.com/cover.jpg"
        with aioresponses() as mocked:
            mocked.get(url, body=IMG_BYTES)
            await cache1.ensure("Artist", "Album", url)

        cache2 = ImageCache(dir=cache_dir, max_bytes=10_000)
        assert len(cache2) == 1
        local = cache2.local_url_if_present("Artist", "Album")
        assert local is not None

    @pytest.mark.asyncio
    async def test_orphan_manifest_entry_dropped_on_load(self, cache_dir):
        cache1 = ImageCache(dir=cache_dir, max_bytes=10_000)
        url = "https://example.com/cover.jpg"
        with aioresponses() as mocked:
            mocked.get(url, body=IMG_BYTES)
            await cache1.ensure("Artist", "Album", url)

        # Delete the file but leave the manifest pointing to it
        key = ImageCache.album_key("Artist", "Album")
        cache1.file_path(key).unlink()

        cache2 = ImageCache(dir=cache_dir, max_bytes=10_000)
        assert len(cache2) == 0

    def test_corrupted_manifest_starts_fresh(self, cache_dir):
        cache_dir.mkdir(parents=True)
        (cache_dir / "manifest.json").write_text("{ not json")
        cache = ImageCache(dir=cache_dir, max_bytes=10_000)
        assert len(cache) == 0

    def test_missing_dir_starts_fresh(self, tmp_path):
        cache = ImageCache(dir=tmp_path / "nope", max_bytes=10_000)
        assert len(cache) == 0
