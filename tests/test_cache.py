import json

import pytest

from backend.cache import TrackCache
from backend.models import TrackInfo


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "tracks.json"


@pytest.fixture
def cache(cache_path):
    return TrackCache(path=cache_path, max_bytes=10_000)


def make_track(title: str, artist: str = "Artist") -> TrackInfo:
    return TrackInfo(
        title=title,
        artist=artist,
        album="Album",
        cover_url="http://example.com/cover.jpg",
        year="2024",
        genre="Rock",
        label="Label",
    )


class TestBasicGetPut:
    def test_get_miss_returns_none(self, cache):
        assert cache.get("missing:key") is None

    def test_put_then_get(self, cache):
        track = make_track("Song A")
        cache.put("artist:song a", track)

        result = cache.get("artist:song a")
        assert result is not None
        assert result.title == "Song A"
        assert result.cover_url == "http://example.com/cover.jpg"

    def test_contains(self, cache):
        cache.put("k", make_track("S"))
        assert "k" in cache
        assert "other" not in cache

    def test_len(self, cache):
        assert len(cache) == 0
        cache.put("a", make_track("A"))
        cache.put("b", make_track("B"))
        assert len(cache) == 2


class TestLRUOrdering:
    def test_get_promotes_to_most_recent(self, cache):
        cache.put("a", make_track("A"))
        cache.put("b", make_track("B"))
        cache.put("c", make_track("C"))

        cache.get("a")  # promote a to most recent

        # Order should now be b, c, a
        keys = list(cache._entries.keys())
        assert keys == ["b", "c", "a"]

    def test_put_existing_promotes(self, cache):
        cache.put("a", make_track("A"))
        cache.put("b", make_track("B"))
        cache.put("a", make_track("A2"))  # re-put a

        keys = list(cache._entries.keys())
        assert keys == ["b", "a"]


class TestSizeEviction:
    def test_oldest_evicted_when_size_exceeded(self, cache_path):
        # Tiny cache: each entry is ~200+ bytes serialized
        # Each entry is ~180 bytes; cap at 400 means 2 fit, 3 do not.
        cache = TrackCache(path=cache_path, max_bytes=400)

        cache.put("a", make_track("A"))
        cache.put("b", make_track("B"))
        cache.put("c", make_track("C"))

        # 'a' should have been evicted (oldest)
        assert "a" not in cache
        assert "b" in cache
        assert "c" in cache

    def test_eviction_respects_lru_order(self, cache_path):
        # Each entry is ~180 bytes; cap at 400 means 2 fit, 3 do not.
        cache = TrackCache(path=cache_path, max_bytes=400)

        cache.put("a", make_track("A"))
        cache.put("b", make_track("B"))
        cache.get("a")  # promote a, now b is oldest
        cache.put("c", make_track("C"))

        # b should have been evicted, not a
        assert "b" not in cache
        assert "a" in cache
        assert "c" in cache

    def test_keeps_at_least_one_entry(self, cache_path):
        # Set max smaller than a single entry
        cache = TrackCache(path=cache_path, max_bytes=10)

        cache.put("a", make_track("A"))

        # Even though it exceeds max_bytes, we keep it
        assert len(cache) == 1
        assert "a" in cache

    def test_size_bytes_grows(self, cache):
        before = cache.size_bytes()
        cache.put("a", make_track("A"))
        after = cache.size_bytes()
        assert after > before


class TestPersistence:
    def test_save_then_reload(self, cache_path):
        cache1 = TrackCache(path=cache_path, max_bytes=10_000)
        cache1.put("a", make_track("A"))
        cache1.put("b", make_track("B"))

        # New instance reads from disk
        cache2 = TrackCache(path=cache_path, max_bytes=10_000)
        assert len(cache2) == 2
        assert cache2.get("a").title == "A"
        assert cache2.get("b").title == "B"

    def test_lru_order_persisted(self, cache_path):
        cache1 = TrackCache(path=cache_path, max_bytes=10_000)
        cache1.put("a", make_track("A"))
        cache1.put("b", make_track("B"))
        cache1.put("c", make_track("C"))
        cache1.get("a")  # promote a

        cache2 = TrackCache(path=cache_path, max_bytes=10_000)
        keys = list(cache2._entries.keys())
        assert keys == ["b", "c", "a"]

    def test_missing_file_starts_fresh(self, tmp_path):
        cache = TrackCache(path=tmp_path / "nonexistent.json", max_bytes=10_000)
        assert len(cache) == 0

    def test_corrupted_file_starts_fresh(self, cache_path):
        cache_path.write_text("not valid json {{{")
        cache = TrackCache(path=cache_path, max_bytes=10_000)
        assert len(cache) == 0

    def test_atomic_write_no_partial_file(self, cache_path):
        cache = TrackCache(path=cache_path, max_bytes=10_000)
        cache.put("a", make_track("A"))

        # No leftover .tmp file
        assert not cache_path.with_suffix(cache_path.suffix + ".tmp").exists()
        # Real file is valid JSON
        with open(cache_path) as f:
            data = json.load(f)
        assert "a" in data

    def test_creates_parent_dir(self, tmp_path):
        nested = tmp_path / "deeply" / "nested" / "tracks.json"
        cache = TrackCache(path=nested, max_bytes=10_000)
        cache.put("a", make_track("A"))
        assert nested.exists()
