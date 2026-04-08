from dataclasses import dataclass
from enum import Enum


class DisplayState(str, Enum):
    LISTENING = "listening"
    IDENTIFIED = "identified"
    IDLE = "idle"


@dataclass
class TrackInfo:
    title: str
    artist: str
    album: str | None = None
    cover_url: str | None = None
    cover_url_hires: str | None = None
    year: str | None = None
    genre: str | None = None
    label: str | None = None

    @property
    def display_key(self) -> str:
        return f"{self.artist}:{self.title}".lower()
