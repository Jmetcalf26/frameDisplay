"""Probe what image sizes Spotify's /v1/albums/{id} endpoint actually returns.

Uses Client Credentials auth (no user login needed) — works for any public
catalog endpoint. Prints the full images[] array for a handful of albums
so we can verify empirically whether 3000x3000 is available or whether
the 640x640 cap is real.

Usage:
    python scripts/spotify_probe_images.py \\
        --client-id YOUR_ID --client-secret YOUR_SECRET
"""

import argparse
import base64
import json
import sys
import urllib.parse
import urllib.request

TOKEN_URL = "https://accounts.spotify.com/api/token"
ALBUMS_URL = "https://api.spotify.com/v1/albums/{id}"
TRACKS_URL = "https://api.spotify.com/v1/tracks/{id}"

# A spread of albums — different eras, labels, popularity tiers.
SAMPLE_ALBUM_IDS = [
    ("Radiohead - In Rainbows", "7eyQXxuf2nGj9d2367Gi5f"),
    ("The Beatles - Abbey Road", "0ETFjACtuP2ADo6LFhL6HN"),
    ("Kendrick Lamar - DAMN.", "4eLPsYPBmXABThSJ821sqY"),
    ("Taylor Swift - 1989 (Taylor's Version)", "64LU4c1nfjz1t4VnGhagcg"),
    ("Fleetwood Mac - Rumours", "1bt6q2SruMsBtcerNVtpZB"),
]
SAMPLE_TRACK_IDS = [
    ("Radiohead - Nude", "1hKdDCpiI9mqz1jVHRKG0E"),
]


def get_app_token(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read().decode())
    return payload["access_token"]


def fetch(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    args = parser.parse_args()

    token = get_app_token(args.client_id, args.client_secret)

    print("=== /v1/albums/{id} ===")
    for label, album_id in SAMPLE_ALBUM_IDS:
        data = fetch(ALBUMS_URL.format(id=album_id), token)
        print(f"\n{label}")
        for img in data.get("images", []):
            print(f"  {img.get('width')}x{img.get('height')}  {img.get('url')}")

    print("\n=== /v1/tracks/{id} (images live on track.album.images) ===")
    for label, track_id in SAMPLE_TRACK_IDS:
        data = fetch(TRACKS_URL.format(id=track_id), token)
        album = data.get("album", {})
        print(f"\n{label}  (album: {album.get('name')})")
        for img in album.get("images", []):
            print(f"  {img.get('width')}x{img.get('height')}  {img.get('url')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
