import asyncio

import httpx
import microsoft_teams.apps
import pytest

from toucan_bot import cli, config

CREDS = config.Credentials("11111111-2222-3333-4444-555555555555", "secret")


def fake_app(monkeypatch, statuses):
    """App whose send answers with the given HTTP statuses in turn; 201 succeeds."""
    calls = []

    class App:
        def __init__(self, **kw):
            pass

        async def initialize(self):
            pass

        async def send(self, conversation_id, text, *, service_url):
            status = statuses[len(calls)]
            calls.append(status)
            if status != 201:
                request = httpx.Request("POST", service_url)
                raise httpx.HTTPStatusError("failed", request=request, response=httpx.Response(status, request=request))
            return type("Sent", (), {"id": "activity-1"})()

    async def no_sleep(_):
        pass

    monkeypatch.setattr(microsoft_teams.apps, "App", App)
    monkeypatch.setattr(cli.asyncio, "sleep", no_sleep)
    return calls


def send():
    return asyncio.run(cli._send(CREDS, "19:chat", "https://smba.example", "hello"))


def test_send_retries_a_401_until_teams_accepts(monkeypatch):
    calls = fake_app(monkeypatch, [401, 401, 201])
    assert send() == "activity-1"
    assert calls == [401, 401, 201]


def test_send_gives_up_after_the_last_401(monkeypatch):
    calls = fake_app(monkeypatch, [401] * 4)
    with pytest.raises(httpx.HTTPStatusError):
        send()
    assert len(calls) == len(cli.RETRY_401_DELAYS) + 1


def test_send_does_not_retry_other_errors(monkeypatch):
    calls = fake_app(monkeypatch, [403])
    with pytest.raises(httpx.HTTPStatusError):
        send()
    assert calls == [403]


@pytest.fixture
def send_environment(tmp_path, monkeypatch):
    from toucan_bot import store
    monkeypatch.setenv('TOUCAN_BOT_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('TOUCAN_CONFIG', str(tmp_path / 'absent.toml'))
    monkeypatch.setattr(config, 'load_credentials', lambda: CREDS)
    with store.connect(config.db_path()) as con:
        store.upsert_conversation(con, id='chat', type='groupChat', name='Project A',
                                  service_url='https://smba.example.com', tenant_id=None)


@pytest.mark.parametrize('failure,expected', [
    ('read-timeout', 4), ('write-error', 4), ('disconnect', 4), ('empty-503', 4),
    ('connect-error', 1), ('auth-timeout', 1), ('initialize-timeout', 1), ('forbidden', 1),
])
def test_cli_send_reports_whether_delivery_is_uncertain(send_environment, monkeypatch, failure, expected):
    request = httpx.Request('POST', 'https://login.microsoftonline.com/token' if failure == 'auth-timeout'
                            else 'https://smba.example.com/v3/conversations/chat/activities')
    error = {
        'read-timeout': httpx.ReadTimeout('response lost', request=request),
        'write-error': httpx.WriteError('connection lost', request=request),
        'disconnect': httpx.RemoteProtocolError('response lost', request=request),
        'connect-error': httpx.ConnectError('cannot connect', request=request),
        'auth-timeout': httpx.ReadTimeout('token response lost', request=request),
        'initialize-timeout': httpx.ReadTimeout('initialization failed', request=request),
        'empty-503': httpx.HTTPStatusError('unavailable', request=request, response=httpx.Response(503, request=request)),
        'forbidden': httpx.HTTPStatusError('forbidden', request=request, response=httpx.Response(403, request=request)),
    }[failure]
    calls = []

    class App:
        def __init__(self, **kwargs):
            pass

        async def initialize(self):
            if failure == 'initialize-timeout':
                raise error

        async def send(self, *args, **kwargs):
            calls.append(args)
            raise error

    monkeypatch.setattr(microsoft_teams.apps, 'App', App)
    assert cli.main(['send', '--conversation', 'chat', '--text', 'hello']) == expected
    assert len(calls) == (0 if failure == 'initialize-timeout' else 1)


@pytest.mark.parametrize('argv', [
    ['serve'], ['status'], ['setup-url'], ['send', '--conversation', 'chat'],
    ['delete', '--conversation', 'chat', '--activity', 'm1'],
    ['react', '--conversation', 'chat', '--message', 'm1', '--emoji', 'eyes'],
    ['conversations'], ['messages'], ['app-type', 'SingleTenant'], ['reset'],
])
@pytest.mark.parametrize('global_json', [False, True])
def test_every_subcommand_accepts_json(monkeypatch, capsys, argv, global_json):
    import json
    def command(args):
        print(json.dumps({'json': args.json}))
        return 0
    monkeypatch.setattr(cli, 'cmd_' + argv[0].replace('-', '_'), command)
    args = ['--json', *argv] if global_json else [*argv, '--json']
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out) == {'json': True}


def test_accepted_send_with_failed_local_record_is_uncertain(send_environment, monkeypatch):
    import sqlite3
    from toucan_bot import store
    calls = fake_app(monkeypatch, [201])

    def cannot_record(*args, **kwargs):
        raise sqlite3.OperationalError('disk full')

    monkeypatch.setattr(store, 'record_sent', cannot_record)
    assert cli.main(['send', '--conversation', 'chat', '--text', 'hello']) == 4
    assert calls == [201]
