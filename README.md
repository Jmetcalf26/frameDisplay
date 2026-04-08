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
                                         ▼
                                  ┌──────────────┐
                                  │  Dedup Check  │ ── same song? → sleep & loop
                                  └──────┬───────┘
                                         │ new track
                                         ▼
                                  ┌──────────────┐
                                  │   Discogs     │ (optional enrichment)
                                  │  (discogs.py) │
                                  └──────┬───────┘
                                         │ enriched TrackInfo
                                         ▼
                                  ┌──────────────┐    WebSocket     ┌────────────┐
                                  │  Broadcast   │ ──────────────> │  Frontend   │
                                  │  (app.py)    │                 │  (browser)  │
                                  └──────────────┘                 └────────────┘
```

The backend runs a continuous loop: record a 5-second audio snippet → identify via Shazam → deduplicate → optionally enrich with Discogs metadata → broadcast to the frontend over WebSocket. The frontend displays album art and track info in a vertical layout optimized for a portrait-oriented Frame TV.

Three consecutive failed recognitions (~48 seconds) are required before clearing the display, preventing flicker during quiet passages or between tracks.

## Project Structure

```
frameDisplay/
├── config.yaml              # Mic settings, API keys, timing, server port
├── config.example.yaml      # Template with placeholder values
├── requirements.txt         # Runtime dependencies
├── requirements-dev.txt     # Test dependencies
├── run.py                   # Entry point
├── backend/
│   ├── app.py               # aiohttp server, WebSocket, listen loop
│   ├── audio.py             # Record mic → WAV bytes in memory
│   ├── recognizer.py        # shazamio wrapper
│   ├── discogs.py            # Discogs API search + enrichment
│   └── models.py            # TrackInfo dataclass, DisplayState enum
├── frontend/
│   ├── index.html           # Single-page display
│   ├── style.css            # Vertical layout, art-on-wall aesthetic
│   └── app.js               # WebSocket client, DOM updates
├── scripts/
│   ├── install.sh           # Linux/Pi dependency install
│   ├── install-mac.sh       # macOS dependency install
│   └── framedisplay.service # systemd unit file
└── tests/
    ├── test_models.py
    ├── test_recognizer.py
    ├── test_discogs.py
    ├── test_audio.py
    └── test_app.py
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
# Edit config.yaml with your Discogs credentials

source venv/bin/activate
python run.py
```

Open `http://localhost:8080` in a browser (or Chromium kiosk mode on the Pi).

## Configuration

See `config.example.yaml` for all options. Key settings:

- `audio.device` — mic device index (`null` for system default)
- `audio.snippet_duration` — seconds of audio to record per attempt (default: 5)
- `audio.loop_interval` — seconds between recognition attempts (default: 10)
- `discogs.consumer_key` / `consumer_secret` — your Discogs API credentials
- `discogs.enabled` — set `false` to use only Shazam cover art

## Testing

```bash
source venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

The test suite (41 tests) covers all backend components with mocked external dependencies:

| Module | Coverage |
|---|---|
| `models.py` | `display_key` dedup logic, defaults, enum values |
| `recognizer.py` | Shazam response parsing, cover art fallback, missing fields |
| `discogs.py` | Enrichment, multi-genre/label joins, API and network errors |
| `audio.py` | sounddevice parameters, WAV format output |
| `app.py` | Config init, message building, dedup, WebSocket broadcast, dead client cleanup |

## Deployment (Raspberry Pi)

The systemd service auto-starts the backend. For kiosk display, add to autostart:

```
chromium-browser --kiosk --noerrdialogs --disable-infobars --incognito http://localhost:8080
```
