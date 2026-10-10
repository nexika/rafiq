"""Spotify, with the user's own account: play, pause, skip, play on another device, add songs to playlists.

Setup, once: create an app at https://developer.spotify.com/dashboard with the redirect URI
http://127.0.0.1:8888/callback, put its client ID under [spotify] client_id in the config (or in
SPOTIFY_CLIENT_ID), then run `rafiq spotify-setup`. The login uses PKCE, so no client secret is stored;
the token is kept in ~/.config/rafiq/spotify/token.json, readable only by you. Playback control needs
Spotify Premium; searching and playlists work without it.

Songs Rafiq recognises go to the "Rafiq finds" playlist through save_found(), made on first use.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from .. import config
from . import tool

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
SCOPES = ["user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing",
          "playlist-read-private", "playlist-modify-private", "playlist-modify-public"]
FINDS = "Rafiq finds"
KINDS = ("track", "playlist", "album", "artist")


class SpotifyError(Exception):
    def __init__(self, status: int, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.status, self.reason = status, reason


# ---------------------------------------------------------------------- login and the HTTP client

def folder() -> Path:
    path = config.home() / "spotify"
    path.mkdir(parents=True, exist_ok=True)
    return path


def client_id(cfg: dict) -> str:
    return os.environ.get("SPOTIFY_CLIENT_ID") or cfg.get("spotify", {}).get("client_id", "")


def redirect_uri(cfg: dict) -> str:
    return f"http://127.0.0.1:{int(cfg.get('spotify', {}).get('port', 8888))}/callback"


def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge) for the PKCE login: the challenge is the verifier's SHA-256, base64url."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client: str, redirect: str, challenge: str, state: str) -> str:
    return f"{ACCOUNTS}/authorize?" + urlencode({
        "client_id": client, "response_type": "code", "redirect_uri": redirect, "scope": " ".join(SCOPES),
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state})


def save_token(path: Path, answer: dict, previous: dict | None = None) -> dict:
    """Keep a token answer from Spotify; a refresh may leave out the refresh token, so the old one stays."""
    token = {"access_token": answer["access_token"],
             "refresh_token": answer.get("refresh_token") or (previous or {}).get("refresh_token", ""),
             "expires_at": time.time() + int(answer.get("expires_in", 3600))}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(token, out)
    path.chmod(0o600)
    return token


class Api:
    """The Spotify Web API as the user: refreshes the token when it runs out, and turns error answers
    into SpotifyError with Spotify's reason (NO_ACTIVE_DEVICE, PREMIUM_REQUIRED...)."""

    def __init__(self, client: str, token_path: Path, http=None):
        import httpx

        self.client, self.token_path = client, token_path
        self.http = http or httpx.Client(timeout=10)

    def _token(self, force_refresh: bool = False) -> str:
        if not self.token_path.is_file():
            raise SpotifyError(0, "NOT_SET_UP", "Spotify is not set up: run `rafiq spotify-setup`")
        token = json.loads(self.token_path.read_text())
        if force_refresh or token.get("expires_at", 0) < time.time() + 60:
            resp = self.http.post(f"{ACCOUNTS}/api/token", data={
                "grant_type": "refresh_token", "refresh_token": token.get("refresh_token", ""),
                "client_id": self.client})
            if resp.status_code != 200:
                raise SpotifyError(resp.status_code, "LOGIN_EXPIRED",
                                   "the Spotify login has expired: run `rafiq spotify-setup` again")
            token = save_token(self.token_path, resp.json(), token)
        return token["access_token"]

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict | None:
        resp = None
        for attempt in range(2):
            resp = self.http.request(method, API + path, params=params, json=body,
                                     headers={"Authorization": f"Bearer {self._token(force_refresh=attempt > 0)}"})
            if resp.status_code != 401:
                break
        if resp.status_code >= 400:
            try:
                error = resp.json().get("error", {})
            except ValueError:
                error = {}
            error = error if isinstance(error, dict) else {"message": str(error)}
            raise SpotifyError(resp.status_code, error.get("reason", ""), error.get("message", resp.reason_phrase))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()


