import json
import logging
import pathlib
from collections import OrderedDict
from dataclasses import asdict

from backend.models import TrackInfo

log = logging.getLogger("framedisplay")


class TrackCache:
    """LRU cache of enriched TrackInfo, persisted to a JSON file on disk.

    Eviction is by total serialized size: when adding a new entry pushes the
    cache over max_bytes, the oldest entries are dropped until it fits.
    """

    def __init__(self, path: pathlib.Path | str, max_bytes: int):
        self.path = pathlib.Path(path)
        self.max_bytes = max_bytes
        self._entries: OrderedDict[str, dict] = OrderedDict()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("Failed to create track cache dir %s: %s", self.path.parent, e)
        self._load()

    def get(self, key: str) -> TrackInfo | None:
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        self._save()  # persist new LRU order
        return TrackInfo(**self._entries[key])

    def put(self, key: str, track: TrackInfo) -> None:
        self._entries[key] = asdict(track)
        self._entries.move_to_end(key)
        self._evict_if_needed()
        self._save()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def size_bytes(self) -> int:
        return len(json.dumps(self._entries).encode("utf-8"))

    def _evict_if_needed(self) -> None:
        # Always keep at least one entry (the one we just added) even if it
        # alone exceeds max_bytes — better to have it than nothing.
        while self.size_bytes() > self.max_bytes and len(self._entries) > 1:
            evicted_key, _ = self._entries.popitem(last=False)
            log.info("Cache evicted oldest entry: %s", evicted_key)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            self._entries = OrderedDict(data)
            log.info("Cache loaded %d entries from %s", len(self._entries), self.path)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load cache from %s (%s), starting fresh", self.path, e)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w") as f:
                json.dump(self._entries, f)
            tmp.replace(self.path)  # atomic on POSIX
        except OSError as e:
            log.warning("Failed to save cache to %s: %s", self.path, e)
