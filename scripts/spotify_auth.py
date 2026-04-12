"""One-time Spotify OAuth bootstrap.

Run this once on any machine with a browser. It opens Spotify's consent
page, captures the auth code via a localhost redirect, exchanges it for
a refresh token, and writes the refresh token to the configured file.

Usage:
    python scripts/spotify_auth.py \\
        --client-id YOUR_CLIENT_ID \\
        --client-secret YOUR_CLIENT_SECRET

The Spotify app's redirect URI must be set to http://127.0.0.1:8888/callback
in the Spotify developer dashboard.
"""

import argparse
import base64
import http.server
import pathlib
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

REDIRECT_URI = "http://127.0.0.1:8888/callback"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPE = "user-read-currently-playing user-read-playback-state"


class _Handler(http.server.BaseHTTPRequestHandler):
    # Filled in by the main thread before serving.
    result: dict = {}
    expected_state: str = ""

    def do_GET(self):  # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        state = (qs.get("state") or [""])[0]
        code = (qs.get("code") or [""])[0]
        error = (qs.get("error") or [""])[0]

        if error:
            _Handler.result["error"] = error
            body = f"Auth failed: {error}. You can close this tab."
        elif state != _Handler.expected_state:
            _Handler.result["error"] = "state mismatch"
            body = "State mismatch. You can close this tab."
        elif not code:
            _Handler.result["error"] = "no code in callback"
            body = "No code returned. You can close this tab."
        else:
            _Handler.result["code"] = code
            body = "Spotify auth successful. You can close this tab."

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args):
        pass  # quiet the stdlib's default stderr logging


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument(
        "--token-file",
        default="cache/spotify-token.txt",
        help="Path to write the refresh token (relative to repo root or absolute)",
    )
    args = parser.parse_args()

    token_path = pathlib.Path(args.token_file)
    if not token_path.is_absolute():
        # Run from repo root typically; resolve against cwd.
        token_path = pathlib.Path.cwd() / token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)

    state = secrets.token_urlsafe(24)
    _Handler.expected_state = state
    _Handler.result = {}

    auth_params = {
        "client_id": args.client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "show_dialog": "true",
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(auth_params)}"

    server = http.server.HTTPServer(("127.0.0.1", 8888), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"Opening browser to: {auth_url}")
    try:
        webbrowser.open(auth_url)
    except Exception:
        print("Could not open browser automatically; paste the URL above into one.")

    print("Waiting for Spotify redirect...")
    while not _Handler.result:
        pass
    server.shutdown()

    if "error" in _Handler.result:
        print(f"Auth failed: {_Handler.result['error']}", file=sys.stderr)
        return 1

    code = _Handler.result["code"]

    # Exchange the code for tokens.
    basic = base64.b64encode(
        f"{args.client_id}:{args.client_secret}".encode()
    ).decode()
    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            import json
            payload = json.loads(resp.read().decode())
    except Exception as e:
        print(f"Token exchange failed: {e}", file=sys.stderr)
        return 1

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        print(f"No refresh_token in response: {payload}", file=sys.stderr)
        return 1

    token_path.write_text(refresh_token)
    print(f"Refresh token written to: {token_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
