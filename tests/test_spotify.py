import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from rafiq.tools import REGISTRY, load_all, spotify

TRACK = {"name": "So What", "uri": "spotify:track:sowhat", "artists": [{"name": "Miles Davis"}]}


class FakeApi:
    """Answers Spotify Web API calls from a table: (method, path) -> answer, or a SpotifyError to raise."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path, params or {}, body))
        answer = self.routes.get((method, path))
        if isinstance(answer, Exception):
            raise answer
        return answer() if callable(answer) else answer

    def sent(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


def test_spotify_tools_load_only_when_enabled():
    assert "spotify_play" not in load_all({})
    tools = load_all({"spotify": {"enabled": True}})
    for name in ("spotify_play", "spotify_control", "spotify_now_playing", "spotify_devices",
                 "spotify_add_to_playlist"):
        assert tools[name].description, name
    assert "spotify_play" not in load_all({}) and "spotify_play" in REGISTRY
    assert tools["spotify_play"].action({"query": "jazz"}) is None
    assert "Rafiq finds" in tools["spotify_add_to_playlist"].action({})
    assert "Road trip" in tools["spotify_add_to_playlist"].action({"playlist": "Road trip"})


def test_play_searches_and_starts_the_first_track():
    api = FakeApi({("GET", "/search"): {"tracks": {"items": [TRACK]}}})
    assert spotify.play(api, "so what miles davis") == "playing So What by Miles Davis"
    assert api.sent("GET", "/search")[0][2] == {"q": "so what miles davis", "type": "track", "limit": 1}
    assert api.sent("PUT", "/me/player/play")[0][3] == {"uris": ["spotify:track:sowhat"]}


def test_play_a_genre_starts_a_playlist():
    playlist = {"name": "Jazz Classics", "uri": "spotify:playlist:jazz", "owner": {"display_name": "Spotify"}}
    api = FakeApi({("GET", "/search"): {"playlists": {"items": [None, playlist]}}})
    assert spotify.play(api, "jazz", kind="playlist") == "playing the playlist Jazz Classics"
    assert api.sent("PUT", "/me/player/play")[0][3] == {"context_uri": "spotify:playlist:jazz"}


def test_play_nothing_found():
    api = FakeApi({("GET", "/search"): {"tracks": {"items": []}}})
    assert spotify.play(api, "zzzz").startswith("error: nothing on Spotify")
    assert not api.sent("PUT", "/me/player/play")


def test_play_without_a_query_resumes():
    api = FakeApi()
    assert spotify.play(api) == "resumed"
    assert api.sent("PUT", "/me/player/play")[0][3] is None


def test_play_on_a_named_device_in_another_room():
    api = FakeApi({("GET", "/me/player/devices"): {"devices": [
        {"id": "pc", "name": "Desktop", "type": "Computer", "is_active": True},
        {"id": "kit", "name": "Kitchen speaker", "type": "Speaker", "is_active": False}]},
        ("GET", "/search"): {"tracks": {"items": [TRACK]}}})
    assert "Kitchen speaker" in spotify.play(api, "so what", device="kitchen")
    assert api.sent("PUT", "/me/player/play")[0][2] == {"device_id": "kit"}


def test_play_on_an_unknown_device_names_the_ones_there_are():
    api = FakeApi({("GET", "/me/player/devices"): {"devices": [{"id": "pc", "name": "Desktop"}]}})
    text = spotify.play(api, "so what", device="garage")
    assert text.startswith("error") and "Desktop" in text


@pytest.mark.parametrize("action, method, path, said", [
    ("pause", "PUT", "/me/player/pause", "paused"),
    ("resume", "PUT", "/me/player/play", "resumed"),
    ("next", "POST", "/me/player/next", "skipped to the next song"),
    ("previous", "POST", "/me/player/previous", "back to the previous song"),
])
def test_control(action, method, path, said):
    api = FakeApi()
    assert spotify.control(api, action) == said
    assert api.sent(method, path)


def test_control_unknown_action():
    assert spotify.control(FakeApi(), "explode").startswith("error")


def test_errors_are_said_plainly():
    api = FakeApi({("POST", "/me/player/next"): spotify.SpotifyError(404, "NO_ACTIVE_DEVICE")})
    assert "open Spotify" in spotify.control(api, "next")
    api = FakeApi({("PUT", "/me/player/pause"): spotify.SpotifyError(403, "PREMIUM_REQUIRED")})
    assert "Premium" in spotify.control(api, "pause")


def test_now_playing():
    api = FakeApi({("GET", "/me/player/currently-playing"): {"is_playing": True, "item": TRACK}})
    assert spotify.now_playing(api) == "playing now: So What by Miles Davis"
    assert spotify.now_playing(FakeApi()) == "nothing is playing"


def test_devices():
    api = FakeApi({("GET", "/me/player/devices"): {"devices": [
        {"id": "pc", "name": "Desktop", "type": "Computer", "is_active": True}]}})
    assert spotify.devices(api) == "- Desktop (Computer, playing here)"
    assert spotify.devices(FakeApi({("GET", "/me/player/devices"): {"devices": []}})).startswith("no device")


def playlists_api(existing=(), items=()):
    created = {"id": "new", "name": "Rafiq finds"}
    return FakeApi({
        ("GET", "/me/playlists"): {"items": list(existing), "next": None},
        ("POST", "/me/playlists"): created,
        ("GET", "/search"): {"tracks": {"items": [TRACK]}},
        ("GET", "/me/player/currently-playing"): {"is_playing": True, "item": TRACK},
        ("GET", "/playlists/old/items"): {"items": [{"item": {"uri": u}} for u in items], "next": None},
        ("GET", "/playlists/new/items"): {"items": [], "next": None},
    })


def test_add_the_song_playing_now_to_a_named_playlist():
    api = playlists_api(existing=[{"id": "old", "name": "Road Trip"}])
    assert spotify.add_current(api, "road trip") == "added So What by Miles Davis to Road Trip"
    assert api.sent("POST", "/playlists/old/items")[0][3] == {"uris": ["spotify:track:sowhat"]}
    assert not api.sent("POST", "/me/playlists")


def test_add_to_the_finds_playlist_creates_it_once():
    api = playlists_api()
    assert spotify.add_current(api, "") == "added So What by Miles Davis to Rafiq finds"
    assert api.sent("POST", "/me/playlists")[0][3]["name"] == "Rafiq finds"
    assert api.sent("POST", "/playlists/new/items")


def test_add_to_a_playlist_that_does_not_exist_is_an_error():
    api = playlists_api()
    assert spotify.add_current(api, "Gym").startswith("error: no playlist called 'Gym'")
    assert not api.sent("POST", "/me/playlists")


def test_add_when_nothing_plays():
    assert spotify.add_current(FakeApi(), "").startswith("error: nothing is playing")


def test_a_recognised_song_is_saved_to_the_finds_playlist():
    api = playlists_api(existing=[{"id": "old", "name": "Rafiq finds"}])
    assert spotify.save_found(api, "So What", "Miles Davis") == "added So What by Miles Davis to Rafiq finds"
    assert api.sent("GET", "/search")[0][2]["q"] == "track:So What artist:Miles Davis"


def test_a_song_already_in_the_finds_playlist_is_not_added_twice():
    api = playlists_api(existing=[{"id": "old", "name": "Rafiq finds"}], items=["spotify:track:sowhat"])
    assert "already" in spotify.save_found(api, "So What", "Miles Davis")
    assert not api.sent("POST", "/playlists/old/items")


def test_pkce_challenge_matches_the_verifier():
    verifier, challenge = spotify.pkce_pair()
    assert 43 <= len(verifier) <= 128
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == digest


def test_authorize_url_asks_for_playback_and_playlists():
    url = spotify.authorize_url("abc", "http://127.0.0.1:8888/callback", "chal", "st")
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["abc"] and query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["st"] and "user-modify-playback-state" in query["scope"][0]
    assert "playlist-modify-private" in query["scope"][0]


def test_api_refreshes_an_expired_token_and_keeps_it(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({"access_token": "old", "refresh_token": "r1", "expires_at": 0}))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "accounts.spotify.com":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["r1"]
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600, "refresh_token": "r2"})
        assert request.headers["Authorization"] == "Bearer fresh"
        return httpx.Response(200, json={"devices": []})

    api = spotify.Api("abc", token_path, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.call("GET", "/me/player/devices") == {"devices": []}
    saved = json.loads(token_path.read_text())
    assert saved["access_token"] == "fresh" and saved["refresh_token"] == "r2"
    assert token_path.stat().st_mode & 0o077 == 0


def test_api_turns_http_errors_into_spotify_errors(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({"access_token": "t", "refresh_token": "r", "expires_at": 9e12}))

    def handler(request):
        if request.url.path.endswith("/currently-playing"):
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"status": 404, "message": "Player command failed",
                                                   "reason": "NO_ACTIVE_DEVICE"}})

    api = spotify.Api("abc", token_path, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.call("GET", "/me/player/currently-playing") is None
    with pytest.raises(spotify.SpotifyError) as err:
        api.call("POST", "/me/player/next")
    assert err.value.status == 404 and err.value.reason == "NO_ACTIVE_DEVICE"


def test_api_without_a_token_says_to_run_setup(tmp_path):
    with pytest.raises(spotify.SpotifyError, match="spotify-setup"):
        spotify.Api("abc", tmp_path / "none.json").call("GET", "/me")
