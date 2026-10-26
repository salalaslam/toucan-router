import asyncio
import io
import json
import stat
import zipfile

import anyio
import httpx
import pytest

HTTP_CLIENT = httpx.AsyncClient

BOT_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TOUCAN_BOT_TENANT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("TOUCAN_BOT_PUBLIC_URL", "https://bot.example.com")
    monkeypatch.setenv("TOUCAN_BOT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TOUCAN_BOT_STATE_DIR", str(tmp_path / "state"))
    from toucan_bot import config, server

    async def inline(func, *args, **kwargs):
        return func(*args)

    # The sandbox cannot wake worker threads; exercise ASGI and file responses in this loop.
    monkeypatch.setattr(anyio.to_thread, "run_sync", inline)
    # A successful setup schedules SIGTERM so systemd restarts the bot; never let it reach pytest.
    monkeypatch.setattr(server.os, "kill", lambda *a: None)
    return config, server


def entra(monkeypatch, server, status, body):
    """Stand in for Microsoft's token endpoint; the code under test still builds and sends the request."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def client(server, creds=None):
    app, web = server.build(creds)
    asyncio.run(app.initialize())
    class Client:
        def request(self, method, url, **kwargs):
            async def send():
                async with HTTP_CLIENT(transport=httpx.ASGITransport(app=web), base_url="https://bot.example.com") as http:
                    return await http.request(method, url, **kwargs)
            return asyncio.run(send())

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

    return Client()


def test_wrong_setup_token_is_not_found(env):
    config, server = env
    c = client(server)
    assert c.get("/setup/nope").status_code == 404
    assert c.post("/setup/nope", data={"client_id": BOT_ID, "client_secret": "s"}).status_code == 404
    assert c.get("/setup/nope/toucan-teams-app.zip").status_code == 404
    assert c.get(f"/setup/{config.setup_token()}").status_code == 200


def test_rejected_secret_is_not_saved(env, monkeypatch):
    config, server = env
    seen = entra(monkeypatch, server, 401, {"error": "invalid_client",
                                            "error_description": "AADSTS7000215: Invalid client secret provided.\nTrace ID: x"})
    c = client(server)
    r = c.post(f"/setup/{config.setup_token()}", data={"client_id": BOT_ID, "client_secret": "wrong"})
    assert r.status_code == 400
    assert "AADSTS7000215" in r.text and "Trace ID" not in r.text
    assert config.load()["tenant_id"] in str(seen[0].url)
    assert config.load_credentials() is None


def test_accepted_secret_is_saved_privately_and_locks_the_form(env, monkeypatch):
    config, server = env
    entra(monkeypatch, server, 200, {"access_token": "t", "token_type": "Bearer"})
    c = client(server)
    token = config.setup_token()
    r = c.post(f"/setup/{token}", data={"client_id": BOT_ID, "client_secret": "good"}, follow_redirects=False)
    assert r.status_code == 303

    creds_file = config.config_dir() / "credentials.json"
    assert stat.S_IMODE(creds_file.stat().st_mode) == 0o600
    assert config.load_credentials().client_id == BOT_ID

    z = zipfile.ZipFile(io.BytesIO(c.get(f"/setup/{token}/toucan-teams-app.zip").content))
    manifest = json.loads(z.read("manifest.json"))
    assert manifest["id"] == BOT_ID and manifest["bots"][0]["botId"] == BOT_ID
    assert {"color.png", "outline.png"} <= set(z.namelist())

    again = c.post(f"/setup/{token}", data={"client_id": BOT_ID, "client_secret": "other"})
    assert again.status_code == 409
    assert config.load_credentials().client_secret == "good"


def test_messaging_endpoint_rejects_everything_before_setup(env):
    _, server = env
    r = client(server).post("/api/messages", json={"type": "message", "text": "hi"})
    assert r.status_code == 401


def test_setup_token_never_reaches_uvicorn_access_log(env, monkeypatch, capsys):
    from uvicorn.protocols.http.h11_impl import H11Protocol
    from uvicorn.server import ServerState
    config, server = env
    servers, create = [], server.uvicorn.Server

    def capture(settings):
        instance = create(settings)
        servers.append(instance)
        return instance

    monkeypatch.setattr(server.uvicorn, "Server", capture)
    server.build()
    token = config.setup_token()

    class Transport(asyncio.Transport):
        def __init__(self):
            self.output = bytearray()

        def get_extra_info(self, name, default=None):
            return default

        def is_closing(self):
            return False

        def write(self, data):
            self.output.extend(data)

        def close(self):
            pass

        def resume_reading(self):
            pass

    async def request():
        state = ServerState()
        protocol = H11Protocol(servers[0].config, state, {})
        transport = Transport()
        protocol.connection_made(transport)
        protocol.data_received(f"GET /setup/{token} HTTP/1.1\r\nHost: bot.example.com\r\nConnection: close\r\n\r\n".encode())
        await asyncio.gather(*state.tasks)
        protocol.connection_lost(None)
        return transport.output

    assert b"200 OK" in asyncio.run(request())
    output = capsys.readouterr()
    assert token not in output.out + output.err


def test_service_can_start_with_no_existing_state_directory(env):
    import configparser
    from pathlib import Path
    config, server = env
    unit = configparser.ConfigParser(interpolation=None, strict=False)
    unit.read(Path(__file__).resolve().parents[2] / "contrib/systemd/toucan-bot.service")
    # systemd refuses a missing mandatory working directory before ExecStart can create it.
    directory = unit["Service"].get("WorkingDirectory", "")
    assert not directory or directory.startswith("-")
    assert not config.state_dir().exists()
    server.build()
    assert config.db_path().is_file()


def test_download_rebuilds_a_stale_manifest(env):
    from toucan_bot import package
    config, server = env
    config.save_credentials(config.Credentials(BOT_ID, 'secret'))
    stale = package.manifest(BOT_ID)
    stale.pop('authorization')
    config.state_dir().mkdir(parents=True)
    with zipfile.ZipFile(config.package_path(), 'w') as archive:
        archive.writestr('manifest.json', json.dumps(stale))
    response = client(server).get(f'/setup/{config.setup_token()}/toucan-teams-app.zip')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
    assert {'name': 'ChatMessage.Read.Chat', 'type': 'Application'} in manifest['authorization']['permissions']['resourceSpecific']


def test_privacy_and_setup_disclose_every_message(env):
    config, server = env
    config.config_path().parent.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text('org_name = "Example Org"\ncontact = "admin@example.com"\ncatchup_lookback_days = 23\n')
    c = client(server)
    for path in ('/privacy', f'/setup/{config.setup_token()}'):
        page = c.get(path).text
        assert 'every message in chats it has been added to' in page
        assert "people Toucan doesn't route" in page
        assert 'edits, deletions and restorations' in ' '.join(page.split())
        assert 'Microsoft Graph' in page and 'screenshots' in page
        assert '30 days' in page and "owner's own" in page
        assert 'cannot download' in ' '.join(page.split())
        assert 'sender name and Teams ID' in page
        assert 'Example Org' in page and 'admin@example.com' in page
        normalized = ' '.join(page.split())
        assert 'messages modified in the last 23 days' in normalized
        assert 'older messages' in normalized
