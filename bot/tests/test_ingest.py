import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from microsoft_teams.api import (ConversationUpdateActivity, InstalledActivity, InstalledUpgradeActivity, MessageActivity,
                                  MessageDeleteActivity, MessageUpdateActivity, UninstalledActivity)

from toucan_bot import config, names, server, store

TENANT = '00000000-0000-0000-0000-000000000000'
ALICE = '00000000-0000-0000-0000-000000000001'


async def wait_until(check, pause=asyncio.sleep):
    # Give real worker threads time to run while scheduling remains accelerated.
    async with asyncio.timeout(2):
        while not check():
            await pause(.001)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv('TOUCAN_BOT_TENANT_ID', TENANT)
    monkeypatch.setenv('TOUCAN_BOT_PUBLIC_URL', 'https://bot.example.com')
    monkeypatch.setenv('TOUCAN_BOT_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('TOUCAN_BOT_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('TOUCAN_CONFIG', str(tmp_path / 'router.toml'))
    app, web = server.build()

    async def receive(model=MessageActivity, **values):
        payload = dict(id='m1', type='message', serviceUrl='https://smba.example.com/region',
                       timestamp='2026-10-24T10:00:00Z', text='<p>Hello &amp; <b>world</b></p>',
                       conversation=dict(id='chat', tenantId=TENANT, conversationType='groupChat', name='Project A'),
                       recipient=dict(id='bot', name='Toucan'),
                       **{'from': dict(id='alice', aadObjectId=ALICE, name='Alice Example')})
        payload.update(values)
        if model is MessageUpdateActivity and payload.get('text') is None:
            payload.pop('text')  # SDK defaults an absent notification body to an empty string.
        activity = model.model_validate(payload)
        # The SDK's own middleware chain: a handler that does not call ctx.next() stops the rest.
        processor = app.activity_processor
        ctx = await processor._build_context(activity, SimpleNamespace(service_url=activity.service_url), [])
        await processor.execute_middleware_chain(ctx, app.router.select_handlers(activity))

    return receive, web


def test_messages_edits_and_deletes_reach_the_store_via_sdk_handlers(bot):
    receive, _ = bot
    asyncio.run(receive(replyToId='quoted-id'))
    asyncio.run(receive())
    with store.connect(config.db_path()) as con:
        message = con.execute('SELECT * FROM messages').fetchone()
        assert con.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
        assert message['text'] == 'Hello & world' and not message['mentioned']
        assert message['raw_html'] == '<p>Hello &amp; <b>world</b></p>'
        assert message['reply_to_id'] == 'quoted-id' and message['sender_aad_id'] == ALICE
    asyncio.run(receive(MessageUpdateActivity, type='messageUpdate', channelData=dict(eventType='editMessage'),
                        timestamp='2026-10-24T10:01:00Z', text='<p>Edited</p>',
                        attachments=[dict(contentType='text/plain', name='brief.txt', contentUrl='https://example.com/file')]))
    asyncio.run(receive(MessageDeleteActivity, type='messageDelete', channelData=dict(eventType='softDeleteMessage'),
                        timestamp='2026-10-24T10:02:00Z'))
    asyncio.run(receive())
    with store.connect(config.db_path()) as con:
        message = con.execute('SELECT * FROM messages').fetchone()
        assert message['text'] == 'Edited' and message['deleted'] == 1
        assert message['ts'] == '2026-10-24T10:00:00+00:00'
        assert message['edited_at'] == '2026-10-24T10:01:00+00:00'
        assert message['reply_to_id'] == 'quoted-id'
        assert json.loads(message['attachments_json'])[0]['name'] == 'brief.txt'


def test_only_bot_mentions_are_stripped_and_other_tenants_are_ignored(bot):
    receive, _ = bot
    asyncio.run(receive(text='<at>Toucan</at> <at>Bob</at> check this', entities=[
        dict(type='mention', text='<at>Toucan</at>', mentioned=dict(id='bot', name='Toucan')),
        dict(type='mention', text='<at>Bob</at>', mentioned=dict(id='bob', name='Bob'))]))
    asyncio.run(receive(id='outsider', conversation=dict(id='other', tenantId='another-tenant')))
    with store.connect(config.db_path()) as con:
        messages = con.execute('SELECT * FROM messages').fetchall()
        assert len(messages) == 1
        assert messages[0]['text'] == 'Bob check this' and messages[0]['mentioned'] == 1



def test_push_mentions_are_stored_with_entra_ids_for_the_router(bot):
    receive, _ = bot
    bob, owner = '00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000009'
    asyncio.run(receive(ConversationUpdateActivity, type='conversationUpdate',
                        membersAdded=[dict(id='bob', aadObjectId=bob.upper(), name='Bob Example')]))
    def mention(text, **account):
        return dict(type='mention', text=f'<at>{text}</at>', mentioned=account)
    asyncio.run(receive(text='<at>Bob</at> <at>Example</at> <at>Owner</at> <at>Everyone</at> <at>Ops</at> <at>Toucan</at> hi', entities=[
        mention('Bob', id='bob', name='Bob Example'), mention('Example', id='bob', name='Bob Example'),
        mention('Owner', id='owner', name='Owner', aadObjectId=owner), mention('Everyone', id='chat', name='Everyone'),
        mention('Ops', id='tag-id', name='Ops', type='tag'), mention('Toucan', id='bot', name='Toucan'),
        dict(type='clientInfo', locale='en-US')]))
    asyncio.run(receive(id='plain', text='No tags'))
    asyncio.run(receive(id='stranger', text='<at>Carol</at> please', entities=[mention('Carol', id='carol', name='Carol')]))
    with store.connect(config.db_path()) as con:
        stored = {r['id']: r['mentions_json'] for r in con.execute('SELECT id,mentions_json FROM messages')}
    assert json.loads(stored['m1']) == [
        dict(text='Bob Example', kind='user', id=bob), dict(text='Owner', kind='user', id=owner),
        dict(text='Everyone', kind='conversation', id='chat'), dict(text='Ops', kind='tag', id='tag-id'),
        dict(text='Toucan', kind='application', id='bot')]
    assert json.loads(stored['plain']) == []
    # Not in the roster yet: kept, but unresolved, so the router cannot call it someone else's.
    assert json.loads(stored['stranger']) == [dict(text='Carol', kind='user', id=None)]


@pytest.mark.parametrize('event', ['upgrade', 'install', 'ordinary message', 'mention only', 'uninstall'])
def test_consent_signals_end_the_catchup_backoff_through_sdk_routing(bot, event):
    import time
    receive, _ = bot
    asyncio.run(receive(id='first'))  # registers the conversation
    with store.connect(config.db_path()) as con:
        store.catchup_result(con, 'chat', error='HTTP 403', status=403)
        con.execute('UPDATE catchup SET retry_at=?,auth_failures=3', (time.time() + 3600,))
        con.commit()
    install = dict(type='installationUpdate', text=None)
    if event == 'upgrade':
        asyncio.run(receive(InstalledUpgradeActivity, action='upgrade', **install))
    elif event == 'install':
        asyncio.run(receive(InstalledActivity, action='add', **install))
    elif event == 'uninstall':
        asyncio.run(receive(UninstalledActivity, action='remove', **install))
    elif event == 'ordinary message':
        asyncio.run(receive(id='second'))  # without consent Teams pushes only @mentions
    else:
        asyncio.run(receive(id='second', text='<at>Toucan</at> hi', entities=[
            dict(type='mention', text='<at>Toucan</at>', mentioned=dict(id='bot', name='Toucan'))]))
    with store.connect(config.db_path()) as con:
        row = con.execute("SELECT retry_at,auth_failures FROM catchup WHERE conversation_id='chat'").fetchone()
    cleared = event in ('upgrade', 'install', 'ordinary message')
    assert (row['retry_at'] is None) == cleared
    assert row['auth_failures'] == 3  # still doubles if access is in fact still missing

def test_conversation_names_and_personal_members_resolve_with_router_aliases(bot, tmp_path):
    receive, _ = bot
    asyncio.run(receive())
    asyncio.run(receive(ConversationUpdateActivity, type='conversationUpdate', topicName='Renamed project',
                        membersAdded=[dict(id='bob', aadObjectId='00000000-0000-0000-0000-000000000002', name='Bob Example')]))
    asyncio.run(receive(conversation=dict(id='personal', tenantId=TENANT, conversationType='personal', name='Personal room')))
    (tmp_path / 'router.toml').write_text('''[[routes]]
name = "project-a"
label = "Team room"
source = "teams"
chats = ["chat"]
[[routes]]
name = "elsewhere"
source = "teams"
chats = ["unknown-chat"]
''')
    chats = names.load_chats()
    ids = ['chat', 'personal']
    assert names.resolve('renamed project', ids, chats) == 'chat'
    assert names.resolve('project-a', ids, chats) == 'chat'
    assert names.resolve('team room', ids, chats) == 'chat'
    with pytest.raises(names.NotFound):
        names.resolve('Alice', ids, chats)
    assert names.resolve('personal', ids, chats) == 'personal'
    with pytest.raises(names.NotFound, match='Add Toucan'):
        names.resolve('elsewhere', ids, chats)
    with store.connect(config.db_path()) as con:
        assert con.execute('SELECT name FROM members WHERE id=?', ('bob',)).fetchone()[0] == 'Bob Example'


def test_heartbeat_refreshes_while_idle_and_stops_at_shutdown(bot, monkeypatch):
    _, web = bot
    original_sleep = asyncio.sleep
    ticks = []

    async def fast_sleep(seconds):
        ticks.append(seconds)
        await original_sleep(.001)

    monkeypatch.setattr(server.asyncio, 'sleep', fast_sleep)
    monkeypatch.setattr(store, 'now', lambda: '2026-10-24T10:10:00+00:00')

    async def run():
        async with web.router.lifespan_context(web):
            await wait_until(lambda: 120 in ticks)
            with sqlite3.connect(config.db_path()) as con:
                assert con.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone()[0] == '2026-10-24T10:10:00+00:00'
        before = len(ticks)
        await original_sleep(0)
        assert len(ticks) == before

    asyncio.run(run())
    assert set(ticks) == {60, 120}


def test_existing_store_migrates_additively_and_can_reopen(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as con:
        con.executescript('''CREATE TABLE messages (
            conversation_id TEXT NOT NULL, id TEXT NOT NULL, ts TEXT NOT NULL,
            sender_aad_id TEXT, sender_name TEXT, text TEXT NOT NULL, mentioned INTEGER NOT NULL,
            reply_to_id TEXT, received_at TEXT NOT NULL, PRIMARY KEY(conversation_id,id));
            INSERT INTO messages VALUES ('chat','old','2026-10-24T10:00:00Z',NULL,'Alice','<p>Keep &amp; me</p>',0,NULL,'now');
            UPDATE messages SET rowid=100;
            CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO meta VALUES ('store_generation','before-rebuild');
            INSERT INTO meta VALUES ('change_sequence','100');
            CREATE TABLE conversations (id TEXT PRIMARY KEY,type TEXT,name TEXT,service_url TEXT NOT NULL,
                tenant_id TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL);
            INSERT INTO conversations VALUES ('chat','groupChat','Project A','https://example.com',NULL,'first','last');
            CREATE TABLE sent (seq INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL,
                activity_id TEXT,text TEXT NOT NULL,sent_at TEXT NOT NULL);
            INSERT INTO sent VALUES (1,'chat','receipt','Already sent','sent-time');
            CREATE TABLE catchup (conversation_id TEXT PRIMARY KEY,cursor TEXT,last_success TEXT,last_error TEXT,http_status INTEGER);
            INSERT INTO catchup VALUES ('chat','2026-10-24T09:00:00Z','success',NULL,NULL);
            CREATE TABLE pending_media (conversation_id TEXT NOT NULL,message_id TEXT NOT NULL,
                source TEXT NOT NULL,url TEXT NOT NULL,attachment_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(conversation_id,message_id,source,url));
            INSERT INTO pending_media VALUES ('chat','old','graph','https://example.com/image','{}',2,100);''')
    generations = []
    for _ in range(2):
        with store.connect(path) as con:
            message = con.execute('SELECT * FROM messages').fetchone()
            assert message['text'] == 'Keep & me' and message['deleted'] == 0
            assert message['raw_html'] == '<p>Keep &amp; me</p>'
            assert message['edited_at'] is None and message['attachments_json'] == '[]'
            generations.append(con.execute("SELECT value FROM meta WHERE key='store_generation'").fetchone()[0])
            assert generations[-1] != 'before-rebuild' and generations[-1] == generations[0]
            assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '4'
            assert message['change_seq'] > 0
            assert int(con.execute("SELECT value FROM meta WHERE key='change_sequence'").fetchone()[0]) == message['change_seq']
            assert message['live_seen_at'] == 'now' and message['first_source'] == 'live'
            assert con.execute('SELECT name,first_seen,last_seen FROM conversations').fetchone()[:] == ('Project A', 'first', 'last')
            assert con.execute('SELECT activity_id,text,sent_at FROM sent').fetchone()[:] == ('receipt', 'Already sent', 'sent-time')
            assert con.execute('SELECT cursor,next_url,retry_at FROM catchup').fetchone()[:] == ('2026-10-24T09:00:00Z', None, None)
            assert con.execute('SELECT attempts,next_attempt,claim_id,claim_until FROM pending_media').fetchone()[:] == (2, 100, None, None)
            assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_replayed_edit_does_not_resurrect_deleted_message(bot):
    receive, _ = bot
    asyncio.run(receive())
    asyncio.run(receive(MessageDeleteActivity, type='messageDelete', channelData=dict(eventType='softDeleteMessage'),
                        timestamp='2026-10-24T10:02:00Z'))
    for minute in ('01', '03'):
        asyncio.run(receive(MessageUpdateActivity, type='messageUpdate', channelData=dict(eventType='editMessage'),
                            timestamp=f'2026-10-24T10:{minute}:00Z', text='Replayed edit'))
    with store.connect(config.db_path()) as con:
        row = con.execute('SELECT * FROM messages').fetchone()
        assert row['deleted'] == 1
        assert row['deleted_at'] == '2026-10-24T10:02:00+00:00'


@pytest.mark.parametrize('original_first', [False, True])
@pytest.mark.parametrize('creation', [None, '2026-10-24T15:05:00+05:00'])
def test_out_of_order_edit_preserves_creation_time(bot, original_first, creation):
    receive, _ = bot
    if original_first:
        asyncio.run(receive())
    asyncio.run(receive(MessageUpdateActivity, type='messageUpdate', channelData=dict(eventType='editMessage'),
                        timestamp='2026-10-24T10:05:00Z', localTimestamp=creation, text='Latest text'))
    with store.connect(config.db_path()) as con:
        row = con.execute('SELECT * FROM messages').fetchone()
        expected = '2026-10-24T10:00:00+00:00' if original_first else None
        assert row['ts'] == expected
    asyncio.run(receive())
    with store.connect(config.db_path()) as con:
        row = con.execute('SELECT * FROM messages').fetchone()
        assert row['ts'] == '2026-10-24T10:00:00+00:00'
        assert row['text'] == 'Latest text'


def test_store_files_are_private_at_creation_and_on_reopen(tmp_path, monkeypatch):
    import os
    import stat
    path = tmp_path / 'state' / 'bot.db'
    connect = sqlite3.connect
    modes = []

    def observe(*args, **kwargs):
        con = connect(*args, **kwargs)
        modes.append(stat.S_IMODE(path.stat().st_mode))
        return con

    monkeypatch.setattr(store.sqlite3, 'connect', observe)
    previous = os.umask(0o022)
    try:
        con = store.connect(path)
        assert modes == [0o600]
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        for suffix in ('', '-wal', '-shm'):
            file = path.with_name(path.name + suffix)
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
            file.chmod(0o644)
        with store.connect(path):
            assert all(stat.S_IMODE(path.with_name(path.name + suffix).stat().st_mode) == 0o600
                       for suffix in ('', '-wal', '-shm'))
        con.close()
    finally:
        os.umask(previous)


def test_guid_case_is_ignored_by_tenant_filter_and_storage(bot, monkeypatch):
    receive, _ = bot
    tenant = 'AAAAAAAA-0000-0000-0000-000000000000'
    sender = 'BBBBBBBB-0000-0000-0000-000000000000'
    monkeypatch.setenv('TOUCAN_BOT_TENANT_ID', tenant)
    # Build the handlers with the uppercase configuration.
    app, _ = server.build()
    payload = dict(id='case', type='message', text='hello',
                   conversation=dict(id='case-chat', tenantId=tenant.lower()),
                   recipient=dict(id='bot'), **{'from': dict(id='alice', aadObjectId=sender, name='Alice')})
    activity = MessageActivity.model_validate(payload)
    async def run():
        for handler in app.router.select_handlers(activity):
            await handler(SimpleNamespace(activity=activity))
    asyncio.run(run())
    with store.connect(config.db_path()) as con:
        assert con.execute("SELECT sender_aad_id FROM messages WHERE id='case'").fetchone()[0] == sender.lower()
        assert con.execute("SELECT tenant_id FROM conversations WHERE id='case-chat'").fetchone()[0] == tenant.lower()
    assert config.load()['tenant_id'] == tenant.lower()


def test_undelete_clears_tombstone_but_old_events_do_not(bot):
    receive, _ = bot
    asyncio.run(receive())
    asyncio.run(receive(MessageDeleteActivity, type='messageDelete', channelData=dict(eventType='softDeleteMessage'),
                        timestamp='2026-10-24T10:02:00Z'))
    asyncio.run(receive(MessageUpdateActivity, type='messageUpdate', channelData=dict(eventType='undeleteMessage'),
                        timestamp='2026-10-24T10:04:00Z', text='Restored'))
    with store.connect(config.db_path()) as con:
        assert con.execute('SELECT deleted FROM messages').fetchone()[0] == 0
    asyncio.run(receive(MessageDeleteActivity, type='messageDelete', channelData=dict(eventType='softDeleteMessage'),
                        timestamp='2026-10-24T10:02:00Z'))
    asyncio.run(receive(MessageUpdateActivity, type='messageUpdate', channelData=dict(eventType='editMessage'),
                        timestamp='2026-10-24T10:03:00Z', text='Stale'))
    with store.connect(config.db_path()) as con:
        row = con.execute('SELECT * FROM messages').fetchone()
        assert row['deleted'] == 0 and row['text'] == 'Restored'


def test_startup_catchup_repeats_and_stops_with_server(bot, monkeypatch):
    import httpx
    from toucan_bot import catchup
    receive, _ = bot
    asyncio.run(receive())
    creds = config.Credentials(ALICE, 'test-secret', tenant_id=TENANT)
    app, web = server.build(creds)
    requests, rosters, sleeps, ready = [], [], [], []
    real_client, real_sleep = httpx.AsyncClient, asyncio.sleep
    async def token(*args):
        return 'test-token'
    async def roster(con, app, chat):
        rosters.append(chat['id'])
    async def fast_sleep(seconds):
        sleeps.append(seconds)
        if seconds == 3600:
            await asyncio.Event().wait()
        else:
            await real_sleep(0)
    def respond(request):
        requests.append(request)
        if len(requests) >= 2:
            ready[0].set()
        return httpx.Response(200, json={'value': [dict(id='recovered', createdDateTime='2026-10-24T10:00:00Z',
            lastModifiedDateTime='2026-10-24T10:00:00Z', body=dict(contentType='text', content='Missed request'),
            **{'from': dict(user=dict(id=ALICE, displayName='Alice Example'))})]})
    monkeypatch.setattr(app.token_provider, 'get_app_token', token)
    monkeypatch.setattr(catchup, 'roster', roster)
    monkeypatch.setattr(server.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(server.asyncio, 'sleep', fast_sleep)
    async def run():
        ready.append(asyncio.Event())
        await app.initialize()
        async with web.router.lifespan_context(web):
            await wait_until(ready[0].is_set)
            assert len(requests) >= 2
        count = len(requests)
        await real_sleep(0)
        assert len(requests) == count
    asyncio.run(run())
    with store.connect(config.db_path()) as con:
        recovered = con.execute("SELECT * FROM messages WHERE id='recovered'").fetchone()
        assert recovered['text'] == 'Missed request' and recovered['first_source'] == 'graph'
    assert rosters == ['chat'] and 120 in sleeps


def test_live_screenshot_uses_connector_auth_and_bot_sender_stays_an_application(bot, monkeypatch):
    import httpx
    app, _ = server.build(config.Credentials(ALICE, 'test-secret', tenant_id=TENANT))
    real_client = httpx.AsyncClient
    requests = []
    async def token(*args):
        assert not args  # default Bot Connector scope, never Graph
        return 'connector-token'
    def respond(request):
        requests.append(request)
        assert request.headers['Authorization'] == 'Bearer connector-token'
        return httpx.Response(200, content=b'image-bytes', headers={'Content-Type': 'image/png'})
    monkeypatch.setattr(app.token_provider, 'get_app_token', token)
    monkeypatch.setattr(server.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    a = MessageActivity.model_validate(dict(id='live-image', type='message', timestamp='2026-10-24T10:00:00Z',
        conversation=dict(id='chat', tenantId=TENANT, conversationType='groupChat'), recipient=dict(id='bot'),
        attachments=[dict(contentType='image/png', contentUrl='https://smba.trafficmanager.net/v3/attachments/image/views/original'),
                     dict(contentType='reference', name='file.pdf', contentUrl='https://example.com/file.pdf')],
        **{'from': dict(id='28:application', role='bot', aadObjectId=ALICE, name='Build app')}))
    async def run():
        for handler in app.router.select_handlers(a):
            await handler(SimpleNamespace(activity=a))
    asyncio.run(run())
    with store.connect(config.db_path()) as con:
        row = con.execute('SELECT * FROM messages').fetchone()
        assert row['sender_aad_id'] is None and row['sender_app_id'] == '28:application'
        attachments = json.loads(row['attachments_json'])
        from pathlib import Path
        assert Path(attachments[0]['local_path']).read_bytes() == b'image-bytes'
        assert attachments[1]['name'] == 'file.pdf' and len(attachments) == 2
    assert len(requests) == 1


@pytest.mark.parametrize('model,event,kind', [
    (MessageUpdateActivity, 'editMessage', 'messageUpdate'),
    (MessageDeleteActivity, 'softDeleteMessage', 'messageDelete'),
    (MessageUpdateActivity, 'undeleteMessage', 'messageUpdate'),
])
def test_mutation_local_timestamp_never_becomes_creation_and_graph_repairs_legacy(bot, model, event, kind):
    from toucan_bot import catchup
    receive, _ = bot
    asyncio.run(receive(model, type=kind, channelData=dict(eventType=event),
                        timestamp='2026-10-24T10:05:00Z', localTimestamp='2026-10-24T15:05:00+05:00'))
    with store.connect(config.db_path()) as con:
        assert con.execute('SELECT ts FROM messages').fetchone()[0] is None
        # Simulate a v2 mutation-derived timestamp already persisted by the old version.
        con.execute("UPDATE messages SET ts='2026-10-24T10:05:00Z'")
        con.commit()
        values = catchup.normalise(dict(id='m1', createdDateTime='2026-01-01T00:00:00Z',
            lastModifiedDateTime='2026-10-24T10:04:00Z', body=dict(contentType='text', content='Older snapshot'),
            **{'from': dict(user=dict(id=ALICE, displayName='Alice Example'))}), 'bot')
        store.record_message(con, conversation_id='chat', **values)
        row = con.execute('SELECT * FROM messages').fetchone()
        assert row['ts'] == '2026-01-01T00:00:00Z'
        assert row['modified_at'] == '2026-10-24T10:05:00+00:00'


def test_notification_only_undelete_preserves_stored_content(bot):
    receive, _ = bot
    asyncio.run(receive(attachments=[dict(contentType='reference', name='brief.pdf', contentUrl='https://example.com/brief.pdf')]))
    asyncio.run(receive(MessageDeleteActivity, type='messageDelete', channelData=dict(eventType='softDeleteMessage'),
                        timestamp='2026-10-24T10:02:00Z', text=None))
    asyncio.run(receive(MessageUpdateActivity, type='messageUpdate', channelData=dict(eventType='undeleteMessage'),
                        timestamp='2026-10-24T10:04:00Z', text=None, attachments=None))
    with store.connect(config.db_path()) as con:
        row = con.execute('SELECT * FROM messages').fetchone()
        assert row['deleted'] == 0
        assert row['text'] == 'Hello & world' and row['raw_html'] == '<p>Hello &amp; <b>world</b></p>'
        assert json.loads(row['attachments_json'])[0]['name'] == 'brief.pdf'


def test_unknown_live_sender_can_be_identified_by_graph_without_becoming_application_context(bot):
    from toucan_bot import catchup
    receive, _ = bot
    asyncio.run(receive(MessageUpdateActivity, type='messageUpdate', channelData=dict(eventType='editMessage'),
                        timestamp='2026-10-24T10:05:00Z',
                        **{'from': dict(id='alice', name='Alice Example')}))
    with store.connect(config.db_path()) as con:
        values = catchup.normalise(dict(id='m1', createdDateTime='2026-10-24T10:00:00Z',
            lastModifiedDateTime='2026-10-24T10:05:00Z', body=dict(contentType='text', content='Recovered'),
            **{'from': dict(user=dict(id=ALICE, displayName='Alice Example'))}), 'bot')
        store.record_message(con, conversation_id='chat', **values)
        row = con.execute('SELECT * FROM messages').fetchone()
        assert row['sender_aad_id'] == ALICE and row['sender_app_id'] is None


@pytest.mark.parametrize('order', ['newer-first', 'older-first', 'newer-fails'])
def test_superseded_roster_cannot_restore_person_alias_after_membership_change(bot, order):
    from microsoft_teams.api import TeamsChannelAccount
    from toucan_bot import catchup
    receive, _ = bot
    owner = '00000000-0000-0000-0000-000000000009'
    bob = '00000000-0000-0000-0000-000000000002'
    config.config_path().write_text(f'owner_teams_id = "{owner}"\n')
    people = [TeamsChannelAccount(id=id, aad_object_id=id, name=name) for id, name in
              [(owner, 'Owner Example'), (ALICE, 'Alice Example'), (bob, 'Bob Example')]]
    with store.connect(config.db_path()) as con:
        asyncio.run(receive())
        for person in people[:2]:
            store.remember_member(con, 'chat', person)
        # The observed activity has a Connector ID different from the roster IDs.
        con.execute('DELETE FROM members WHERE id=?', ('alice',))
        con.execute("INSERT INTO meta VALUES ('roster:chat','confirmed')")
        con.commit()
        assert names.resolve('Alice', ['chat'], names.load_chats()) == 'chat'
        async def run():
            replies = [asyncio.get_running_loop().create_future() for _ in range(2)]
            started = [asyncio.Event(), asyncio.Event()]
            calls = []
            class Members:
                async def get_all(self):
                    index = len(calls)
                    calls.append(index)
                    started[index].set()
                    return await replies[index]
            app = SimpleNamespace(api=SimpleNamespace(from_service_url=lambda url: SimpleNamespace(
                conversations=SimpleNamespace(members=lambda chat: Members()))))
            chat = store.get_conversation(con, 'chat')
            older = asyncio.create_task(catchup.roster(con, app, chat))
            await started[0].wait()
            await receive(ConversationUpdateActivity, type='conversationUpdate',
                          membersAdded=[dict(id=bob, aadObjectId=bob, name='Bob Example')])
            with pytest.raises(names.NotFound):
                names.resolve('Alice', ['chat'], names.load_chats())
            newer = asyncio.create_task(catchup.roster(con, app, chat))
            await started[1].wait()
            if order == 'older-first':
                replies[0].set_result(people[:2])
                await older
                with pytest.raises(names.NotFound):
                    names.resolve('Alice', ['chat'], names.load_chats())
                replies[1].set_result(people)
                await newer
            else:
                if order == 'newer-fails':
                    replies[1].set_exception(TimeoutError())
                else:
                    replies[1].set_result(people)
                await newer
                replies[0].set_result(people[:2])
                await older
            with pytest.raises(names.NotFound):
                names.resolve('Alice', ['chat'], names.load_chats())
            assert {r['aad_id'] for r in con.execute('SELECT aad_id FROM members')} == {owner, ALICE, bob}
        asyncio.run(run())


@pytest.mark.parametrize('fault', ['busy', 'crash'])
def test_background_maintenance_recovers_and_reports_failures(bot, monkeypatch, caplog, capsys, fault):
    import httpx
    from toucan_bot import catchup, cli, media
    receive, _ = bot
    asyncio.run(receive())
    # Keep the old implementation's reproduction fast; preserve a zero busy timeout.
    connect = store.connect
    def short_connect(*args, **kw):
        con = connect(*args, **kw)
        timeout = con.execute('PRAGMA busy_timeout').fetchone()[0]
        con.execute('PRAGMA busy_timeout=' + str(min(timeout, 20)))
        return con
    monkeypatch.setattr(store, 'connect', short_connect)
    app, web = server.build(config.Credentials(ALICE, 'test-secret', tenant_id=TENANT))
    client, sleep, prune = httpx.AsyncClient, asyncio.sleep, media.prune
    requests, sleeps, failures = [], [], []
    def maybe_crash(*args, **kw):
        if fault == 'crash' and not failures:
            failures.append(True)
            raise RuntimeError('test failure')
        return prune(*args, **kw)
    monkeypatch.setattr(media, 'prune', maybe_crash)
    async def token(*args):
        return 'test-token'
    async def roster(*args):
        pass
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'value': []})
    monkeypatch.setattr(app.token_provider, 'get_app_token', token)
    monkeypatch.setattr(catchup, 'roster', roster)
    monkeypatch.setattr(server.httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    async def run():
        failed, retry = asyncio.Event(), asyncio.Event()
        async def pause(seconds):
            sleeps.append(seconds)
            if seconds == 1:
                if sleeps.count(1) >= (2 if fault == 'busy' else 1):
                    failed.set()
                await retry.wait()
            elif seconds == 120:
                await asyncio.Event().wait()
            elif seconds in (60, 3600):
                await asyncio.Event().wait()
            else:
                await sleep(0)
        monkeypatch.setattr(server.asyncio, 'sleep', pause)
        writer = sqlite3.connect(config.db_path(), timeout=0)
        if fault == 'busy':
            writer.execute('BEGIN IMMEDIATE')
        try:
            async with web.router.lifespan_context(web):
                await wait_until(failed.is_set)
                health = await next(r.endpoint for r in web.routes if r.path == '/healthz')()
                assert health['ok'] is False
                if fault == 'crash':
                    cli.main(['status', '--json'])
                    report = json.loads(capsys.readouterr().out)
                    assert report['background']['tasks']['expiry']['last_error'] == 'RuntimeError'
                writer.rollback()
                retry.set()
                health = next(r.endpoint for r in web.routes if r.path == '/healthz')
                async with asyncio.timeout(2):
                    while True:
                        current = (await health())['background']
                        if len(current) == 6 and all(t['last_success'] and not t['last_error'] for t in current.values()):
                            break
                        await sleep(.001)
                assert requests
                cli.main(['status', '--json'])
                report = json.loads(capsys.readouterr().out)
                task = report['background']['tasks']['expiry' if fault == 'crash' else 'catchup']
                assert task['last_success'] and task['last_error'] is None and task['failures'] >= 1
                assert report['background']['ok']
        finally:
            writer.rollback()
            writer.close()
    asyncio.run(run())
    assert any(('catchup' if fault == 'busy' else 'expiry') in r.message and ('OperationalError' if fault == 'busy' else 'RuntimeError') in r.message
               for r in caplog.records)


def test_expiry_worker_keeps_handlers_responsive_and_joins_on_shutdown(bot, monkeypatch):
    import threading
    from pathlib import Path
    receive, _ = bot
    app, web = server.build(config.Credentials(ALICE, 'test-secret', tenant_id=TENANT))
    async def token(*args):
        return None
    monkeypatch.setattr(app.token_provider, 'get_app_token', token)
    glob = Path.glob
    release, finished = threading.Event(), threading.Event()
    threads = []
    async def run():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        def slow_scan(path, pattern):
            if pattern == '*.img':
                threads.append(threading.get_ident())
                loop.call_soon_threadsafe(started.set)
                try:
                    release.wait(2)
                finally:
                    finished.set()
            yield from glob(path, pattern)
        monkeypatch.setattr(Path, 'glob', slow_scan)
        context = web.router.lifespan_context(web)
        closing = None
        await context.__aenter__()
        try:
            await wait_until(started.is_set)
            assert len(threads) == 1 and threads[0] != threading.get_ident()
            await asyncio.wait_for(receive(text='While files are slow'), 0.5)
            with sqlite3.connect(config.db_path()) as con:
                assert con.execute("SELECT text FROM messages WHERE id='m1'").fetchone()[0] == 'While files are slow'
            closing = asyncio.create_task(context.__aexit__(None, None, None))
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await wait_until(closing.done)
            await closing
            assert finished.is_set()
        finally:
            release.set()
            if closing is None:
                closing = asyncio.create_task(context.__aexit__(None, None, None))
            await wait_until(closing.done)
            await closing
    asyncio.run(run())


def test_persistent_expiry_failure_does_not_stop_catchup_media_or_rosters(bot, monkeypatch, capsys):
    import httpx
    import os
    import time
    from pathlib import Path
    from toucan_bot import cli
    receive, _ = bot
    image = 'https://smba.trafficmanager.net/v3/attachments/image/views/original'
    asyncio.run(receive(attachments=[dict(contentType='image/png', contentUrl=image)]))
    directory = config.state_dir() / 'media'
    directory.mkdir()
    expired = directory / 'expired.img'
    expired.write_bytes(b'expired')
    os.utime(expired, (time.time() - 31 * 86400,) * 2)
    app, web = server.build(config.Credentials(ALICE, 'test-secret', tenant_id=TENANT))
    attempts, lists = [], []
    unlink, client, sleep = Path.unlink, httpx.AsyncClient, asyncio.sleep
    def denied(path, *args, **kw):
        if path == expired:
            attempts.append(True)
            raise PermissionError('expiry denied')
        return unlink(path, *args, **kw)
    async def token(*args):
        return 'test-token'
    async def members():
        return [SimpleNamespace(id=ALICE, aad_object_id=ALICE, name='Alice Example', role=None)]
    def respond(request):
        if '/chats/' in request.url.path:
            lists.append(request)
            if len(lists) == 1:
                # Require a later scheduled pass, even when the first pass returns
                # normally after recording a per-chat failure. (Authorization
                # failures back off for longer; see test_catchup.)
                return httpx.Response(500)
            return httpx.Response(200, json={'value': [dict(id='recovered', createdDateTime='2026-10-24T10:00:00Z',
                lastModifiedDateTime='2026-10-24T10:00:00Z', body=dict(contentType='text', content='Recovered'),
                **{'from': dict(user=dict(id=ALICE, displayName='Alice Example'))})]})
        assert str(request.url) == image
        return httpx.Response(200, content=b'live-image', headers={'Content-Type': 'image/png'})
    monkeypatch.setattr(Path, 'unlink', denied)
    monkeypatch.setattr(app.token_provider, 'get_app_token', token)
    monkeypatch.setattr(app.api, 'from_service_url', lambda url: SimpleNamespace(conversations=SimpleNamespace(
        members=lambda chat: SimpleNamespace(get_all=members))))
    monkeypatch.setattr(server.httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    healthy = ('catchup', 'media', 'graph_media', 'roster', 'heartbeat')
    def completed():
        with sqlite3.connect(config.db_path()) as con:
            con.execute('BEGIN')
            recovered = con.execute("SELECT text FROM messages WHERE id='recovered'").fetchone()
            attachment = json.loads(con.execute("SELECT attachments_json FROM messages WHERE id='m1'").fetchone()[0])[0]
            confirmed = con.execute("SELECT value FROM meta WHERE key='roster:chat'").fetchone()
            tasks = {key.removeprefix('task:'): json.loads(value)
                     for key, value in con.execute("SELECT key,value FROM meta WHERE key LIKE 'task:%'")}
        expiry = tasks.get('expiry', {})
        return (len(attempts) >= 2 and recovered and attachment.get('download_status') == 'available' and confirmed
                and expiry.get('last_error') == 'PermissionError' and expiry.get('failures', 0) >= 2
                and all(tasks.get(name, {}).get('last_success') and not tasks[name]['last_error'] for name in healthy))
    async def run():
        async def pause(seconds):
            await sleep(.01)
        monkeypatch.setattr(server.asyncio, 'sleep', pause)
        async with web.router.lifespan_context(web):
            await wait_until(completed)
            cli.main(['status', '--json'])
            tasks = json.loads(capsys.readouterr().out)['background']['tasks']
            assert tasks['expiry']['last_error'] == 'PermissionError' and tasks['expiry']['failures'] >= 2
            for name in healthy:
                assert tasks[name]['last_success'] and tasks[name]['last_error'] is None
    asyncio.run(run())
    assert expired.exists() and len(lists) >= 2


@pytest.mark.parametrize('operation', ['mkdir', 'chmod', 'replace'])
def test_graph_media_directory_failure_does_not_block_catchup(bot, monkeypatch, capsys, operation):
    import httpx
    import os
    from pathlib import Path
    from toucan_bot import catchup, cli, media
    receive, _ = bot
    asyncio.run(receive())
    image = catchup.message_base('chat', 'm1') + '/hostedContents/image/$value'
    with store.connect(config.db_path()) as con:
        store.record_message(con, conversation_id='chat', **catchup.normalise(dict(id='m1',
            createdDateTime='2026-10-24T10:00:00Z', lastModifiedDateTime='2026-10-24T10:00:00Z',
            body=dict(contentType='html', content=f'<img src="{image}">'),
            attachments=[dict(contentType='image/png', contentUrl=image)]), 'bot'))
        media.queue(con, 'chat', 'm1', 'graph')
    app, web = server.build(config.Credentials(ALICE, 'test-secret', tenant_id=TENANT))
    directory = config.state_dir() / 'media'
    filesystem_api = os if operation == 'replace' else Path
    original, client, sleep = getattr(filesystem_api, operation), httpx.AsyncClient, asyncio.sleep
    denied = [True]
    def filesystem(path, *args, **kw):
        target = Path(args[0]).parent if operation == 'replace' else path
        if target == directory and denied[0]:
            raise PermissionError('image directory denied')
        return original(path, *args, **kw)
    async def token(*args):
        return 'test-token'
    async def members():
        return []
    def respond(request):
        if '/hostedContents/' in request.url.path:
            return httpx.Response(200, content=b'recovered-image', headers={'Content-Type': 'image/png'})
        return httpx.Response(200, json={'value': [dict(id='recovered', createdDateTime='2026-10-24T10:00:00Z',
            body=dict(contentType='text', content='Recovered despite image failure'))]})
    monkeypatch.setattr(filesystem_api, operation, filesystem)
    monkeypatch.setattr(app.token_provider, 'get_app_token', token)
    monkeypatch.setattr(app.api, 'from_service_url', lambda url: SimpleNamespace(conversations=SimpleNamespace(
        members=lambda chat: SimpleNamespace(get_all=members))))
    monkeypatch.setattr(server.httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    def state():
        with sqlite3.connect(config.db_path()) as con:
            recovered = con.execute("SELECT 1 FROM messages WHERE id='recovered'").fetchone()
            item = json.loads(con.execute("SELECT attachments_json FROM messages WHERE id='m1'").fetchone()[0])[0]
            return bool(recovered), item, con.execute('SELECT attempts,next_attempt FROM pending_media').fetchone()
    async def run():
        resume = asyncio.Event()
        async def pause(seconds):
            if seconds >= 60:
                await resume.wait()
            await sleep(.001)
        monkeypatch.setattr(server.asyncio, 'sleep', pause)
        async with web.router.lifespan_context(web):
            await wait_until(lambda: state()[0] and state()[1].get('download_status') == 'pending')
            _, item, job = state()
            assert item['download_error'] == 'PermissionError' and job[0] == 1 and job[1] > media.time.time()
            cli.main(['status', '--json'])
            tasks = json.loads(capsys.readouterr().out)['background']['tasks']
            assert tasks['catchup']['last_success'] and not tasks['catchup']['last_error']
            denied[0] = False
            with sqlite3.connect(config.db_path()) as con:
                con.execute('UPDATE pending_media SET next_attempt=0')
            resume.set()
            await wait_until(lambda: state()[1].get('download_status') == 'available')
            assert Path(state()[1]['local_path']).read_bytes() == b'recovered-image'
    asyncio.run(run())


def test_screenshots_expire_after_credentials_are_removed(bot):
    import os
    import time
    from pathlib import Path
    receive, _ = bot
    asyncio.run(receive())
    directory = config.state_dir() / 'media'
    directory.mkdir()
    path = directory / 'expired.img'
    path.write_bytes(b'private-screenshot')
    os.utime(path, (time.time() - 31 * 86400,) * 2)
    with store.connect(config.db_path()) as con:
        con.execute('UPDATE messages SET attachments_json=?', (json.dumps([
            dict(contentType='image/png', download_status='available', local_path=str(path))]),))
    config.save_credentials(config.Credentials(ALICE, 'test-secret', tenant_id=TENANT))
    config.clear_credentials()
    _, web = server.build()
    def expired():
        with sqlite3.connect(config.db_path()) as con:
            con.execute('BEGIN')
            item = json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]
            row = con.execute("SELECT value FROM meta WHERE key='task:expiry'").fetchone()
        task = json.loads(row[0]) if row else {}
        return item.get('download_status') == 'expired' and task.get('last_success') and not task['last_error']
    async def run():
        async with web.router.lifespan_context(web):
            await wait_until(expired)
    asyncio.run(run())
    assert not path.exists()
    with sqlite3.connect(config.db_path()) as con:
        assert 'local_path' not in json.loads(con.execute('SELECT attachments_json FROM messages').fetchone()[0])[0]


def test_roster_failure_reports_status_retries_and_finishes_other_chats(bot, monkeypatch, capsys):
    import httpx
    from toucan_bot import cli
    receive, _ = bot
    asyncio.run(receive())
    with store.connect(config.db_path()) as con:
        store.upsert_conversation(con, id='other', type='groupChat', name=None,
                                  service_url='https://smba.example.com/region', tenant_id=TENANT)
        con.execute("UPDATE conversations SET last_seen='9999' WHERE id='chat'")
    app, web = server.build(config.Credentials(ALICE, 'test-secret', tenant_id=TENANT))
    calls, fail = [], [True]
    client, sleep = httpx.AsyncClient, asyncio.sleep
    async def token(*args):
        return 'test-token'
    class Members:
        def __init__(self, chat):
            self.chat = chat
        async def get_all(self):
            calls.append(self.chat)
            if self.chat == 'chat' and fail[0]:
                response = httpx.Response(401, request=httpx.Request('GET', 'https://smba.example.com/members'))
                response.raise_for_status()
            return [SimpleNamespace(id=ALICE, aad_object_id=ALICE, name='Alice Example', role=None)]
    monkeypatch.setattr(app.token_provider, 'get_app_token', token)
    monkeypatch.setattr(app.api, 'from_service_url', lambda url: SimpleNamespace(conversations=SimpleNamespace(members=Members)))
    monkeypatch.setattr(server.httpx, 'AsyncClient', lambda **kw: client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'value': []})), **kw))
    async def run():
        retry, failed = asyncio.Event(), asyncio.Event()
        async def pause(seconds):
            if seconds == 1:
                failed.set()
                await retry.wait()
            elif seconds >= 60:
                await asyncio.Event().wait()
            else:
                await sleep(.001)
        monkeypatch.setattr(server.asyncio, 'sleep', pause)
        async with web.router.lifespan_context(web):
            await wait_until(lambda: len(calls) >= 2)
            cli.main(['status', '--json'])
            report = json.loads(capsys.readouterr().out)
            assert report['roster']['errors'] == {'chat': 'HTTP 401'} and not report['roster']['ok']
            await wait_until(failed.is_set)
            task = report['background']['tasks']['roster']
            assert task['last_error'] and task['failures'] == 1
            assert calls[:2] == ['chat', 'other']
            with sqlite3.connect(config.db_path()) as con:
                assert con.execute("SELECT 1 FROM meta WHERE key='roster:other'").fetchone()
                assert not con.execute("SELECT 1 FROM meta WHERE key='roster:chat'").fetchone()
            fail[0] = False
            retry.set()
            def recovered():
                with sqlite3.connect(config.db_path()) as con:
                    task = con.execute("SELECT value FROM meta WHERE key='task:roster'").fetchone()
                return task and json.loads(task[0])['last_success']
            await wait_until(recovered)
            cli.main(['status', '--json'])
            report = json.loads(capsys.readouterr().out)
            assert report['roster'] == dict(errors={}, ok=True)
            assert not report['background']['tasks']['roster']['last_error']
    asyncio.run(run())
