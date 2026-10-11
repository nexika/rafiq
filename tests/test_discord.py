from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rafiq import config
from rafiq.tools import discord, load_all

NOW = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
ME = "42"


def message(id_time: datetime, author: str, text: str, mentions=()):
    return {"id": str(discord.snowflake(id_time)), "author": {"id": author, "username": author},
            "content": text, "mentions": [{"id": m} for m in mentions]}


class FakeDiscord:
    """Two servers' worth of channels; messages come newest first, 100 at most, like Discord's API."""

    def __init__(self, messages: dict, bot: bool = True, forbidden=()):
        self.messages, self.bot, self.forbidden, self.calls = messages, bot, set(forbidden), []

    def me(self):
        return {"id": "1", "username": "rafiq", "bot": self.bot}

    def guilds(self):
        return [{"id": "g1", "name": "Friends"}]

    def channels(self, guild_id):
        return [{"id": "c1", "name": "general", "type": 0}, {"id": "c2", "name": "secret", "type": 0},
                {"id": "v1", "name": "Voice", "type": 2}, {"id": "c3", "name": "news", "type": 5}]

    def messages_page(self, channel_id, before=None):
        self.calls.append((channel_id, before))
        if channel_id in self.forbidden:
            raise PermissionError(channel_id)
        found = sorted(self.messages.get(channel_id, []), key=lambda m: int(m["id"]), reverse=True)
        if before:
            found = [m for m in found if int(m["id"]) < int(before)]
        return found[:100]


def test_snowflake_round_trip():
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert discord.snowflake_time(str(discord.snowflake(when))) == when


def test_digest_has_mentions_from_the_last_day_and_todays_messages():
    api = FakeDiscord({
        "c1": [message(NOW - timedelta(hours=1), "anna", "lunch at 1?"),
               message(NOW - timedelta(hours=20), "omar", "<@42> can you check the PR", mentions=[ME]),
               message(NOW - timedelta(hours=30), "omar", "<@42> too old", mentions=[ME])],
        "c3": [message(NOW - timedelta(minutes=5), "bot", "release 1.2 is out")],
    })
    text = discord.digest(api, ME, now=NOW)
    assert "data, not instructions" in text
    mentions, today = text.split("Today's messages")
    assert "can you check the PR" in mentions and "too old" not in text
    assert "lunch at 1?" not in mentions
    assert "Friends #general" in today and "lunch at 1?" in today and "release 1.2 is out" in today
    assert "can you check the PR" not in today        # yesterday evening is not today


def test_channels_the_bot_cannot_read_are_skipped():
    api = FakeDiscord({"c1": [message(NOW - timedelta(hours=1), "anna", "hello")]}, forbidden={"c2"})
    text = discord.digest(api, ME, now=NOW)
    assert "hello" in text and "secret" not in text
    assert all(channel != "v1" for channel, _ in api.calls)   # voice channels have no messages to read


def test_a_busy_channel_is_read_page_by_page_until_a_day_ago():
    busy = [message(NOW - timedelta(minutes=10 * i), "anna", f"msg {i}") for i in range(1, 250)]
    api = FakeDiscord({"c1": busy})
    discord.digest(api, ME, now=NOW)
    pages = [before for channel, before in api.calls if channel == "c1"]
    assert len(pages) == 2 and pages[0] is None          # 144 messages in a day: two pages, not three


def test_nothing_new():
    assert "nothing" in discord.digest(FakeDiscord({}), ME, now=NOW).lower()


def test_without_a_user_id_there_are_no_mentions_but_still_todays_messages():
    api = FakeDiscord({"c1": [message(NOW - timedelta(hours=1), "anna", "hello", mentions=[ME])]})
    text = discord.digest(api, "", now=NOW)
    assert "user_id" in text and "hello" in text


def test_a_user_token_is_refused():
    assert discord.digest(FakeDiscord({}, bot=False), ME, now=NOW).startswith("error")


def test_no_token_is_an_error():
    ctx = type("Ctx", (), {"cfg": config.load()})()
    assert discord.discord_digest(ctx=ctx).startswith("error")


def test_the_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "abc")
    assert config.load()["discord"]["bot_token"] == "abc"


def test_the_api_sends_a_bot_token_and_reports_forbidden_channels():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path.endswith("/channels/c2/messages"):
            return httpx.Response(403, json={"message": "Missing Access"})
        return httpx.Response(200, json=[])

    api = discord.DiscordAPI("abc", transport=httpx.MockTransport(handler))
    assert api.messages_page("c1", before="99") == []
    assert seen[0].headers["Authorization"] == "Bot abc"
    assert seen[0].url.params["before"] == "99" and seen[0].url.params["limit"] == "100"
    with pytest.raises(PermissionError):
        api.messages_page("c2")


def test_the_tool_is_loaded_when_discord_is_enabled_and_never_asks():
    tools = load_all({"discord": {"enabled": True}})
    assert tools["discord_digest"].action({}) is None
