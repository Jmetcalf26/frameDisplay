# Frame Display

Identifies currently playing vinyl/CD music and displays album art on a Samsung Frame TV.

**Stack:** Python 3.10+ / shazamio / Discogs API / aiohttp / Chromium kiosk

## Architecture

```
┌─────────────┐     WAV bytes     ┌──────────────┐
│  Microphone  │ ───────────────> │  Recognizer   │
│  (audio.py)  │                  │  (shazamio)   │
└─────────────┘                   └──────┬───────┘
                                         │ TrackInfo | None
                                         │  (None on full recording → fallback)
                                         ▼
                                  ┌──────────────┐
                                  │  AcoustID     │ (optional Chromaprint fallback)
                                  │ (fpcalc/MB)   │
                                  └──────┬───────┘
                                         │ TrackInfo | None
                                         ▼
                                  ┌──────────────┐
                                  │  Recency +    │ ── stale or same? → skip
                                  │  Dedup Check  │
                                  └──────┬───────┘
                                         │ new track
                                         ▼
                                  ┌──────────────┐
                                  │  Track Cache  │ ── hit? → reuse cached metadata
                                  │  (cache.py)   │
                                  └──────┬───────┘
                                         │ miss
                                         ▼
                                  ┌──────────────┐
                                  │   Discogs     │ (optional enrichment)
                                  │  (discogs.py) │
                                  └──────┬───────┘
                                         │ enriched TrackInfo
                                         ▼
                                  ┌──────────────┐
                                  │  Image Cache  │ ── ensures album art on disk
                                  │ (image_cache) │
                                  └──────┬───────┘
                                         │
                                         ▼
                                  ┌──────────────┐    WebSocket     ┌────────────┐
                                  │  Broadcast   │ ──────────────> │  Frontend   │
                                  │  (app.py)    │                 │  (browser)  │
                                  └──────────────┘                 └────────────┘
```

The backend records audio in a continuous loop, firing **two parallel snapshots** at each window: a *cumulative* snapshot from the start and a *windowed* snapshot covering only the most recent segment. Both are sent to Shazam (calls are serialized through an `asyncio.Lock` because shazamio races on concurrent calls). A recency check rejects results whose audio ended before the currently displayed track's; at equal end times, the windowed result wins because its audio is fresher.

When a new track is identified, its display key (`artist:title` lowercased) is checked against an on-disk **track cache** of previously enriched metadata. On a miss, the raw Apple Music cover URL is upscaled, Discogs is queried for label/year/genre + hi-res art, and the result is cached. On a hit, all of that is skipped.

Album art bytes are stored separately in an **image cache** keyed by `(artist, album)` so that two songs from the same record share the same file. Both caches use byte-size LRU eviction so the Pi doesn't run out of disk.

There is no idle state — the last identified track stays on screen until a new one is recognized.

## Project Structure

```
frameDisplay/
├── config.yaml              # Mic settings, API keys, timing, server port (gitignored)
├── config.example.yaml      # Template with placeholder values
├── requirements.txt         # Runtime dependencies
├── requirements-dev.txt     # Test dependencies
├── run.py                   # Entry point
├── backend/
│   ├── app.py               # aiohttp server, WebSocket, listen loop
│   ├── audio.py             # Mic recording with mid-stream snapshots
│   ├── recognizer.py        # shazamio wrapper + Apple Music CDN upscaling
│   ├── acoustid_client.py   # AcoustID/Chromaprint fallback recognizer
│   ├── discogs.py           # Discogs API search + enrichment
│   ├── cache.py             # Persistent track metadata cache (LRU)
│   ├── image_cache.py       # Persistent album-art file cache (LRU)
│   ├── list_devices.py      # `python -m backend.list_devices` to find your mic
│   └── models.py            # TrackInfo dataclass, DisplayState enum
├── frontend/
│   ├── index.html           # Single-page display
│   ├── style.css            # Vertical layout, art-on-wall aesthetic
│   └── app.js               # WebSocket client, DOM updates
├── cache/                   # Created at runtime: tracks.json + images/
├── scripts/
│   ├── install.sh           # Linux/Pi dependency install
│   ├── install-mac.sh       # macOS dependency install
│   └── framedisplay.service # systemd unit file
└── tests/                   # 96 tests, see Testing section
```

## Setup

### Raspberry Pi (Linux)

```bash
bash scripts/install.sh
```

### macOS (local development)

```bash
bash scripts/install-mac.sh
```

### Then

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your Discogs credentials and mic device

source venv/bin/activate
python -m backend.list_devices    # find your mic, paste the index into config.yaml
python run.py
```

Open `http://localhost:8080` in a browser (or Chromium kiosk mode on the Pi).

## Configuration

See `config.example.yaml` for all options. Key settings:

- `audio.device` — mic device index (`null` for system default; use `python -m backend.list_devices` to list them)
- `audio.listeners` — list of snapshot durations. The longest is the full recording; the rest fire mid-stream. Default `[5, 10]` records for 10s and snapshots at 5s.
- `audio.loop_interval` — seconds to sleep between recording cycles (`0` = no sleep, default)
- `discogs.consumer_key` / `consumer_secret` — your Discogs API credentials
- `discogs.enabled` — set `false` to use only Shazam cover art
- `acoustid.enabled` / `acoustid.api_key` — optional Chromaprint fallback (see below)
- `cache.enabled` / `cache.max_bytes` — track metadata cache (default 512 KB)
- `image_cache.enabled` / `image_cache.max_bytes` — album-art file cache (default 100 MB)

### AcoustID fallback recognizer

Shazam has blind spots — older catalog music, classical, jazz, certain pressings — and a different fingerprinting algorithm catches different things. AcoustID + Chromaprint runs as a fallback whenever Shazam returns no match on the full recording. It's free; register at <https://acoustid.org/api-key>.

It only fires on the longest listener (where there's enough audio for Chromaprint to be confident), and only when Shazam misses, so the happy path costs nothing extra. Hits are then enriched by Discogs and cached just like Shazam hits.

Requires the `fpcalc` binary, installed by `scripts/install.sh` via `libchromaprint-tools` (Linux) or `brew install chromaprint` (macOS). Set `acoustid.enabled: true` and paste your API key into `config.yaml` to turn it on.

## Testing

```bash
source venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

The test suite (116 tests) covers all backend components with mocked external dependencies:

| Module | Coverage |
|---|---|
| `models.py` | `display_key` dedup logic, defaults, enum values |
| `recognizer.py` | Shazam response parsing, Apple Music CDN upscaling, missing fields |
| `acoustid_client.py` | AcoustID lookup parsing, score thresholding, error handling |
| `discogs.py` | Enrichment, multi-genre/label joins, API and network errors |
| `audio.py` | sounddevice parameters, WAV format output, mid-stream snapshots |
| `cache.py` | LRU eviction, byte-size capping, atomic disk persistence |
| `image_cache.py` | Per-album keying, LRU eviction, orphan manifest entries, download failures |
| `app.py` | Init, message building, recency + dedup, recognizer lock, AcoustID fallback gating, cache integration, shutdown, WebSocket broadcast |

## Deployment (Raspberry Pi)

The systemd service auto-starts the backend with `Restart=always`, journal logging, and a 512 MB memory cap. For kiosk display, add to autostart:

```
chromium-browser --kiosk --noerrdialogs --disable-infobars --incognito http://localhost:8080
```

For a portrait-oriented Frame TV, set `display_rotate=1` (or `3`) in `/boot/firmware/config.txt`.