def exchange_code(client: str, code: str, verifier: str, redirect: str, http) -> dict:
    resp = http.post(f"{ACCOUNTS}/api/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": client,
        "code_verifier": verifier}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def setup(cfg: dict, timeout: float = 300) -> str:
    """Log in in the browser; a one-shot server on 127.0.0.1 catches Spotify's answer."""
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    import httpx

    client = client_id(cfg)
    if not client:
        return ("Create an app at https://developer.spotify.com/dashboard with the redirect URI "
                f"{redirect_uri(cfg)}, then put its client ID under [spotify] client_id in "
                f"{config.home() / 'config.toml'} (or in SPOTIFY_CLIENT_ID).")
    redirect, state = redirect_uri(cfg), secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair()
    answer: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server's name)
            query = parse_qs(urlparse(self.path).query)
            answer.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Rafiq: you can close this tab.")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", int(cfg.get("spotify", {}).get("port", 8888))), Callback)
    server.timeout = timeout
    url = authorize_url(client, redirect, challenge, state)
    print(f"Opening the Spotify login. If no browser opens, visit:\n{url}", flush=True)
    webbrowser.open(url)
    with server:
        server.handle_request()
    if answer.get("state") != state or "code" not in answer:
        return f"Spotify login failed: {answer.get('error', 'no answer')}"
    with httpx.Client(timeout=15) as http:
        token = exchange_code(client, answer["code"], verifier, redirect, http)
    path = folder() / "token.json"
    save_token(path, token)
    return f"Spotify connected; token saved at {path}."


# ---------------------------------------------------------------------- what the tools do

