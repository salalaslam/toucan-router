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


def capture_sends(monkeypatch):
    sent = []

    class App:
        def __init__(self, **kwargs):
            pass

        async def initialize(self):
            pass

        async def send(self, conversation_id, activity, *, service_url):
            sent.append(activity)
            return type('Sent', (), {'id': 'activity-1'})()

    monkeypatch.setattr(microsoft_teams.apps, 'App', App)
    return sent


def test_cli_send_posts_a_resized_inline_image_with_caption(send_environment, monkeypatch, tmp_path):
    import base64, io
    from PIL import Image
    from toucan_bot import store
    path = tmp_path / 'screenshot.png'
    Image.new('RGB', (2400, 1200), 'navy').save(path)
    sent = capture_sends(monkeypatch)
    assert cli.main(['send', '--conversation', 'chat', '--image', str(path), '--text', 'The new page']) == 0
    body = sent[0].model_dump(by_alias=True, exclude_none=True)
    assert body['text'] == 'The new page'
    [attachment] = body['attachments']
    assert attachment['contentType'] == 'image/png'
    header, data = attachment['contentUrl'].split(',', 1)
    assert header == 'data:image/png;base64'
    assert Image.open(io.BytesIO(base64.b64decode(data))).size == (1024, 512)
    with store.connect(config.db_path()) as con:
        assert con.execute('SELECT text FROM sent').fetchone()[0] == 'The new page\n[image: screenshot.png]'


@pytest.mark.parametrize('kind', ['animated', 'not-an-image', 'missing'])
def test_cli_send_refuses_images_teams_cannot_show_before_sending(send_environment, monkeypatch, tmp_path, kind, capsys):
    from PIL import Image
    path = tmp_path / 'file.gif'
    if kind == 'animated':
        frames = [Image.new('RGB', (20, 20), c) for c in ('red', 'blue')]
        frames[0].save(path, save_all=True, append_images=frames[1:])
    elif kind == 'not-an-image':
        path.write_text('plain text')
    sent = capture_sends(monkeypatch)
    assert cli.main(['send', '--conversation', 'chat', '--image', str(path)]) == 2
    assert sent == []
    assert {'animated': 'animated', 'not-an-image': 'not a readable image', 'missing': 'does not exist'}[kind] in capsys.readouterr().err


def test_large_photos_are_reencoded_under_the_size_cap(tmp_path):
    import os
    from PIL import Image
    from toucan_bot import outbound
    path = tmp_path / 'photo.png'
    Image.frombytes('RGB', (2000, 1500), os.urandom(2000 * 1500 * 3)).save(path)
    image = outbound.prepare(path)
    assert image.content_type == 'image/jpeg' and image.name == 'photo.jpg'
    assert len(image.data) <= outbound.MAX_BYTES and max(image.width, image.height) <= outbound.MAX_SIDE
