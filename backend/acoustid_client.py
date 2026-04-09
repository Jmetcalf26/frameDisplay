"""AcoustID fallback recognizer.

Uses Chromaprint fingerprinting (via the pyacoustid package and the
fpcalc binary) to identify tracks that Shazam couldn't. Returns the
same TrackInfo shape as backend.recognizer.Recognizer so the caller
can treat the two interchangeably.
"""

import asyncio
import logging
import os
import tempfile

import acoustid

from backend.models import TrackInfo

log = logging.getLogger("framedisplay")

CAA_URL_TEMPLATE = "https://coverartarchive.org/release/{mbid}/front-500"


class AcoustIDClient:
    def __init__(self, api_key: str, min_score: float = 0.5):
        self.api_key = api_key
        self.min_score = min_score

    async def identify(self, audio_bytes: bytes) -> TrackInfo | None:
        """Fingerprint WAV bytes and look the result up via AcoustID."""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._identify_sync, audio_bytes)
        except Exception:
            log.exception("AcoustID identify crashed")
            return None

    def _identify_sync(self, audio_bytes: bytes) -> TrackInfo | None:
        # pyacoustid wants a file path. Use a tempfile in /tmp (tmpfs on Pi OS).
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            try:
                duration, fingerprint = acoustid.fingerprint_file(tmp_path)
            except acoustid.NoBackendError:
                log.warning(
                    "AcoustID: fpcalc not found. Install libchromaprint-tools."
                )
                return None
            except acoustid.FingerprintGenerationError as e:
                log.warning("AcoustID: fingerprint generation failed: %s", e)
                return None

            try:
                response = acoustid.lookup(
                    self.api_key,
                    fingerprint,
                    duration,
                    meta=["recordings", "releases"],
                )
            except acoustid.WebServiceError as e:
                log.warning("AcoustID: lookup failed: %s", e)
                return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        return self._parse_response(response)

    def _parse_response(self, response: dict) -> TrackInfo | None:
        """Pull the best-scoring recording out of an AcoustID lookup response."""
        if response.get("status") != "ok":
            log.warning("AcoustID: non-ok status: %s", response.get("status"))
            return None

        results = response.get("results") or []
        if not results:
            return None

        # Results come sorted by score descending, but be defensive.
        best = max(results, key=lambda r: r.get("score", 0))
        score = best.get("score", 0)
        if score < self.min_score:
            log.info(
                "AcoustID: best score %.2f below threshold %.2f", score, self.min_score
            )
            return None

        recordings = best.get("recordings") or []
        if not recordings:
            log.info("AcoustID: result had no recordings")
            return None

        recording = recordings[0]
        title = recording.get("title")
        if not title:
            log.info("AcoustID: recording had no title")
            return None

        artist = self._join_artists(recording.get("artists", []))
        album, year, release_mbid = self._pick_release(recording.get("releases", []))

        cover_url = CAA_URL_TEMPLATE.format(mbid=release_mbid) if release_mbid else None

        log.info(
            "AcoustID: %s - %s (%s, score=%.2f)",
            artist or "Unknown",
            title,
            album or "no album",
            score,
        )
        return TrackInfo(
            title=title,
            artist=artist or "Unknown",
            album=album,
            cover_url=cover_url,
            year=year,
        )

    @staticmethod
    def _join_artists(artists: list) -> str | None:
        if not artists:
            return None
        parts = []
        for a in artists:
            name = a.get("name", "")
            joinphrase = a.get("joinphrase", "")
            parts.append(name + joinphrase)
        joined = "".join(parts).strip()
        return joined or None

    @staticmethod
    def _pick_release(releases: list) -> tuple[str | None, str | None, str | None]:
        """Pick the most useful release: prefer one with a title and a date."""
        if not releases:
            return None, None, None
        # Prefer releases that have both a title and a date (year).
        scored = sorted(
            releases,
            key=lambda r: (bool(r.get("title")), bool(r.get("date", {}).get("year"))),
            reverse=True,
        )
        release = scored[0]
        title = release.get("title")
        year_int = release.get("date", {}).get("year")
        year = str(year_int) if year_int else None
        mbid = release.get("id")
        return title, year, mbid
