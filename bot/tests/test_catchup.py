import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from microsoft_teams.api import TeamsChannelAccount

from toucan_bot import catchup, cli, config, media, names, store

ALICE = '00000000-0000-0000-0000-000000000001'
OWNER = '00000000-0000-0000-0000-000000000009'


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv('TOUCAN_BOT_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('TOUCAN_BOT_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('TOUCAN_CONFIG', str(tmp_path / 'router.toml'))
    con = store.connect(config.db_path())
    store.upsert_conversation(con, id='chat', type='groupChat', name=None,
                              service_url='https://smba.trafficmanager.net/region', tenant_id=OWNER)
    yield con, config.load()
    con.close()


@pytest.fixture
def after_read(monkeypatch):
    """Commit through a second real SQLite connection after a selected read."""
    def install(matches, callback):
        connect = sqlite3.connect
        called = []
        class Cursor(sqlite3.Cursor):
            def finish(self, rows):
                if self.interleave and not called:
                    called.append(True)
                    callback()
                return rows
            def fetchone(self):
                return self.finish(super().fetchone())
            def fetchall(self):
                return self.finish(super().fetchall())
        class Connection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                cursor = self.cursor(factory=Cursor)
                cursor.interleave = matches(' '.join(sql.lower().split()))
                return cursor.execute(sql, parameters)
        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, **kw, factory=Connection))
        return called
    return install


def message(id='m1', **kw):
    return dict(id=id, createdDateTime='2026-10-24T10:00:00Z', lastModifiedDateTime='2026-10-24T10:00:00Z',
                body=dict(contentType='html', content='<p>Hello &amp; world</p>'),
                **{'from': dict(user=dict(id=ALICE, displayName='Alice Example'))}) | kw