def _said(fn):
    """Spotify's errors in words the user hears."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        try:
            return fn(*args, **kwargs)
        except SpotifyError as exc:
            if exc.reason == "NO_ACTIVE_DEVICE":
                return "error: no Spotify device is active: open Spotify on a phone, computer or speaker first"
            if exc.reason == "PREMIUM_REQUIRED" or (exc.status == 403 and "premium" in str(exc).lower()):
                return "error: controlling playback needs Spotify Premium"
            return f"error: Spotify: {exc}"
    return wrapper


def _artists(item: dict) -> str:
    return ", ".join(a["name"] for a in item.get("artists", []) if a.get("name"))


def _title(item: dict) -> str:
    artists = _artists(item)
    return f"{item.get('name', '')} by {artists}" if artists else item.get("name", "")


def _device(api, name: str) -> dict | str:
    """The device whose name contains `name`, or the error to say."""
    found = (api.call("GET", "/me/player/devices") or {}).get("devices", [])
    wanted = name.casefold().strip()
    for device in found:
        if wanted in device.get("name", "").casefold():
            return device
    names = ", ".join(d.get("name", "") for d in found) or "none"
    return f"error: no Spotify device called {name!r}; the devices are: {names}"


@_said
def play(api, query: str = "", kind: str = "track", device: str = "") -> str:
    params, where = None, ""
    if device:
        target = _device(api, device)
        if isinstance(target, str):
            return target
        params, where = {"device_id": target["id"]}, f" on {target['name']}"
    if not query:
        api.call("PUT", "/me/player/play", params)
        return "resumed" + where
    kind = kind if kind in KINDS else "track"
    found = api.call("GET", "/search", {"q": query, "type": kind, "limit": 1}) or {}
    items = [i for i in found.get(kind + "s", {}).get("items", []) if i]
    if not items:
        return f"error: nothing on Spotify for {query!r}"
    item = items[0]
    if kind == "track":
        api.call("PUT", "/me/player/play", params, {"uris": [item["uri"]]})
        return f"playing {_title(item)}{where}"
    api.call("PUT", "/me/player/play", params, {"context_uri": item["uri"]})
    what = {"playlist": f"the playlist {item.get('name')}", "album": f"the album {_title(item)}"}
    return f"playing {what.get(kind, item.get('name'))}{where}"


CONTROLS = {"pause": ("PUT", "/me/player/pause", "paused"), "resume": ("PUT", "/me/player/play", "resumed"),
            "next": ("POST", "/me/player/next", "skipped to the next song"),
            "previous": ("POST", "/me/player/previous", "back to the previous song")}


@_said
def control(api, action: str) -> str:
    if action not in CONTROLS:
        return f"error: the actions are {', '.join(CONTROLS)}"
    method, path, said = CONTROLS[action]
    api.call(method, path)
    return said


def _playing(api) -> dict | None:
    now = api.call("GET", "/me/player/currently-playing") or {}
    return now.get("item") if now.get("item") and now.get("item", {}).get("uri") else None


@_said
def now_playing(api) -> str:
    item = _playing(api)
    return f"playing now: {_title(item)}" if item else "nothing is playing"


@_said
def devices(api) -> str:
    found = (api.call("GET", "/me/player/devices") or {}).get("devices", [])
    lines = [f"- {d.get('name')} ({d.get('type', 'device')}{', playing here' if d.get('is_active') else ''})"
             for d in found]
    return "\n".join(lines) if lines else "no device has Spotify open"


def _pages(api, path: str, params: dict) -> list:
    items, offset = [], 0
    while True:
        page = api.call("GET", path, {**params, "offset": offset}) or {}
        items += [i for i in page.get("items", []) if i]
        if not page.get("next") or len(items) >= 2000:
            return items
        offset += params["limit"]


def _playlist(api, name: str, create: bool) -> dict | None:
    wanted = name.casefold().strip()
    for playlist in _pages(api, "/me/playlists", {"limit": 50}):
        if playlist.get("name", "").casefold().strip() == wanted:
            return playlist
    if not create:
        return None
    return api.call("POST", "/me/playlists", body={"name": name, "public": False,
                                                    "description": "Songs Rafiq found for you"})


def _add(api, item: dict, name: str, finds: str = FINDS) -> str:
    playlist = _playlist(api, name, create=name.casefold() == finds.casefold())
    if not playlist:
        return f"error: no playlist called {name!r}"
    entries = _pages(api, f"/playlists/{playlist['id']}/items", {"limit": 100})
    if any((e.get("item") or e.get("track") or {}).get("uri") == item["uri"] for e in entries):
        return f"{_title(item)} is already in {playlist.get('name', name)}"
    api.call("POST", f"/playlists/{playlist['id']}/items", body={"uris": [item["uri"]]})
    return f"added {_title(item)} to {playlist.get('name', name)}"


@_said
def add_current(api, playlist: str = "") -> str:
    item = _playing(api)
    if not item:
        return "error: nothing is playing on Spotify"
    return _add(api, item, playlist or FINDS)


@_said
def save_found(api, title: str, artist: str = "") -> str:
    """Add a song Rafiq recognised to the "Rafiq finds" playlist, once."""
    query = f"track:{title}" + (f" artist:{artist}" if artist else "")
    items = [i for i in ((api.call("GET", "/search", {"q": query, "type": "track", "limit": 1}) or {})
                         .get("tracks", {}).get("items", [])) if i]
    if not items:
        return f"error: {title!r} is not on Spotify"
    return _add(api, items[0], FINDS)


# ---------------------------------------------------------------------- the tools

def _api(ctx) -> Api:
    cfg = ctx.cfg if ctx is not None else config.load()
    return Api(client_id(cfg), folder() / "token.json")


@tool("spotify_play", "Play music on Spotify: a song, a playlist (good for a genre or a mood, e.g. 'jazz'), "
      "an album or an artist, optionally on another device such as a speaker in another room. With no query, "
      "resume.",
      {"query": {"type": "string", "description": "what to play, e.g. 'so what miles davis' or 'jazz'"},
       "kind": {"type": "string", "enum": list(KINDS), "description": "default track; playlist for a genre"},
       "device": {"type": "string", "description": "name of the device to play on (see spotify_devices)"}})
def spotify_play(query: str = "", kind: str = "track", device: str = "", ctx=None) -> str:
    return play(_api(ctx), query, kind, device)


@tool("spotify_control", "Pause, resume, skip to the next song or go back to the previous one on Spotify.",
      {"action": {"type": "string", "enum": list(CONTROLS)}}, required=("action",))
def spotify_control(action: str, ctx=None) -> str:
    return control(_api(ctx), action)


@tool("spotify_now_playing", "The song playing on Spotify now.")
def spotify_now_playing(ctx=None) -> str:
    return now_playing(_api(ctx))


@tool("spotify_devices", "The devices with Spotify open (computers, phones, speakers in other rooms).")
def spotify_devices(ctx=None) -> str:
    return devices(_api(ctx))


@tool("spotify_add_to_playlist", f"Add the song playing on Spotify now to one of the user's playlists; "
      f"default '{FINDS}'. Rafiq asks the user first.",
      {"playlist": {"type": "string", "description": f"playlist name; empty for '{FINDS}'"}},
      confirm=lambda a: f"add the song playing now to your playlist '{a.get('playlist') or FINDS}'")
def spotify_add_to_playlist(playlist: str = "", ctx=None) -> str:
    return add_current(_api(ctx), playlist)
