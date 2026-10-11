import json
from datetime import UTC, date, datetime, timedelta

import pytest

from rafiq import config
from rafiq.tools import instagram, load_all

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


class FakeGraph:
    """Answers the Graph API calls the tool makes, from a table, and records them."""

    def __init__(self, followers=1000, media=None, views=None):
        self.followers = followers
        self.media = media or []
        self.views = views or {}
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, params))
        path = url.removeprefix(instagram.GRAPH + "/")
        if path == "oauth/access_token":
            return {"access_token": "long-" + params["fb_exchange_token"], "expires_in": 60 * 86400}
        if path == "me/accounts":
            return {"data": [{"name": "Page", "instagram_business_account": {"id": "111", "username": "rafiq"}},
                             {"name": "Page without Instagram"}]}
        if path.endswith("/insights"):
            media_id = path.split("/")[0]
            if media_id not in self.views:
                raise instagram.GraphError("no insights for this media")
            return {"data": [{"name": "views", "values": [{"value": self.views[media_id]}]}]}
        if path.endswith("/media"):
            return {"data": self.media}
        return {"username": "rafiq", "followers_count": self.followers, "media_count": len(self.media)}


def save_token(expires_at=NOW + timedelta(days=50), accounts=None):
    instagram.save_token({"access_token": "tok", "expires_at": expires_at.timestamp(),
                          "accounts": accounts or [{"id": "111", "username": "rafiq"}]})


def video(id, caption, when="2026-10-01T10:00:00+0000"):  # noqa: A002
    return {"id": id, "caption": caption, "media_type": "VIDEO", "timestamp": when}


def test_instagram_tool_is_only_loaded_when_enabled():
    assert "instagram_stats" not in load_all({})
    tools = load_all({"instagram": {"enabled": True}})
    assert tools["instagram_stats"].action({}) is None
    assert tools["instagram_stats"].schema()["type"] == "object"


def test_first_look_saves_a_snapshot_and_says_comparisons_start_tomorrow():
    save_token()
    graph = FakeGraph(followers=1200, media=[video("m1", "Cairo at night")], views={"m1": 500})
    text = instagram.instagram_stats(get=graph, now=NOW)
    assert "@rafiq: 1,200 followers" in text
    assert "first snapshot" in text
    assert "Cairo at night" in text and "500 views" in text
    snaps = instagram.load_snapshots()
    assert snaps["rafiq"]["2026-10-10"]["followers"] == 1200
    assert snaps["rafiq"]["2026-10-10"]["media"]["m1"]["views"] == 500


def test_compares_with_the_last_snapshot_before_today():
    save_token()
    instagram.instagram_stats(get=FakeGraph(followers=1000, media=[video("m1", "Old reel")], views={"m1": 900}),
                              now=NOW - timedelta(days=1))
    graph = FakeGraph(followers=988, views={"m1": 1500, "m2": 300},
                      media=[video("m2", "Brand new reel", "2026-10-10T07:00:00+0000"), video("m1", "Old reel")])
    text = instagram.instagram_stats(get=graph, now=NOW)
    assert "988 followers (12 lost since 2026-10-09)" in text
    assert "1 new post since 2026-10-09" in text and "Brand new reel" in text
    assert "Old reel: 1,500 views (+600)" in text


def test_gained_followers_are_counted_as_gained():
    save_token()
    instagram.instagram_stats(get=FakeGraph(followers=1000), now=NOW - timedelta(days=3))
    text = instagram.instagram_stats(get=FakeGraph(followers=1040), now=NOW)
    assert "1,040 followers (40 gained since 2026-10-07)" in text


def test_several_accounts_and_one_by_name():
    save_token(accounts=[{"id": "111", "username": "rafiq"}, {"id": "222", "username": "studio"}])
    graph = FakeGraph()
    text = instagram.instagram_stats(get=graph, now=NOW)
    assert "@rafiq" in text and "@studio" in text
    only = instagram.instagram_stats(account="@Studio", get=FakeGraph(), now=NOW)
    assert "@studio" in only and "@rafiq" not in only
    assert instagram.instagram_stats(account="nobody", get=FakeGraph(), now=NOW).startswith("error")


def test_config_limits_the_accounts():
    save_token(accounts=[{"id": "111", "username": "rafiq"}, {"id": "222", "username": "studio"}])
    ctx = type("Ctx", (), {"cfg": {"instagram": {"enabled": True, "accounts": ["studio"]}}})()
    text = instagram.instagram_stats(get=FakeGraph(), now=NOW, ctx=ctx)
    assert "@studio" in text and "@rafiq" not in text


def test_media_without_views_still_lists():
    save_token()
    graph = FakeGraph(media=[{"id": "p1", "caption": "A photo", "media_type": "IMAGE",
                              "timestamp": "2026-10-10T08:00:00+0000"}])
    text = instagram.instagram_stats(get=graph, now=NOW)
    assert "1,000 followers" in text


def test_a_failing_account_does_not_hide_the_others():
    save_token(accounts=[{"id": "111", "username": "rafiq"}, {"id": "999", "username": "broken"}])

    def get(url, params):
        if "/999" in url:
            raise instagram.GraphError("permissions")
        return FakeGraph()(url, params)

    text = instagram.instagram_stats(get=get, now=NOW)
    assert "@rafiq: 1,000 followers" in text and "@broken: error" in text


def test_not_set_up_is_an_error():
    assert instagram.instagram_stats(get=FakeGraph(), now=NOW).startswith("error")


def test_token_close_to_expiry_is_refreshed(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    save_token(expires_at=NOW + timedelta(days=3))
    graph = FakeGraph()
    instagram.instagram_stats(get=graph, now=NOW)
    exchange = [p for u, p in graph.calls if u.endswith("oauth/access_token")]
    assert exchange and exchange[0]["fb_exchange_token"] == "tok"
    saved = instagram.load_token()
    assert saved["access_token"] == "long-tok"
    assert saved["expires_at"] == pytest.approx((NOW + timedelta(days=60)).timestamp())
    assert all(p.get("access_token") == "long-tok" for u, p in graph.calls if "oauth" not in u)


def test_fresh_token_is_not_refreshed(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    save_token()
    graph = FakeGraph()
    instagram.instagram_stats(get=graph, now=NOW)
    assert not [u for u, _ in graph.calls if u.endswith("oauth/access_token")]


def test_setup_exchanges_the_token_and_finds_the_linked_accounts(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    message = instagram.setup("short", get=FakeGraph(), now=NOW)
    assert "@rafiq" in message
    token = instagram.load_token()
    assert token["access_token"] == "long-short"
    assert token["accounts"] == [{"id": "111", "username": "rafiq"}]
    path = config.home() / "instagram" / "token.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "long-short" not in message


def test_setup_needs_the_app_id_and_secret():
    assert "INSTAGRAM_APP_ID" in instagram.setup("short", get=FakeGraph(), now=NOW)
    assert instagram.load_token() is None


def test_old_snapshots_are_pruned():
    save_token()
    old = (NOW.date() - timedelta(days=instagram.KEEP_DAYS + 5)).isoformat()
    path = config.data_dir() / "instagram.json"
    path.write_text(json.dumps({"rafiq": {old: {"followers": 1, "media_count": 0, "media": {}}}}))
    instagram.instagram_stats(get=FakeGraph(), now=NOW)
    assert list(instagram.load_snapshots()["rafiq"]) == [date(2026, 10, 10).isoformat()]
