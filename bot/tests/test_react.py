import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import microsoft_teams.apps
import pytest
from microsoft_teams.api import ApiClient, MessageReactionActivity
from microsoft_teams.common.http import Client

from toucan_bot import cli, config, names, server, store


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("TOUCAN_BOT_TENANT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("TOUCAN_BOT_PUBLIC_URL", "https://bot.example.com")
    monkeypatch.setenv("TOUCAN_BOT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TOUCAN_BOT_STATE_DIR", str(tmp_path / "state"))
    config.save_credentials(config.Credentials("11111111-2222-3333-4444-555555555555", "secret"))
    con = store.connect(config.db_path())
    store.upsert_conversation(con, id="19:chat", type="groupChat", name="Alice", service_url="https://smba.example/region",
                              tenant_id=config.load()["tenant_id"])
    con.close()
    monkeypatch.setattr(names, "load_chats", lambda: {"19:chat": {"name": "Alice", "type": "oneOnOne"}})


def responses(monkeypatch, replies):
    """Use the real SDK with a fake HTTP transport and record retry delays."""
    requests, sleeps = [], []

    def handler(request):
        status, headers = replies[len(requests)]
        requests.append(request)
        return httpx.Response(status, headers=headers)

    class App:
        def __init__(self, **kw):
            http = Client(_http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            self.api = ApiClient("https://wrong.example", http)

        async def initialize(self):
            pass

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(microsoft_teams.apps, "App", App)
    monkeypatch.setattr(cli.asyncio, "sleep", sleep)
    return requests, sleeps


@pytest.mark.parametrize("emoji,expected", [
    ("eyes", "1f440_eyes"),
    ("check", "2705_whiteheavycheckmark"),
    ("done", "2705_whiteheavycheckmark"),
    ("pin", "1f4cc_pushpin"),
    ("thumbsup", "like"),
    ("heart", "heart"),
    ("1f389_partypopper", "1f389_partypopper"),
])
def test_react_resolves_person_and_sends_teams_ids(bot, monkeypatch, capsys, emoji, expected):
    requests, sleeps = responses(monkeypatch, [(204, {})])
    assert cli.main(["--json", "react", "--to", "Alice", "--message", "1730", "--emoji", emoji]) == 0
    assert [(r.method, str(r.url)) for r in requests] == [
        ("PUT", f"https://smba.example/region/v3/conversations/19:chat/activities/1730/reactions/{expected}")]
    result = json.loads(capsys.readouterr().out)
    assert result == {"conversation_id": "19:chat", "to": "Alice", "activity_id": "1730", "emoji": expected,
                      "removed": False, "dry_run": False}
    assert sleeps == []


def test_remove_honours_retry_after_before_retrying(bot, monkeypatch, capsys):
    requests, sleeps = responses(monkeypatch, [(429, {"Retry-After": "2"}), (429, {"Retry-After": "5"}), (204, {})])
    assert cli.main(["react", "--conversation", "19:chat", "--message", "1730", "--emoji", "check", "--remove"]) == 0
    assert sleeps == [2, 5]
    assert [(r.method, str(r.url)) for r in requests] == [
        ("DELETE", "https://smba.example/region/v3/conversations/19:chat/activities/1730/reactions/2705_whiteheavycheckmark")] * 3
    assert "removed 2705_whiteheavycheckmark from message 1730 in Alice" in capsys.readouterr().out


def test_react_reuses_401_retry_and_stops_after_bounded_429s(bot, monkeypatch, capsys):
    requests, sleeps = responses(monkeypatch, [(429, {"Retry-After": "2"}), (401, {}),
                                               (429, {"Retry-After": "4"}), (429, {"Retry-After": "6"}), (429, {})])
    assert cli.main(["react", "--conversation", "19:chat", "--message", "1730", "--emoji", "eyes"]) == 1
    assert sleeps == [2, 3, 4, 6]
    assert len(requests) == 5
    assert "HTTP 429" in capsys.readouterr().err


@pytest.mark.parametrize("status,error", [(403, "bot cannot access"), (404, "no longer exists")])
def test_permanent_failure_is_reported_without_retry(bot, monkeypatch, capsys, status, error):
    requests, sleeps = responses(monkeypatch, [(status, {})])
    assert cli.main(["react", "--conversation", "19:chat", "--message", "1730", "--emoji", "eyes"]) == 1
    assert len(requests) == 1 and sleeps == []
    assert error in capsys.readouterr().err


def test_dry_run_resolves_and_prints_target_without_http(bot, monkeypatch, capsys):
    requests, sleeps = responses(monkeypatch, [])
    assert cli.main(["--json", "react", "--to", "Alice", "--message", "1730", "--emoji", "pin", "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] and result["conversation_id"] == "19:chat" and result["activity_id"] == "1730"
    assert result["emoji"] == "1f4cc_pushpin"
    assert requests == [] and sleeps == []


def test_incoming_reaction_is_logged_without_acting_and_respects_tenant(bot, caplog):
    app, _ = server.build()

    async def receive():
        for tenant in (config.load()["tenant_id"], "another-tenant"):
            activity = MessageReactionActivity.model_validate({
                "id": "reaction-1", "type": "messageReaction", "serviceUrl": "https://smba.example/region",
                "from": {"id": "person-1"}, "recipient": {"id": "bot-1"},
                "conversation": {"id": "19:chat", "tenantId": tenant},
                "replyToId": "1730", "reactionsAdded": [{"type": "like"}],
            })
            for handler in app.router.select_handlers(activity):
                await handler(SimpleNamespace(activity=activity))

    with caplog.at_level(logging.INFO, logger="toucan_bot"):
        asyncio.run(receive())
    assert [r.getMessage() for r in caplog.records if r.name == "toucan_bot"] == [
        "message reaction in 19:chat on 1730 from person-1: ['like']",
        "ignored activity from a different tenant"]