def test_graph_pages_dedupe_push_and_enrich_files_cards_and_application_context(state):
    con, settings = state
    chat_id = '19:example@thread.v2'
    store.record_message(con, conversation_id=chat_id, id='m1', ts='2026-10-24T10:00:00+00:00',
                         sender_aad_id=ALICE, sender_name='Alice Example', text='Hello & world',
                         mentioned=False, reply_to_id=None)
    file = dict(id='file', contentType='reference', name='brief.pdf', contentUrl='https://example.com/brief.pdf')
    card = dict(contentType='application/vnd.microsoft.card.adaptive', content='{"body":[{"text":"Build passed"}]}')
    forwarded = dict(contentType='forwardedMessageReference', content='{"messageId":"original"}')
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers['Authorization'] == 'Bearer graph-test-token'
        if len(calls) == 1:
            query = parse_qs(request.url.query.decode())
            assert query['$orderby'] == ['lastModifiedDateTime desc'] and query['$top'] == ['50']
            return httpx.Response(200, json={'value': [message(attachments=[file, forwarded])],
                                  '@odata.nextLink': str(request.url).replace('%3A', ':').replace('%40', '@') + '&$skiptoken=second'})
        return httpx.Response(200, json={'value': [message('app-message', attachments=[card], **{
            'from': dict(application=dict(id='application', displayName='Build app'))})]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await catchup.sync_chat(con, client, 'graph-test-token', chat_id, settings, 'bot')
    asyncio.run(run())
    assert len(calls) == 2
    rows = {r['id']: dict(r) for r in con.execute('SELECT * FROM messages')}
    assert len(rows) == 2
    assert rows['m1']['first_source'] == 'live'
    assert rows['m1']['live_seen_at'] and rows['m1']['graph_seen_at']
    assert json.loads(rows['m1']['attachments_json']) == [file, forwarded]
    assert rows['app-message']['sender_aad_id'] is None
    assert rows['app-message']['sender_app_id'] == 'application'
    assert json.loads(rows['app-message']['attachments_json']) == [card]
    assert con.execute('SELECT cursor,last_success,last_error FROM catchup').fetchone()[:2] != (None, None)
    asyncio.run(run())
    app = con.execute("SELECT attachments_json FROM messages WHERE id='app-message'").fetchone()
    assert json.loads(app[0]) == [card]  # an ID-less card is not duplicated on replay


def test_failed_page_replays_without_advancing_cursor_and_other_chats_continue(state, monkeypatch):
    con, settings = state
    clock = [time.time()]
    monkeypatch.setattr(catchup, 'time', SimpleNamespace(time=lambda: clock[0]))
    async def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    for chat in ('denied', 'healthy'):
        store.upsert_conversation(con, id=chat, type='groupChat', name=None, service_url='', tenant_id=OWNER)
    store.upsert_conversation(con, id='personal', type='personal', name=None, service_url='', tenant_id=OWNER)
    store.catchup_result(con, 'chat', cursor='2026-10-24T09:00:00+00:00')
    token_requests, calls = [], []

    async def token(scope, tenant):
        token_requests.append((scope, tenant))
        return 'graph-test-token'

    def respond(request):
        calls.append(str(request.url))
        if '/denied/' in request.url.path:
            return httpx.Response(403)
        if '/chat/' in request.url.path:
            if '$skiptoken' in request.url.query.decode():
                return httpx.Response(503)
            return httpx.Response(200, json={'value': [message()], '@odata.nextLink': str(request.url) + '&$skiptoken=two'})
        return httpx.Response(200, json={'value': []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await catchup.sync(con, client, SimpleNamespace(id='bot', token_provider=SimpleNamespace(get_app_token=token)), settings)
    asyncio.run(run())
    assert token_requests == [('https://graph.microsoft.com/.default', settings['tenant_id'])]
    states = {r['conversation_id']: dict(r) for r in con.execute('SELECT * FROM catchup')}
    assert states['chat']['cursor'] == '2026-10-24T09:00:00+00:00'
    assert states['chat']['http_status'] == 503
    assert states['denied']['http_status'] == 403 and 'RSC' in states['denied']['last_error']
    assert states['healthy']['last_success'] and not any('/personal/' in url for url in calls)
    assert con.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
    asyncio.run(run())
    assert con.execute('SELECT count(*) FROM messages').fetchone()[0] == 1


@pytest.mark.parametrize('source', ['graph', 'live'])
@pytest.mark.parametrize('streamed', [False, True])
def test_image_cache_is_private_bounded_and_expires_without_downloading_files(state, source, streamed):
    con, settings = state
    settings['media_max_bytes'] = 4
    base = catchup.message_base('chat', 'm1')
    url = base + '/hostedContents/image/$value' if source == 'graph' else 'https://smba.trafficmanager.net/v3/attachments/image/views/original'
    attachments = [dict(contentType='image/png', contentUrl=url),
                   dict(contentType='reference', name='picture.png', contentUrl='https://example.com/picture.png')]
    requests = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'12'
            yield b'34'

    def respond(request):
        requests.append(request)
        if streamed:
            return httpx.Response(200, stream=Chunks(), headers={'Content-Type': 'image/png'})
        return httpx.Response(200, content=b'1234', headers={'Content-Type': 'image/png'})

    async def run(items):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return [await media.download(client, a, source + '-token', settings, source=source, key='chat/m1') for a in items]
    saved = asyncio.run(run(attachments))
    path = config.state_dir() / 'media' / os.path.basename(saved[0]['local_path'])
    assert path.read_bytes() == b'1234'
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert len(requests) == 1 and requests[0].headers['Authorization'] == 'Bearer ' + source + '-token'
    assert saved[1] == attachments[1]
    store.record_message(con, conversation_id='chat', **catchup.normalise(message(attachments=saved), 'bot'))
    os.utime(path, (time.time() - 31 * 86400,) * 2)
    media.prune(con, settings)
    assert not path.exists()
    expired = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
    assert expired['download_status'] == 'expired' and 'local_path' not in expired
    settings['media_max_bytes'] = 3
    refused = asyncio.run(run(attachments))[0]
    assert refused['download_status'] == 'unavailable' and 'cap' in refused['download_error']
    assert not list(path.parent.iterdir())


def test_hosted_image_403_does_not_fail_message_sync_or_leak_token_to_untrusted_url(state):
    con, settings = state
    base = catchup.message_base('19:example@thread.v2', 'm1').replace('%3A', ':').replace('%40', '@')
    raw = f'<blockquote>Quoted text</blockquote><img src="{base}/hostedContents/image/$value"><img src="https://example.com/steal">'
    calls = []

    def respond(request):
        calls.append(request)
        if '/hostedContents/' in request.url.path:
            return httpx.Response(403)
        return httpx.Response(200, json={'value': [message(body=dict(contentType='html', content=raw))]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await catchup.sync_chat(con, client, 'graph-token', '19:example@thread.v2', settings, 'bot')
            await media.retry_pending(con, client, 'graph-token', settings, source='graph')
    asyncio.run(run())
    row = con.execute('SELECT * FROM messages').fetchone()
    image = json.loads(row['attachments_json'])[0]
    assert image['download_status'] == 'unavailable' and image['http_status'] == 403
    assert row['text'].startswith('Quoted text') and row['raw_html'] == raw
    assert len(calls) == 2 and con.execute('SELECT last_error FROM catchup').fetchone()[0] is None


def test_graph_restore_and_stale_snapshots_preserve_latest_state(state):
    con, _ = state
    def save(**kw):
        store.record_message(con, conversation_id='chat', **catchup.normalise(message(**kw), 'bot'))
    save(deletedDateTime='2026-10-24T10:02:00Z', lastModifiedDateTime='2026-10-24T10:02:00Z')
    save(lastModifiedDateTime='2026-10-24T10:01:00Z')
    assert con.execute('SELECT deleted FROM messages').fetchone()[0] == 1
    save(lastModifiedDateTime='2026-10-24T10:04:00Z', body=dict(contentType='text', content='restored'))
    save(deletedDateTime='2026-10-24T10:02:00Z', lastModifiedDateTime='2026-10-24T10:02:00Z')
    row = con.execute('SELECT * FROM messages').fetchone()
    assert row['deleted'] == 0 and row['text'] == 'restored'
    assert row['ts'] == '2026-10-24T10:00:00Z'


def test_rosters_distinguish_human_1on1_unnamed_group_and_bot_personal_chat(state, monkeypatch):
    con, settings = state
    (config.config_dir()).mkdir()
    config.config_path().write_text(f'owner_teams_id = "{OWNER}"\n')
    people = {id: TeamsChannelAccount(id=id, aad_object_id=id, name=name)
              for id, name in [(OWNER, 'Owner Example'), (ALICE, 'Alice Example'),
                                ('00000000-0000-0000-0000-000000000002', 'Bob Example')]}
    class Members:
        def __init__(self, chat):
            self.chat = chat
        async def get_all(self):
            if self.chat == 'chat':
                return list(people.values())[:2]
            if self.chat == 'group':
                return list(people.values())
            return [people[ALICE]]
    app = SimpleNamespace(api=SimpleNamespace(from_service_url=lambda url: SimpleNamespace(
        conversations=SimpleNamespace(members=Members))))
    for id, kind in [('group', 'groupChat'), ('personal', 'personal')]:
        store.upsert_conversation(con, id=id, type=kind, name=None, service_url='https://example.com', tenant_id=OWNER)
    for chat in store.conversations(con):
        asyncio.run(catchup.roster(con, app, chat))
    chats = names.load_chats()
    assert names.resolve('Alice', list(chats), chats) == 'chat'
    with pytest.raises(names.NotFound):
        names.resolve('Alice Example, Bob Example', list(chats), chats)
    assert names.resolve('personal', list(chats), chats) == 'personal'
    store.upsert_conversation(con, id='other-1on1', type='groupChat', name=None, service_url='', tenant_id=OWNER)
    for person in list(people.values())[:2]:
        store.remember_member(con, 'other-1on1', person)
    con.execute("INSERT INTO meta VALUES ('roster:other-1on1','now')")
    con.commit()
    with pytest.raises(names.NotFound, match='several chats'):
        names.resolve('Alice', [*chats, 'other-1on1'], names.load_chats())


def test_status_reports_graph_only_delivery_and_per_chat_http_failure(state, monkeypatch, capsys):
    con, _ = state
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    store.record_message(con, conversation_id='chat', **catchup.normalise(message(createdDateTime=stamp, lastModifiedDateTime=stamp), 'bot'))
    con.execute("UPDATE messages SET graph_seen_at=datetime('now','-10 minutes')")
    con.commit()
    store.catchup_result(con, 'chat', error='HTTP 403: no RSC grant or unsupported chat type', status=403)
    assert cli.main(['status', '--json']) == 1  # not registered, still report health
    out = json.loads(capsys.readouterr().out)
    assert out['delivery']['ordinary_graph_only'] == 1
    assert out['catchup']['chats'][0]['http_status'] == 403
    assert out['catchup']['chats'][0]['last_success_age_seconds'] is None


def test_person_name_requires_confirmed_owner_pair_and_outranks_group_alias(state):
    con, _ = state
    config.config_dir().mkdir()
    config.config_path().write_text(f'owner_teams_id = "{OWNER}"\n')
    store.remember_member(con, 'chat', TeamsChannelAccount(id=ALICE, aad_object_id=ALICE, name='Alice Example'))
    with pytest.raises(names.NotFound):
        names.resolve('Alice', ['chat'], names.load_chats())
    # A complete roster without the owner is still not the owner's 1:1.
    con.execute("INSERT INTO meta VALUES ('roster:chat','complete')")
    con.commit()
    with pytest.raises(names.NotFound):
        names.resolve('Alice', ['chat'], names.load_chats())
    store.remember_member(con, 'chat', TeamsChannelAccount(id=OWNER, aad_object_id=OWNER, name='Owner Example'))
    assert names.resolve('Alice', ['chat'], names.load_chats()) == 'chat'
    store.upsert_conversation(con, id='group', type='groupChat', name='Alice', service_url='', tenant_id=OWNER)
    (config.config_dir().parent / 'router.toml').write_text('''[[routes]]
source = "teams"
name = "Alice"
chats = ["group"]
''')
    assert names.resolve('Alice', ['chat', 'group'], names.load_chats()) == 'chat'
    # A third human invalidates person aliases even after a complete fetch.
    store.remember_member(con, 'chat', TeamsChannelAccount(id='bob', aad_object_id='bob', name='Bob Example'))
    with pytest.raises(names.NotFound):
        names.resolve('Alice Example', ['chat'], names.load_chats())


@pytest.mark.parametrize('source,url', [
    ('graph', 'https://graph.microsoft.com/v1.0/chats/chat/messages/m1/hostedContents/../../../../private/messages/m2/hostedContents/image/$value'),
    ('graph', 'https://graph.microsoft.com/v1.0/chats/chat/messages/m1/hostedContents/%2e%2e/%2e%2e/%2e%2e/%2e%2e/private/messages/m2/hostedContents/image/$value'),
    ('graph', 'https://graph.microsoft.com/v1.0/chats/chat/messages/m1/hostedContents/%252e%252e%252fprivate/$value'),
    ('graph', 'https://graph.microsoft.com/v1.0/chats/private/messages/m2/hostedContents/image/$value'),
    ('live', 'https://smba.trafficmanager.net/v3/attachments/image/../../conversations/private/activities/m2/hostedContents/image/$value'),
    ('live', 'https://smba.trafficmanager.net/v3/attachments/%252e%252e%252fprivate/views/original'),
    ('live', 'https://smba.trafficmanager.net/amer/../v3/attachments/image/views/original'),
    ('live', 'https://smba.trafficmanager.net/%252e%252e/v3/attachments/image/views/original'),
    ('live', 'https://smba.trafficmanager.net/amer%252fprivate/v3/attachments/image/views/original'),
    ('live', 'https://smba.trafficmanager.net/amer/v3/attachments/%252e%252e%252fprivate/views/original'),
    ('live', 'https://untrusted.example.com/amer/v3/attachments/image/views/original'),
])
def test_image_download_rejects_traversal_and_cross_message_endpoints(state, source, url):
    _, settings = state
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b'private-image', headers={'Content-Type': 'image/png'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await media.download(client, dict(contentType='image/png', contentUrl=url),
                                        'test-token', settings, source=source, key='chat/m1')
    result = asyncio.run(run())
    assert requests == []  # exercise the authenticated downloader, not just its URL parser
    assert result['download_status'] == 'unavailable' and 'local_path' not in result
    if source == 'graph':
        assert media.hosted_images(f'<img src="{url}">', catchup.message_base('chat', 'm1')) == []


def test_graph_paging_retries_the_failed_page_with_pacing_and_retry_after(state, monkeypatch):
    con, settings = state
    from email.utils import format_datetime
    clock = [time.time()]
    sleeps, requests = [], []
    monkeypatch.setattr(catchup, 'time', SimpleNamespace(time=lambda: clock[0]), raising=False)
    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    def respond(request):
        requests.append((str(request.url), clock[0]))
        if len(requests) == 1:
            return httpx.Response(200, json={'value': [message('page-one')], '@odata.nextLink': str(request.url) + '&$skiptoken=two'})
        if len(requests) == 2:
            return httpx.Response(429, headers={'Retry-After': '2'})
        if len(requests) == 3:
            after = format_datetime(datetime.fromtimestamp(clock[0] + 4, timezone.utc), usegmt=True)
            return httpx.Response(503, headers={'Retry-After': after})
        return httpx.Response(200, json={'value': [message('page-two')]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await catchup.sync_chat(con, client, 'token', 'chat', settings, 'bot')
    asyncio.run(run())
    assert len(requests) == 4 and len({url for url, _ in requests[1:]}) == 1
    assert requests[1][1] - requests[0][1] >= 1
    assert requests[2][1] - requests[1][1] >= 2
    assert requests[3][1] - requests[2][1] >= 3
    assert {r['id'] for r in con.execute('SELECT id FROM messages')} == {'page-one', 'page-two'}
    assert con.execute('SELECT cursor FROM catchup').fetchone()[0]


@pytest.mark.parametrize('failure', ['timeout', 429, 503])
def test_media_retry_survives_cursor_advance_and_store_reopen(state, monkeypatch, failure):
    con, settings = state
    clock = [time.time()]
    monkeypatch.setattr(media, 'time', SimpleNamespace(time=lambda: clock[0]))
    image = catchup.message_base('chat', 'm1') + '/hostedContents/image/$value'
    lists, images = [], []
    def respond(request):
        if '/hostedContents/' in request.url.path:
            images.append(request)
            if len(images) == 1:
                if failure == 'timeout':
                    raise httpx.ReadTimeout('offline timeout', request=request)
                return httpx.Response(failure, headers={'Retry-After': '2'})
            return httpx.Response(200, content=b'recovered-image', headers={'Content-Type': 'image/png'})
        lists.append(request)
        return httpx.Response(200, json={'value': [message(body=dict(contentType='html', content=f'<img src="{image}">'))] if len(lists) == 1 else []})
    async def run(db):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await catchup.sync_chat(db, client, 'token', 'chat', settings, 'bot')
            await media.retry_pending(db, client, 'token', settings, source='graph')
    asyncio.run(run(con))
    item = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
    assert item['download_status'] == 'pending'
    assert con.execute('SELECT cursor FROM catchup').fetchone()[0]
    # The unchanged message is absent from all later list responses. Retry state must be durable.
    with store.connect(config.db_path()) as reopened:
        asyncio.run(run(reopened))
        assert len(images) == 1  # backoff is respected
        clock[0] += 600
        asyncio.run(run(reopened))
    item = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
    from pathlib import Path
    assert item['download_status'] == 'available' and Path(item['local_path']).read_bytes() == b'recovered-image'
    assert len(images) == 2 and len(lists) == 3


@pytest.mark.parametrize('failure', ['exhausted', 'cancelled', 'long-retry-after'])
def test_throttled_page_resumes_after_restart_without_replaying_first_page(state, monkeypatch, failure):
    con, settings = state
    clock = [time.time()]
    monkeypatch.setattr(catchup, 'time', SimpleNamespace(time=lambda: clock[0]))
    calls, waits = [], []
    recovered = False
    async def sleep(delay):
        waits.append(delay)
        if failure == 'cancelled' and len(calls) == 2:
            raise asyncio.CancelledError
        clock[0] += delay
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    def respond(request):
        calls.append(str(request.url))
        if '$skiptoken=two' not in str(request.url):
            return httpx.Response(200, json={'value': [message('first')], '@odata.nextLink': str(request.url) + '&$skiptoken=two'})
        if not recovered:
            return httpx.Response(429, headers={'Retry-After': '3600' if failure == 'long-retry-after' else '2'})
        return httpx.Response(200, json={'value': [message('second')]})
    async def run(db):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await catchup.sync_chat(db, client, 'token', 'chat', settings, 'bot')
    with pytest.raises(asyncio.CancelledError if failure == 'cancelled' else httpx.HTTPStatusError):
        asyncio.run(run(con))
    state = con.execute('SELECT cursor,next_url,upper_bound,retry_at FROM catchup').fetchone()
    assert state is not None and state['cursor'] is None and '$skiptoken=two' in state['next_url']
    assert max(waits) <= 30
    recovered = True
    with store.connect(config.db_path()) as reopened:
        count = len(calls)
        asyncio.run(run(reopened))
        assert len(calls) == count  # Retry-After survives restart.
        clock[0] += 4000
        asyncio.run(run(reopened))
        row = reopened.execute('SELECT cursor,next_url FROM catchup').fetchone()
    assert len(calls) == count + 1 and '$skiptoken=two' in calls[-1]
    assert row['cursor'] == state['upper_bound'] and row['next_url'] is None
    assert {r[0] for r in con.execute('SELECT id FROM messages')} == {'first', 'second'}


@pytest.mark.parametrize('prefix', ['amer', 'amer-client-ss.msg'])
def test_regional_connector_image_download_preserves_service_prefix(state, prefix):
    _, settings = state
    url = f'https://smba.trafficmanager.net/{prefix}/v3/attachments/image/views/original'
    calls = []
    def respond(request):
        calls.append(request)
        assert str(request.url) == url
        assert request.headers['Authorization'] == 'Bearer connector-token'
        return httpx.Response(200, content=b'regional-image', headers={'Content-Type': 'image/png'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await media.download(client, dict(contentType='image/png', contentUrl=url),
                                        'connector-token', settings, source='live', key='chat/m1')
    item = asyncio.run(run())
    assert item['download_status'] == 'available' and len(calls) == 1
    from pathlib import Path
    assert Path(item['local_path']).read_bytes() == b'regional-image'


@pytest.mark.parametrize('source', ['graph', 'live'])
def test_image_401_renews_token_after_cursor_advance_and_respects_backoff(state, monkeypatch, source):
    con, settings = state
    settings['tenant_id'] = OWNER
    config.save_credentials(config.Credentials(ALICE, 'test-secret', tenant_id=OWNER))
    clock = [time.time()]
    monkeypatch.setattr(media, 'time', SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(catchup, 'time', SimpleNamespace(time=lambda: clock[0]))
    async def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    image = (catchup.message_base('chat', 'm1') + '/hostedContents/image/$value' if source == 'graph' else
             'https://smba.trafficmanager.net/v3/attachments/image/views/original')
    if source == 'live':
        store.record_message(con, conversation_id='chat', id='m1', ts='2026-10-24T10:00:00Z',
                             sender_aad_id=ALICE, sender_name='Alice Example', text='Live image', mentioned=False,
                             reply_to_id=None, attachments_json=json.dumps([dict(contentType='image/png', contentUrl=image)]))
        media.queue(con, 'chat', 'm1', 'live')
    lists, images, renewals = [], [], []
    def respond(request):
        if request.url.host == 'login.microsoftonline.com':
            renewals.append(request)
            assert con.execute('SELECT cursor FROM catchup').fetchone()[0]
            fields = parse_qs(request.content.decode())
            expected = 'https://graph.microsoft.com/.default' if source == 'graph' else 'https://api.botframework.com/.default'
            assert fields['scope'] == [expected] and fields['grant_type'] == ['client_credentials']
            assert request.url.path == f'/{OWNER}/oauth2/v2.0/token'
            return httpx.Response(200, json={'access_token': 'renewed-token'})
        if '/hostedContents/' in request.url.path or '/attachments/' in request.url.path:
            images.append(request)
            if len(images) <= 2:
                # A rejected renewal must also remain pending, without a tight retry loop.
                return httpx.Response(401)
            assert request.headers['Authorization'] == 'Bearer renewed-token'
            return httpx.Response(200, content=b'renewed-image', headers={'Content-Type': 'image/png'})
        lists.append(request)
        return httpx.Response(200, json={'value': [message(body=dict(contentType='html', content=f'<img src="{image}">'))]
                                        if source == 'graph' and len(lists) == 1 else []})
    async def token(*args):
        return 'expired-token'  # The SDK cache may keep returning the rejected token.
    app = SimpleNamespace(id='bot', token_provider=SimpleNamespace(get_app_token=token))
    async def run(db):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await catchup.sync(db, client, app, settings)
            await media.retry_pending(db, client, 'expired-token', settings, source=source)
    asyncio.run(run(con))
    item = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
    assert item['http_status'] == 401
    assert item['download_status'] == 'pending'
    assert con.execute('SELECT cursor FROM catchup').fetchone()[0]
    assert con.execute('SELECT count(*) FROM pending_media').fetchone()[0] == 1
    with store.connect(config.db_path()) as reopened:
        asyncio.run(run(reopened))
        assert len(images) == 1 and renewals == []
        for attempt in range(2):
            clock[0] += 600
            asyncio.run(run(reopened))
            assert len(images) == attempt + 2 and len(renewals) == attempt + 1
            if attempt == 0:
                assert reopened.execute('SELECT count(*) FROM pending_media').fetchone()[0] == 1
                asyncio.run(run(reopened))
                assert len(images) == 2  # Backoff also follows the renewed token's 401.
    item = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
    from pathlib import Path
    assert item['download_status'] == 'available' and Path(item['local_path']).read_bytes() == b'renewed-image'
    assert con.execute('SELECT count(*) FROM pending_media').fetchone()[0] == 0



def test_name_reader_keeps_members_confirmation_and_generation_in_one_snapshot(state, after_read):
    con, _ = state
    config.config_dir().mkdir()
    config.config_path().write_text(f'owner_teams_id = "{OWNER}"\n')
    people = [TeamsChannelAccount(id=id, aad_object_id=id, name=name) for id, name in
              [(OWNER, 'Owner Example'), (ALICE, 'Alice Example'),
               ('00000000-0000-0000-0000-000000000002', 'Bob Example')]]
    for person in people[:2]:
        store.remember_member(con, 'chat', person)
    store.invalidate_roster(con, 'chat')
    async def members():
        return people
    app = SimpleNamespace(api=SimpleNamespace(from_service_url=lambda url: SimpleNamespace(
        conversations=SimpleNamespace(members=lambda chat: SimpleNamespace(get_all=members)))))
    called = after_read(lambda sql: sql.startswith('select') and 'from members' in sql,
                        lambda: asyncio.run(catchup.roster(con, app, store.get_conversation(con, 'chat'))))
    chats = names.load_chats()
    assert called and con.execute('SELECT count(*) FROM members').fetchone()[0] == 3
    with pytest.raises(names.NotFound):
        names.resolve('Alice', ['chat'], chats)
    with pytest.raises(names.NotFound):
        names.resolve('Alice', ['chat'], names.load_chats())


@pytest.mark.parametrize('first_status', [200, 503])
def test_media_job_claim_prevents_duplicate_http_between_workers(state, monkeypatch, first_status):
    con, settings = state
    clock = [time.time()]
    monkeypatch.setattr(media, 'time', SimpleNamespace(time=lambda: clock[0]))
    image = catchup.message_base('chat', 'm1') + '/hostedContents/image/$value'
    store.record_message(con, conversation_id='chat', **catchup.normalise(message(attachments=[
        dict(contentType='image/png', contentUrl=image)]), 'bot'))
    media.queue(con, 'chat', 'm1', 'graph')
    calls = []
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def respond(request):
            calls.append(request)
            if len(calls) == 1:
                started.set()
                await release.wait()
                return httpx.Response(first_status, content=b'first-image', headers={'Content-Type': 'image/png'})
            return httpx.Response(200, content=b'recovered-image', headers={'Content-Type': 'image/png'})
        with store.connect(config.db_path()) as other:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                first = asyncio.create_task(media.retry_pending(con, client, 'token', settings, source='graph'))
                await started.wait()
                second = asyncio.create_task(media.retry_pending(other, client, 'token', settings, source='graph'))
                await asyncio.sleep(0)
                release.set()
                await asyncio.gather(first, second)
                assert len(calls) == 1
                clock[0] += 600
                await media.retry_pending(other, client, 'token', settings, source='graph')
    asyncio.run(run())
    item = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
    assert item['download_status'] == 'available'
    assert con.execute('SELECT count(*) FROM pending_media').fetchone()[0] == 0
    assert len(calls) == (1 if first_status == 200 else 2)


@pytest.mark.parametrize('terminal', ['available', 'expired'])
@pytest.mark.parametrize('late_status', [200, 503])
def test_stale_media_completion_cannot_replace_success_or_expiry(state, monkeypatch, terminal, late_status):
    con, settings = state
    clock = [time.time()]
    monkeypatch.setattr(media, 'time', SimpleNamespace(time=lambda: clock[0]))
    image = catchup.message_base('chat', 'm1') + '/hostedContents/image/$value'
    store.record_message(con, conversation_id='chat', **catchup.normalise(message(attachments=[
        dict(contentType='image/png', contentUrl=image)]), 'bot'))
    media.queue(con, 'chat', 'm1', 'graph')
    calls = []
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def respond(request):
            calls.append(request)
            if len(calls) == 1:
                started.set()
                await release.wait()
                return httpx.Response(late_status, content=b'stale-image', headers={'Content-Type': 'image/png'})
            return httpx.Response(200, content=b'current-image', headers={'Content-Type': 'image/png'})
        with store.connect(config.db_path()) as other:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                first = asyncio.create_task(media.retry_pending(con, client, 'token', settings, source='graph'))
                await started.wait()
                # Simulate a stalled worker whose lease has expired, then another worker's success.
                clock[0] += 600
                await media.retry_pending(other, client, 'token', settings, source='graph')
                if terminal == 'expired':
                    path = json.loads(other.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]['local_path']
                    os.utime(path, (clock[0] - 31 * 86400,) * 2)
                    media.prune(other, settings)
                release.set()
                await first
    asyncio.run(run())
    item = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
    assert item['download_status'] == terminal
    assert con.execute('SELECT count(*) FROM pending_media').fetchone()[0] == 0
    from pathlib import Path
    if terminal == 'available':
        assert Path(item['local_path']).read_bytes() == b'current-image'
    else:
        assert 'local_path' not in item and not list((config.state_dir() / 'media').glob('*.img'))
    assert not list((config.state_dir() / 'media').glob('*.part'))


@pytest.mark.parametrize('change', ['metadata', 'replacement'])
@pytest.mark.parametrize('late_status', [200, 503])
def test_media_completion_keeps_current_edit_and_replacement_work(state, change, late_status):
    con, settings = state
    image = catchup.message_base('chat', 'm1') + '/hostedContents/image/$value'
    attachment = dict(contentType='image/png', contentUrl=image, name='Before')
    store.record_message(con, conversation_id='chat', **catchup.normalise(message(attachments=[attachment]), 'bot'))
    media.queue(con, 'chat', 'm1', 'graph')
    calls = []
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def respond(request):
            calls.append(request)
            if len(calls) == 1:
                started.set()
                await release.wait()
                return httpx.Response(late_status, content=b'stale-image', headers={'Content-Type': 'image/png'})
            return httpx.Response(200, content=b'current-image', headers={'Content-Type': 'image/png'})
        with store.connect(config.db_path()) as other:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                first = asyncio.create_task(media.retry_pending(con, client, 'token', settings, source='graph'))
                await started.wait()
                if change == 'replacement':
                    store.record_message(other, conversation_id='chat', **catchup.normalise(message(
                        lastModifiedDateTime='2026-10-24T10:01:00Z', lastEditedDateTime='2026-10-24T10:01:00Z', attachments=[]), 'bot'))
                    media.queue(other, 'chat', 'm1', 'graph')
                store.record_message(other, conversation_id='chat', **catchup.normalise(message(
                    lastModifiedDateTime='2026-10-24T10:02:00Z', lastEditedDateTime='2026-10-24T10:02:00Z',
                    attachments=[attachment | dict(name='After')]), 'bot'))
                media.queue(other, 'chat', 'm1', 'graph')
                release.set()
                await first
                item = json.loads(other.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
                assert item['name'] == 'After'
                if change == 'replacement':
                    assert item.get('download_status') != 'available'
                    assert other.execute('SELECT count(*) FROM pending_media').fetchone()[0] == 1
                    await media.retry_pending(other, client, 'token', settings, source='graph')
    asyncio.run(run())
    if change == 'replacement':
        item = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
        from pathlib import Path
        assert item['download_status'] == 'available' and Path(item['local_path']).read_bytes() == b'current-image'
        assert len(calls) == 2


@pytest.mark.parametrize('operation', ['message', 'queue', 'prune'])
def test_message_queue_and_prune_writes_do_not_overwrite_concurrent_changes(state, after_read, operation):
    con, settings = state
    con.execute('PRAGMA busy_timeout=0')
    image = catchup.message_base('chat', 'm1') + '/hostedContents/image/$value'
    attachment = dict(contentType='image/png', contentUrl=image)
    if operation == 'prune':
        directory = config.state_dir() / 'media'
        directory.mkdir()
        path = directory / 'old.img'
        path.write_bytes(b'expired-image')
        os.utime(path, (time.time() - 31 * 86400,) * 2)
        attachment.update(local_path=str(path), download_status='available')
    store.record_message(con, conversation_id='chat', **catchup.normalise(message(attachments=[attachment]), 'bot'))
    deferred = []
    def writer():
        if operation == 'message':
            values = message(deletedDateTime='2026-10-24T10:05:00Z', lastModifiedDateTime='2026-10-24T10:05:00Z')
        else:
            values = message(lastEditedDateTime='2026-10-24T10:05:00Z', lastModifiedDateTime='2026-10-24T10:05:00Z',
                             attachments=[] if operation == 'prune' else [attachment | dict(contentUrl=image.replace('/image/', '/new-image/'))])
        store.record_message(con, conversation_id='chat', **catchup.normalise(values, 'bot'))
        if operation == 'queue':
            media.queue(con, 'chat', 'm1', 'graph')
    def concurrent_writer():
        try:
            writer()
        except sqlite3.OperationalError as error:
            assert 'locked' in str(error)
            con.rollback()
            deferred.append(writer)
    patterns = dict(message='select * from messages where', queue='select deleted,attachments_json from messages',
                    prune='select rowid,conversation_id,id from messages')
    called = after_read(lambda sql: sql.startswith(patterns[operation]), concurrent_writer)
    with store.connect(config.db_path()) as reader:
        if operation == 'message':
            store.record_message(reader, conversation_id='chat', **catchup.normalise(message(
                lastEditedDateTime='2026-10-24T10:01:00Z', lastModifiedDateTime='2026-10-24T10:01:00Z'), 'bot'))
        elif operation == 'queue':
            media.queue(reader, 'chat', 'm1', 'graph')
        else:
            media.prune(reader, settings)
    for retry in deferred:
        retry()
    assert called
    row = con.execute('SELECT * FROM messages').fetchone()
    if operation == 'message':
        assert row['deleted'] == 1 and row['modified_at'] == '2026-10-24T10:05:00Z'
    elif operation == 'queue':
        assert [r['url'] for r in con.execute('SELECT url FROM pending_media')] == [image.replace('/image/', '/new-image/')]
    else:
        assert json.loads(row['attachments_json']) == []


def test_status_catchup_and_delivery_read_one_snapshot(state, after_read, capsys):
    con, _ = state
    store.catchup_result(con, 'chat', error='HTTP 403', status=403)
    def commit_recovery():
        stamp = datetime.now(timezone.utc).isoformat()
        store.record_message(con, conversation_id='chat', **catchup.normalise(message(createdDateTime=stamp), 'bot'))
        con.execute("UPDATE messages SET graph_seen_at=datetime('now','-10 minutes')")
        con.commit()
        store.catchup_result(con, 'chat', cursor=stamp)
    called = after_read(lambda sql: sql.startswith('select') and 'from conversations c left join catchup' in sql,
                        commit_recovery)
    cli.main(['status', '--json'])
    report = json.loads(capsys.readouterr().out)
    assert called and report['catchup']['failing_chats'] == 1
    assert report['delivery']['graph_only'] == 0


def test_media_cache_expiring_during_retry_keeps_recoverable_work(state, monkeypatch):
    from pathlib import Path
    con, settings = state
    clock = [time.time()]
    monkeypatch.setattr(media, 'time', SimpleNamespace(time=lambda: clock[0]))
    image = catchup.message_base('chat', 'm1') + '/hostedContents/image/$value'
    attachment = dict(contentType='image/png', contentUrl=image)
    store.record_message(con, conversation_id='chat', **catchup.normalise(message(attachments=[attachment]), 'bot'))
    media.queue(con, 'chat', 'm1', 'graph')
    calls, pruned = [], []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=b'image', headers={'Content-Type': 'image/png'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            # A worker crashed after caching bytes but before publishing metadata.
            cached = await media.download(client, attachment, 'token', settings, source='graph', key='chat/m1')
            os.utime(cached['local_path'], (clock[0] - 31 * 86400,) * 2)
            chmod = Path.chmod
            def expire(path, *args, **kw):
                chmod(path, *args, **kw)
                if path.suffix == '.img' and not pruned:
                    pruned.append(True)
                    media.prune(con, settings)
            with monkeypatch.context() as patch:
                patch.setattr(Path, 'chmod', expire)
                await media.retry_pending(con, client, 'token', settings, source='graph')
            item = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
            assert pruned and item['download_status'] == 'pending' and 'local_path' not in item
            job = con.execute('SELECT next_attempt FROM pending_media').fetchone()
            assert job is not None
            clock[0] = job['next_attempt'] + 1
            await media.retry_pending(con, client, 'token', settings, source='graph')
    asyncio.run(run())
    item = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
    assert item['download_status'] == 'available' and Path(item['local_path']).read_bytes() == b'image'
    assert len(calls) == 2 and con.execute('SELECT count(*) FROM pending_media').fetchone()[0] == 0


def test_expiry_enumerates_without_writer_lock_and_commits_bounded_batches(state, monkeypatch):
    from pathlib import Path
    con, settings = state
    directory = config.state_dir() / 'media'
    directory.mkdir()
    paths = []
    for i in range(80):
        path = directory / f'{i}.img'
        path.write_bytes(b'expired')
        os.utime(path, (time.time() - 31 * 86400,) * 2)
        store.record_message(con, conversation_id='chat', **catchup.normalise(message(str(i), attachments=[
            dict(contentType='image/png', local_path=str(path), download_status='available')]), 'bot'))
        paths.append(path)
    glob, unlink = Path.glob, Path.unlink
    enumerated, writes = [], []
    def observe_glob(path, pattern):
        if path == directory:
            enumerated.append(con.in_transaction)
            with sqlite3.connect(config.db_path(), timeout=0) as writer:
                writer.execute("INSERT OR REPLACE INTO meta VALUES ('other-writer','progress')")
        yield from glob(path, pattern)
    def observe_sql(sql):
        if sql.startswith('BEGIN'):
            writes.append(set())
        elif sql.startswith('UPDATE messages'):
            # SQLite repeats the parent's trace entry while executing triggers.
            writes[-1].add(('message', sql))
    def observe_unlink(path, *args, **kw):
        assert con.in_transaction  # publication fencing still holds during deletion
        writes[-1].add(('file', path))
        return unlink(path, *args, **kw)
    monkeypatch.setattr(Path, 'glob', observe_glob)
    monkeypatch.setattr(Path, 'unlink', observe_unlink)
    con.set_trace_callback(observe_sql)
    media.prune(con, settings)
    assert enumerated and not any(enumerated)
    assert writes and max(map(len, writes)) <= 32 and sum(map(len, writes)) == 160
    assert not any(p.exists() for p in paths)
    items = [json.loads(row[0])[0] for row in con.execute('SELECT attachments_json FROM messages')]
    assert len(items) == 80 and all(i['download_status'] == 'expired' and 'local_path' not in i for i in items)


def test_change_sequence_tracks_content_visibility_and_media_transactionally(state):
    con, settings = state
    def progress():
        values = dict(con.execute("SELECT key,value FROM meta WHERE key IN ('store_generation','change_sequence')"))
        assert values.get('store_generation'), 'store needs a persistent generation'
        return values['store_generation'], int(values['change_sequence'])
    generation, initial = progress()
    image = catchup.message_base('chat', 'm1') + '/hostedContents/image/$value'
    original = message(attachments=[dict(contentType='image/png', contentUrl=image)])
    store.record_message(con, conversation_id='chat', **catchup.normalise(original, 'bot'))
    first = progress()[1]
    assert first > initial
    store.record_message(con, conversation_id='other-chat', **catchup.normalise(message('other'), 'bot'))
    store.record_message(con, conversation_id='chat', **catchup.normalise(message('child', replyToId='m1'), 'bot'))
    second = progress()[1]
    assert second > first
    store.record_message(con, conversation_id='chat', **catchup.normalise(original, 'bot'))
    assert progress()[1] == second  # identical delivery changes no content
    for i, changes in enumerate([
        dict(body=dict(contentType='text', content='Edited'), lastEditedDateTime='2026-10-24T10:01:00Z'),
        dict(deletedDateTime='2026-10-24T10:02:00Z'),
        dict(body=dict(contentType='text', content='Restored')),
    ], 1):
        store.record_message(con, conversation_id='chat', **catchup.normalise(
            original | changes | dict(lastModifiedDateTime=f'2026-10-24T10:0{i}:00Z'), 'bot'))
        current = progress()[1]
        assert current > second
        assert con.execute("SELECT change_seq FROM messages WHERE conversation_id='chat' AND id='m1'").fetchone()[0] == current
        assert con.execute("SELECT change_seq FROM messages WHERE conversation_id='chat' AND id='child'").fetchone()[0] == current
        second = current
    media.queue(con, 'chat', 'm1', 'graph')
    async def download():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, content=b'image', headers={'Content-Type': 'image/png'}))) as client:
            await media.retry_pending(con, client, 'token', settings, source='graph')
    asyncio.run(download())
    assert progress()[1] > second
    second = progress()[1]
    path = json.loads(con.execute("SELECT attachments_json FROM messages WHERE conversation_id='chat' AND id='m1'").fetchone()[0])[0]['local_path']
    os.utime(path, (time.time() - 31 * 86400,) * 2)
    media.prune(con, settings)
    assert progress()[1] > second
    before_rollback = progress()
    with pytest.raises(RuntimeError):
        with con:
            con.execute("UPDATE messages SET text='Rolled back'")
            raise RuntimeError('rollback')
    assert progress() == before_rollback
    with store.connect(config.db_path()) as reopened:
        assert dict(reopened.execute("SELECT key,value FROM meta"))['store_generation'] == generation
        assert int(reopened.execute("SELECT value FROM meta WHERE key='change_sequence'").fetchone()[0]) == before_rollback[1]


@pytest.mark.parametrize('source', ['live', 'graph'])
def test_image_work_commits_with_message_and_rolls_back_on_queue_failure(state, source):
    con, settings = state
    image = (catchup.message_base('chat', 'm1') + '/hostedContents/image/$value' if source == 'graph' else
             'https://smba.trafficmanager.net/v3/attachments/image/views/original')
    values = catchup.normalise(message(attachments=[dict(contentType='image/png', contentUrl=image)]), 'bot')
    values['source'] = source
    store.record_message(con, conversation_id='chat', **values)
    # Simulate exiting immediately after the message commit, before any HTTP handler resumes.
    with store.connect(config.db_path()) as reopened:
        job = reopened.execute('SELECT source,url FROM pending_media').fetchone()
        assert job is not None and tuple(job) == (source, image)
    con.execute("""CREATE TRIGGER deny_image_work BEFORE INSERT ON pending_media
                   BEGIN SELECT RAISE(ABORT, 'queue unavailable'); END""")
    con.commit()
    with pytest.raises(sqlite3.IntegrityError, match='queue unavailable'):
        store.record_message(con, conversation_id='chat', **(values | dict(id='m2',
            attachments_json=json.dumps([dict(contentType='image/png', contentUrl=image.replace('/m1/', '/m2/'))]))))
    assert not con.execute("SELECT 1 FROM messages WHERE id='m2'").fetchone()
    assert con.execute('SELECT count(*) FROM pending_media').fetchone()[0] == 1


def test_graph_mentions_fill_unknown_push_data_and_publish_the_change(state):
    con, _ = state
    bob = '00000000-0000-0000-0000-000000000002'
    store.record_message(con, conversation_id='chat', id='m1', ts='2026-10-24T10:00:00+00:00', sender_aad_id=ALICE,
                         sender_name='Alice Example', text='Hello & world', mentioned=False, reply_to_id=None)
    assert con.execute("SELECT mentions_json FROM messages WHERE id='m1'").fetchone()[0] is None
    before = int(con.execute("SELECT value FROM meta WHERE key='change_sequence'").fetchone()[0])
    tags = [dict(id=0, mentionText='Bob', mentioned=dict(user=dict(id=bob.upper(), displayName='Bob Example'))),
            dict(id=1, mentionText='Example', mentioned=dict(user=dict(id=bob.upper(), displayName='Bob Example'))),
            dict(id=2, mentionText='Everyone', mentioned=dict(conversation=dict(id='chat', conversationIdentityType='chat'))),
            dict(id=3, mentionText='Toucan', mentioned=dict(application=dict(id='bot', displayName='Toucan')))]
    store.record_message(con, conversation_id='chat', **catchup.normalise(message(mentions=tags), 'bot'))
    row = con.execute("SELECT mentions_json,mentioned,quoted_json,change_seq FROM messages WHERE id='m1'").fetchone()
    assert json.loads(row['mentions_json']) == [dict(text='Bob Example', kind='user', id=bob),
                                                dict(text='Everyone', kind='conversation', id='chat'),
                                                dict(text='Toucan', kind='application', id='bot')]
    assert row['mentioned'] == 1 and row['quoted_json'] == '[]' and row['change_seq'] > before
    # A later duplicate with less identity data does not replace the resolved list.
    seq = row['change_seq']
    store.record_message(con, conversation_id='chat', id='m1', ts='2026-10-24T10:00:00Z', sender_aad_id=ALICE,
                         sender_name='Alice Example', text='Hello & world', mentioned=False, reply_to_id=None,
                         raw_html='<p>Hello &amp; world</p>', mentions_json=json.dumps([dict(text='Bob Example', kind='user', id=None)]))
    row = con.execute("SELECT mentions_json,change_seq FROM messages WHERE id='m1'").fetchone()
    assert len(json.loads(row['mentions_json'])) == 3 and row['change_seq'] == seq


def test_store_upgrade_tracks_mentions_and_backfills_earlier_graph_rows(tmp_path):
    path = tmp_path / 'bot.db'
    with store.connect(path) as con:
        store.record_message(con, conversation_id='chat', **catchup.normalise(message('graph'), 'bot'))
        store.record_message(con, conversation_id='chat', id='live', ts='2026-10-24T10:00:00+00:00', sender_aad_id=ALICE,
                             sender_name='Alice Example', text='Live', mentioned=False, reply_to_id=None)
    # Recreate the 0.1.0 layout: no mentions column, mentions kept in quoted_json, and a
    # change trigger that does not watch mentions.
    graph_tag = [dict(id=0, mentionText='Bob', mentioned=dict(user=dict(id='00000000-0000-0000-0000-000000000002')))]
    with sqlite3.connect(path) as con:
        con.execute('DROP TRIGGER message_change_UPDATE')
        con.execute('ALTER TABLE messages DROP COLUMN mentions_json')
        con.execute("UPDATE messages SET quoted_json=? WHERE id='graph'", (json.dumps(graph_tag),))
        con.execute("""CREATE TRIGGER message_change_UPDATE AFTER UPDATE ON messages WHEN OLD.text IS NOT NEW.text
                       BEGIN UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='change_sequence'; END""")
        generation = con.execute("SELECT value FROM meta WHERE key='store_generation'").fetchone()[0]
        sequence = int(con.execute("SELECT value FROM meta WHERE key='change_sequence'").fetchone()[0])
    with store.connect(path) as con:
        rows = {r['id']: r for r in con.execute('SELECT id,mentions_json,change_seq FROM messages')}
        assert con.execute("SELECT value FROM meta WHERE key='store_generation'").fetchone()[0] == generation
        assert json.loads(rows['graph']['mentions_json']) == [dict(text='Bob', kind='user', id='00000000-0000-0000-0000-000000000002')]
        assert rows['graph']['change_seq'] > sequence  # the router rereads it
        assert rows['live']['mentions_json'] is None
        con.execute("UPDATE messages SET mentions_json='[]' WHERE id='live'")
        assert con.execute("SELECT change_seq FROM messages WHERE id='live'").fetchone()[0] > rows['graph']['change_seq']


def test_missing_consent_backs_off_per_chat_until_success_or_a_sign_of_access(state, monkeypatch):
    con, settings = state
    clock = [time.time()]
    monkeypatch.setattr(catchup, 'time', SimpleNamespace(time=lambda: clock[0]))
    async def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    store.upsert_conversation(con, id='healthy', type='groupChat', name=None, service_url='', tenant_id=OWNER)
    calls, denied = [], [True]

    async def token(scope, tenant):
        return 'graph-test-token'

    def respond(request):
        calls.append(request.url.path.split('/')[3])
        if '/chat/' in request.url.path and denied[0]:
            return httpx.Response(403)
        return httpx.Response(200, json={'value': []})

    def run():
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                await catchup.sync(con, client, SimpleNamespace(id='bot', token_provider=SimpleNamespace(get_app_token=token)), settings)
        calls.clear()
        asyncio.run(go())
        return calls.count('chat')

    interval = settings['catchup_interval_seconds']
    delays = []
    for _ in range(7):
        assert run() == 1
        row = con.execute("SELECT * FROM catchup WHERE conversation_id='chat'").fetchone()
        assert row['http_status'] == 403 and 'consent' in row['last_error']
        delays.append(round(row['retry_at'] - clock[0]))
        assert run() == 0 and 'healthy' in calls  # other chats keep their normal schedule
        clock[0] = row['retry_at'] + 1
    assert delays == [min(900, interval * 2 ** n) for n in range(1, 8)] and max(delays) == 900
    run()
    catchup.retry_now(con, 'chat')  # what consent signals do; SDK wiring is tested in test_ingest
    denied[0] = False
    assert run() == 1
    row = con.execute("SELECT * FROM catchup WHERE conversation_id='chat'").fetchone()
    assert row['last_error'] is None and row['auth_failures'] == 0 and row['retry_at'] is None
    denied[0] = True
    clock[0] += interval
    run()
    row = con.execute("SELECT * FROM catchup WHERE conversation_id='chat'").fetchone()
    assert round(row['retry_at'] - clock[0]) == interval * 2  # success reset the doubling
