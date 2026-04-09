import asyncio
import hashlib
import json
import logging
import pathlib
from collections import OrderedDict

import aiohttp

log = logging.getLogger("framedisplay")


class ImageCache:
    """LRU cache of album art bytes stored as files on disk.

    Keyed by (artist, album) so two songs from the same album share an entry.
    Eviction is by total file size: when adding a new image pushes the cache
    over max_bytes, the oldest entries are deleted from disk until it fits.
    """

    URL_PREFIX = "/cache/images/"

    def __init__(self, dir: pathlib.Path | str, max_bytes: int):
        self.dir = pathlib.Path(dir)
        self.max_bytes = max_bytes
        self.manifest_path = self.dir / "manifest.json"
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._lock = asyncio.Lock()
        self._load()

    @staticmethod
    def album_key(artist: str, album: str) -> str:
        normalized = f"{artist}:{album}".lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def file_path(self, key: str) -> pathlib.Path:
        return self.dir / f"{key}.jpg"

    def url_for(self, key: str) -> str:
        return f"{self.URL_PREFIX}{key}.jpg"

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def total_bytes(self) -> int:
        return sum(e["size"] for e in self._entries.values())

    def local_url_if_present(self, artist: str, album: str) -> str | None:
        """Return the local /cache/images/... URL if the image is cached, else None."""
        key = self.album_key(artist, album)
        if key in self._entries and self.file_path(key).exists():
            return self.url_for(key)
        return None

    async def ensure(self, artist: str, album: str, source_url: str) -> str | None:
        """Make sure the album's image is cached, downloading if needed.

        Promotes the entry to most-recently-used. Returns the local cache URL,
        or None if the source isn't HTTP-fetchable or the download fails.
        """
        if not source_url or not source_url.startswith(("http://", "https://")):
            return None

        key = self.album_key(artist, album)

        async with self._lock:
            if key in self._entries and self.file_path(key).exists():
                self._entries.move_to_end(key)
                self._save_manifest()
                log.info("Image cache hit: %s / %s", artist, album)
                return self.url_for(key)
            # Stale manifest entry whose file got deleted externally
            if key in self._entries:
                del self._entries[key]

        log.info("Image cache miss: %s / %s, downloading", artist, album)
        try:
            data = await self._download(source_url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Image download failed for %s: %s", source_url, e)
            return None

        async with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                self.file_path(key).write_bytes(data)
            except OSError as e:
                log.warning("Failed to write image %s: %s", key, e)
                return None
            self._entries[key] = {"size": len(data)}
            self._entries.move_to_end(key)
            self._evict_if_needed()
            self._save_manifest()
            log.info(
                "Image cached: %s / %s (%d bytes, total %d / %d)",
                artist,
                album,
                len(data),
                self.total_bytes(),
                self.max_bytes,
            )

        return self.url_for(key)

    async def _download(self, url: str) -> bytes:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return await resp.read()

    def _evict_if_needed(self) -> None:
        # Always keep at least the entry we just added.
        while self.total_bytes() > self.max_bytes and len(self._entries) > 1:
            evicted_key, _ = self._entries.popitem(last=False)
            try:
                self.file_path(evicted_key).unlink()
            except OSError:
                pass
            log.info("Image cache evicted: %s", evicted_key)

    def _load(self) -> None:
        if not self.manifest_path.exists():
            return
        try:
            with open(self.manifest_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load image cache manifest: %s", e)
            return
        # Drop manifest entries whose backing files have disappeared.
        kept = OrderedDict()
        for k, v in data.items():
            if self.file_path(k).exists():
                kept[k] = v
        self._entries = kept
        log.info("Image cache loaded %d entries", len(self._entries))

    def _save_manifest(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.manifest_path.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(self._entries, f)
            tmp.replace(self.manifest_path)
        except OSError as e:
            log.warning("Failed to save image cache manifest: %s", e)
