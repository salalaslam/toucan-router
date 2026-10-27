import json
import os
import sqlite3

import pytest

from toucan.config import load
from toucan.core import App
from toucan.storage import connect
from conftest import LID, PHONE


def test_first_sync_and_active_from_exclude_history(world):
    world.wa('history', ts=world.now - 10)
    world.team('history-team', ts=world.now - 10)
    world.seed()
    assert len(world.app.rows('SELECT * FROM messages')) == 2
    world.wa('late-history', ts=world.now - 100)
    world.cfg['routes'][0]['active_ts'] = world.now + 10
    world.wa('before-active')
    assert world.app.sync()['created'] == []
    world.advance(11)
    req = world.request('after-active')
    assert world.app.get(req)['state'] == 'new'
    assert len(world.hooks()) == 1
    assert world.hooks()[0]['count'] == 1


def test_identity_mapping_context_reactions_and_owner_messages(world):
    world.seed()
    world.wa('mapped', sender=LID, chat_jid=LID)
    world.wa('unknown', sender='999999@lid')
    world.wa('outsider', sender='15550000003@s.whatsapp.net')
    world.wa('assistant', sender=None, from_me=1)
    world.wa('reaction-emoji', reaction_emoji='👍')
    world.wa('reaction-target', reaction_to_id='mapped', text='')
    world.wa('tombstone', revoked=1, text='')
    world.wa('placeholder', text='', display_text='(message)')
    world.team('spoof', sender='unapproved-entra')
    world.team('owner', sender='owner-entra')
    created = world.app.sync()['created']
    assert len(created) == 1
    req = world.app.get(created[0])
    assert req['reply_chat'] == PHONE and req['reply_person'] == 'alice'
    messages = {m['msg_id']: m for m in world.app.rows('SELECT * FROM messages')}
    assert set(messages) == {'mapped', 'unknown', 'outsider', 'assistant', 'spoof', 'owner'}
    assert messages['assistant']['sender_key'] == 'toucan'
    assert messages['assistant']['is_owner'] == messages['owner']['is_owner'] == 1
    assert messages['spoof']['sender_key'] is None
    assert len([a for a in world.alerts() if a['key'].startswith('identity:')]) == 1
    world.app.sync()
    notifications = [p for p in world.posts if p[0].startswith('http://127.')]
    assert len(notifications) == 1


@pytest.mark.parametrize('teams', [False, True])
def test_new_outbound_messages_are_context_and_do_not_close_unrelated_requests(world, teams):
    route = next(r for r in world.cfg['routes'] if r['name'] == ('group' if teams else 'alice-whatsapp'))
    if teams:
        route['post_as_bot'] = route['owner_reply_closes'] = True
    world.seed()
    req = world.request(teams=teams)
    # A draft on an existing request must not be acknowledged by a new bot message either.
    if teams:
        world.cfg['people']['alice']['whatsapp'] = []
        route['post_as_bot'] = False
        world.app.deliver(req, world.app.claim([req], 'project-a'), text='Draft answer')
        route['post_as_bot'] = True
    state = world.app.get(req)['state']
    world.advance(1)
    world.app.send(route['name'], 'project-a', text='A separate message', chat=route['chats'][0])
    if teams:
        world.team('sent-id', sender=None)
    else:
        world.wa('sent-id', sender=None, from_me=1)
    assert world.app.sync()['created'] == []
    assert world.app.get(req)['state'] == state
    message = next(m for m in world.app.inbox('project-a') if m['msg_id'] == 'sent-id')
    assert message['request_id'] is None
    if teams:
        world.advance(1)
        world.team('owner-posted', sender='owner-entra')
        world.app.sync()
        assert world.app.get(req)['state'] == 'closed'


def test_teams_latest_version_and_each_request_has_own_audience(world):
    world.seed()
    world.team('a')
    world.team('b', sender='other-entra')
    world.team('a', text='Changed & clear',
               edited_at='2026-10-17T12:00:01+05:00', attachments_json=json.dumps([{'name': 'Brief.pdf', 'contentUrl': 'https://files.invalid/a'}]))
    reqs = world.app.sync()['created']
    assert {world.app.get(r)['reply_person'] for r in reqs} == {'alice', 'other'}
    assert {world.app.get(r)['reply_chat'] for r in reqs} == {'group:chat'}
    inbox = world.app.inbox('project-a')
    assert len(inbox) == 2
    a = next(m for m in inbox if m['msg_id'] == 'a')
    assert a['text'] == 'Changed & clear'
    assert a['edited'] == 1
    assert 'Brief.pdf' in a['attachments_json']
    assert world.app.rows("SELECT * FROM events WHERE kind='inbox'")


def test_old_edits_and_deletions_rewake_once_and_preserve_original_text(world):
    world.seed()
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    world.advance(49 * 3600)  # older than the normal lookback
    with sqlite3.connect(world.wa_path) as db:
        db.execute("UPDATE messages SET text='Changed request',edited=1 WHERE msg_id='req'")
    assert len(world.app.sync()['wakes']) == 1
    assert world.app.get(req)['source_changed'] == 1
    assert world.app.get(req)['claim_id'] == claim
    assert world.app.sync()['wakes'] == []
    with sqlite3.connect(world.wa_path) as db:
        db.execute("UPDATE messages SET text='',display_text='',revoked=1 WHERE msg_id='req'")
    assert world.app.sync()['wakes'] == []
    world.advance(15 * 60)
    assert len(world.app.sync()['wakes']) == 1
    assert world.app.sync()['wakes'] == []
    assert len(world.app.rows("SELECT * FROM events WHERE kind='source_changed'")) == 2
    assert world.app.inbox('project-a')[0]['text'] == 'Changed request'
    assert world.app.inbox('project-a')[0]['deleted'] == 1


def test_teams_edit_and_delete_are_not_lost_outside_lookback(world):
    world.seed()
    req = world.request(teams=True)
    world.app.claim([req], 'project-a')
    original = world.now
    world.advance(49 * 3600)
    world.team('req', ts=original, text='edited', edited_at='2026-10-19T12:00:00+05:00')
    world.app.sync()
    world.team('req', ts=original, text='', deleted=1)
    world.advance(15 * 60)
    world.app.sync()
    assert world.app.get(req)['source_changed'] == 1
    assert world.app.inbox('project-a')[0]['deleted'] == 1
    assert len(world.hooks()) == 3


def test_rewakes_stop_at_limit_and_unclaimed_alert_once(world):
    world.seed()
    req = world.request()
    world.app.sync()
    assert len(world.hooks()) == 1
    for i in range(2):
        world.advance(15 * 60)
        world.app.sync()
    assert world.app.get(req)['wakes'] == 3
    assert len(world.hooks()) == 3
    world.advance(15 * 60)
    world.app.sync()
    assert len(world.hooks()) == 3
    assert {a['key'] for a in world.alerts()} == {'unclaimed:project-a', f'unacked:{req}'}
    assert len([p for p in world.posts if p[0].startswith('http://127.')]) == 2
    world.app.claim([req], 'project-a')
    world.app.sync()
    assert world.alerts() == []


LIMIT = 'API Error: Request rejected (429) · All credentials for model claude-opus-5-5 are cooling down via provider claude'


def test_rate_limited_thread_is_woken_to_carry_on_without_using_its_wake_budget(world):
    world.seed()
    world.t3('note', world.now, 'Progress: 40 of 90 done, no rate limit so far.', role='assistant')
    world.app.sync()
    assert world.alerts() == []
    world.t3('error-1', world.now, LIMIT, role='assistant')
    world.app.sync()
    assert [a['key'] for a in world.alerts()] == ['limited:project-a'] and world.hooks() == []
    world.advance(15 * 60)
    world.app.sync()
    assert [(h['count'], 'carry on' in h['summary']) for h in world.hooks()] == [(0, True)]
    req = world.request()
    for i in range(4):
        world.t3(f'error-{i + 2}', world.now, LIMIT, role='assistant')  # each woken turn fails again
        world.advance(15 * 60)
        world.app.sync()
    assert world.app.get(req)['wakes'] == 0
    assert [h['count'] for h in world.hooks()] == [0, 1, 1, 1, 1]
    assert [a['key'] for a in world.alerts()] == ['limited:project-a']
    world.t3('continue', world.now, 'Continue')
    world.app.sync()
    assert world.alerts() == []


def test_requests_move_to_the_fallback_thread_while_the_owner_is_rate_limited(world):
    backup_hook = world.root / 'backup.hook'
    backup_hook.write_text('https://hook.invalid/api/hooks/backup/BACKUP')
    backup_hook.chmod(0o600)
    world.config_path.write_text(world.config_text.replace('[threads.project-a]\n', '[threads.project-a]\nfallback = "backup"\n') +
                                 f'\n[threads.backup]\nt3_thread = "backup-thread"\nhook_file = "{backup_hook}"\n')
    world.app = App(load(world.config_path), world.db, world.fx, world.paths)
    world.seed()
    world.t3('error', world.now, LIMIT, role='assistant')
    req = world.request()
    world.advance(4 * 60)
    world.app.sync()
    assert world.app.get(req)['thread'] == 'project-a'
    world.advance(60)
    world.app.sync()
    assert world.app.get(req)['thread'] == 'backup' and world.hooks()[-1]['thread'] == 'backup'
    assert [m['request_id'] for m in world.app.inbox('backup') if m['request_id']] == [req]
    world.app.deliver(req, world.app.claim([req], 'backup'), text='Answer')
    assert world.sends[-1][5] == PHONE and world.app.get(req)['state'] == 'closed'


def test_request_not_picked_up_within_ack_window_alerts_owner(world):
    world.seed()
    req = world.request()
    world.advance(4 * 60)
    world.app.sync()
    assert world.alerts() == []
    world.advance(60)
    world.app.sync()
    assert [a['key'] for a in world.alerts()] == [f'unacked:{req}']
    assert 'PRIVATE MESSAGE' not in world.alerts()[0]['text']
    world.app.claim([req], 'project-a')
    world.app.sync()
    assert world.alerts() == []


def test_webhook_payload_has_only_counts_times_and_failure_keeps_requests(world):
    world.seed()
    world.post_status = RuntimeError('https://hook.invalid/SECRET-TOKEN')
    req = world.request()
    world.app.sync()
    assert world.app.get(req)['state'] == 'new'
    assert world.app.get(req)['wakes'] == 0
    assert len(world.hooks()) == 2
    assert set(world.hooks()[0]) == {'thread', 'summary', 'count'}
    assert 'UTC' in world.hooks()[0]['summary']
    assert 'PRIVATE MESSAGE' not in json.dumps(world.hooks())
    assert len([p for p in world.posts if p[0].startswith('http://127.')]) == 1
    assert 'SECRET-TOKEN' not in json.dumps(world.alerts() + world.app.rows('SELECT * FROM events'))
    world.post_status = 200
    world.app.sync()
    assert world.app.get(req)['wakes'] == 1
    assert world.alerts() == []


@pytest.mark.parametrize('permissions', [None, 0o644])
def test_bad_hook_file_blocks_wake_but_retains_requests(world, permissions):
    world.seed()
    if permissions is None:
        world.hook.unlink()
    else:
        world.hook.chmod(permissions)
    req = world.request()
    assert world.hooks() == []
    assert world.app.get(req)['wakes'] == 0
    assert world.alerts()[0]['key'] == 'hook:project-a'


def test_claimed_and_done_alerts_and_recovery(world):
    world.seed()
    first = world.request('first')
    second = world.request('second')
    claim = world.app.claim([first, second], 'project-a')
    world.app.transition(second, claim, 'done', 'Work finished')
    world.advance(4 * 3600 + 1)
    world.app.sync()
    assert {a['key'] for a in world.alerts()} == {f'claimed:{first}', f'done:{second}'}
    world.app.sync()
    assert len([p for p in world.posts if p[0].startswith('http://127.')]) == 2
    world.app.transition(first, claim, 'close', 'No action')
    world.app.transition(second, claim, 'close', 'Owner replied')
    world.app.sync()
    assert world.alerts() == []


def test_promise_alerts_when_overdue_instead_of_as_a_stale_claim(world):
    world.seed()
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    world.app.deliver(req, claim, text='Will do', promise='send the test results', due=world.now + 6 * 3600)
    world.advance(4 * 3600 + 1)
    world.app.sync()
    assert world.alerts() == []
    world.advance(2 * 3600)
    world.app.sync()
    assert [a['text'] for a in world.alerts()] == [
        f'project-a: request {req} has an overdue promise due 2026-10-17 13:00 UTC; still open']
    world.app.deliver(req, claim, text='Results attached')
    world.app.sync()
    assert world.alerts() == []


def test_health_alert_streak_and_locked_database_do_not_seed_route(world):
    os.utime(world.paths.wa / 'HEARTBEAT', (world.now - 601, world.now - 601))
    lock = sqlite3.connect(world.paths.wa / 'session.db', isolation_level=None)
    lock.execute('BEGIN EXCLUSIVE')
    try:
        report = world.app.sync()
        assert 'health:lid_map' in report['problems']
    finally:
        lock.rollback()
        lock.close()
    world.freshen()
    world.app.sync()
    assert world.alerts() == []
    # A missing archive must not silently complete initial seeding.
    world.db.execute("DELETE FROM meta WHERE key='seed:alice-whatsapp'")
    saved = world.wa_path.with_suffix('.saved')
    world.wa_path.rename(saved)
    try:
        result = world.app.sync()
        assert 'health:route:alice-whatsapp' in result['problems']
        assert not world.db.execute("SELECT 1 FROM meta WHERE key='seed:alice-whatsapp'").fetchone()
    finally:
        saved.rename(world.wa_path)


def test_dry_run_copies_state_without_writing_or_sending(world):
    world.seed()
    world.wa('new')
    before = list(world.db.iterdump())
    calls = len(world.posts)
    dry_db = connect(world.db_path, dry=True)
    try:
        result = App(world.cfg, dry_db, world.fx, world.paths, dry=True).sync()
        assert len(result['created']) == 1 and len(result['wakes']) == 1
    finally:
        dry_db.close()
    assert list(world.db.iterdump()) == before
    assert len(world.posts) == calls and not world.sends
    absent = world.root / 'absent-parent' / 'db'
    dry_db = connect(absent, dry=True)
    dry_db.close()
    assert not absent.parent.exists()


def test_new_requests_share_thread_wake_cooldown(world):
    world.seed()
    first = world.request('first')
    world.advance(1)
    second = world.request('second', teams=True)
    assert len(world.hooks()) == 1
    assert world.app.get(second)['wakes'] == 0
    world.advance(899)
    world.app.sync()
    assert len(world.hooks()) == 2
    assert world.hooks()[-1]['count'] == 2
    assert world.app.get(first)['wakes'] == 2
    assert world.app.get(second)['wakes'] == 1


def test_locked_identity_map_defers_ingestion_until_recovery(world):
    world.seed()
    world.wa('pending-lid', sender=LID, chat_jid=LID)
    lock = sqlite3.connect(world.paths.wa / 'session.db', isolation_level=None)
    lock.execute('BEGIN EXCLUSIVE')
    try:
        report = world.app.sync()
        assert 'health:lid_map' in report['problems']
        assert report['created'] == []
        assert world.app.rows('SELECT * FROM messages') == []
    finally:
        lock.rollback()
        lock.close()
    report = world.app.sync()
    assert len(report['created']) == 1
    assert world.app.get(report['created'][0])['reply_chat'] == PHONE
    assert world.alerts() == []


def test_service_failure_alerts_once_and_clears_on_recovery(world):
    import subprocess
    world.seed()
    healthy = world.fx.run
    world.fx.run = lambda argv: subprocess.CompletedProcess(argv, 1, '', '')
    world.app.sync()
    world.app.sync()
    assert {a['key'] for a in world.alerts()} == {'health:whatsapp:service', 'health:teams:service'}
    assert len([p for p in world.posts if p[0].startswith('http://127.')]) == 2
    world.fx.run = healthy
    world.app.sync()
    assert world.alerts() == []


def test_media_attachments_are_context_and_open_request_inbox_survives_age(world):
    world.seed()
    world.wa('media', text='', media_type='document', filename='brief.pdf', local_path='/archive/brief.pdf')
    req = world.app.sync()['created'][0]
    world.advance(25 * 3600)
    inbox = world.app.inbox('project-a')
    assert len(inbox) == 1 and inbox[0]['request_id'] == req
    assert json.loads(inbox[0]['attachments_json']) == [
        {'media_type': 'document', 'filename': 'brief.pdf', 'local_path': '/archive/brief.pdf'}]


def test_new_request_after_short_gap_wakes_without_waiting_for_rewake(world):
    world.seed()
    first = world.request('first')
    world.advance(30)
    second = world.request('second', teams=True)
    assert len(world.hooks()) == 1  # burst: batched
    world.advance(120)
    world.app.sync()
    assert len(world.hooks()) == 2
    assert world.hooks()[-1]['count'] == 1
    assert world.app.get(second)['wakes'] == 1
    assert world.app.get(first)['wakes'] == 1


def test_shadow_mode_tracks_requests_but_posts_nothing(world):
    world.seed()
    world.app.shadow = True
    req = world.request()
    assert world.app.get(req)['wakes'] == 1
    assert world.posts == []
    assert world.app.rows("SELECT * FROM events WHERE kind='wake'")[0]['detail_json'] == '{"count": 1, "shadow": true}'


@pytest.mark.parametrize('embedded', [False, True, 'messageReference', 'forwardedMessageReference'])
def test_teams_quoted_reply_is_in_the_untrusted_inbox(world, embedded):
    from argparse import Namespace
    from toucan.cli import dispatch
    world.seed()
    world.team('quoted', sender='unapproved', text='Original context')
    entities = [{'type': 'quotedReply', 'quotedReply': {'messageId': 'quoted', 'preview': 'Quoted preview'}}]
    attachments = []
    if isinstance(embedded, str):
        field = 'messagePreview' if embedded == 'messageReference' else 'originalMessageContent'
        attachments = [dict(contentType=embedded, content=json.dumps({field: '<p>Quoted preview</p>'}))]
    world.team('reply', text='Please check', reply_to_id='quoted',
               quoted_json=json.dumps(entities) if embedded is True else '[]', attachments_json=json.dumps(attachments))
    world.app.sync()
    text = next(m for m in world.app.inbox('project-a') if m['msg_id'] == 'reply')['text']
    assert 'Quote:\n> ' + ('Quoted preview' if embedded else 'Original context') in text
    output = dispatch(Namespace(command='inbox', thread='project-a', json=False), world.app)
    block = next(b for b in output.split('----- END -----') if 'Please check' in b)
    assert 'UNTRUSTED CONTENT' in block and '| > ' in block


def test_unknown_teams_creation_is_context_but_tracked_edits_still_update(world):
    world.seed()
    world.team('unknown', ts=world.now, edited_at='2026-10-17T07:01:00Z')
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("UPDATE messages SET ts=NULL WHERE id='unknown'")
    assert world.app.sync()['created'] == []
    assert any(m['msg_id'] == 'unknown' and m['request_id'] is None for m in world.app.inbox('project-a'))
    assert world.app.import_pending('group', world.now - 3600) == []
    req = world.request('tracked', teams=True)
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("UPDATE messages SET ts=NULL,text='Changed text',edited_at='2026-10-17T07:02:00Z' WHERE id='tracked'")
    world.app.sync()
    assert world.app.get(req)['source_changed'] == 1
    assert next(m for m in world.app.inbox('project-a') if m['request_id'] == req)['text'] == 'Changed text'


def test_teams_guids_normalize_config_and_archive_read(world):
    from toucan.config import load
    owner = 'AAAAAAAA-0000-0000-0000-000000000000'
    alice = 'BBBBBBBB-0000-0000-0000-000000000000'
    world.config_path.write_text(world.config_text.replace('owner-entra', owner).replace(
        '00000000-0000-0000-0000-000000000001', alice))
    cfg = load(world.config_path)
    assert cfg['settings']['owner_teams_id'] == owner.lower()
    assert cfg['people']['alice']['teams_id'] == alice.lower()
    world.cfg['settings'].update(cfg['settings'])
    world.cfg['people'].update(cfg['people'])
    world.seed()
    world.team('alice', sender=alice.lower())
    world.team('owner', sender=owner)
    assert len(world.app.sync()['created']) == 1
    inbox = {m['msg_id']: m for m in world.app.inbox('project-a')}
    assert inbox['alice']['sender_key'] == 'alice'
    assert inbox['owner']['is_owner'] == 1 and inbox['owner']['sender_raw'] == owner.lower()


def test_teams_application_context_and_file_screenshot_details_reach_inbox(world):
    import json
    from toucan.cli import dispatch, parser
    world.seed()
    # A compromised app must never impersonate the allowlisted human's AAD ID.
    world.team('application', sender_app_id='application', sender_name='Build app',
               text='Build passed', attachments_json=json.dumps([
                   dict(contentType='application/vnd.microsoft.card.adaptive', content={'body': [{'text': 'Build passed'}]})]))
    world.team('files', attachments_json=json.dumps([
        dict(contentType='reference', name='brief.pdf', contentUrl='https://example.com/brief.pdf'),
        dict(contentType='image/png', local_path='/example/media/screenshot.img')]))
    made = world.app.sync()['created']
    assert len(made) == 1
    inbox = world.app.inbox('project-a')
    application = next(m for m in inbox if m['msg_id'] == 'application')
    assert application['request_id'] is None and application['sender_raw'] is None
    assert '[Application context: Build app]' in application['text']
    file_message = next(m for m in inbox if m['msg_id'] == 'files')
    file = json.loads(file_message['attachments_json'])[0]
    assert file['name'] == 'brief.pdf' and file['contentUrl'] == 'https://example.com/brief.pdf'
    assert "owner's own Microsoft login" in file['access_note']
    args = parser().parse_args(['inbox', '--thread', 'project-a'])
    rendered = dispatch(args, world.app)
    assert 'File: brief.pdf https://example.com/brief.pdf' in rendered
    assert 'Screenshot: /example/media/screenshot.img' in rendered


@pytest.mark.parametrize('eligible', [True, 'before-seed', 'before-active', 'seed-context'])
def test_recovered_teams_identity_and_creation_promote_only_deferred_eligible_once(world, eligible):
    if eligible != 'seed-context':
        world.seed()
    creation = world.now
    if eligible == 'before-seed':
        creation -= 60
    if eligible == 'before-active':
        route = next(r for r in world.cfg['routes'] if r['source'] == 'teams')
        route['active_ts'] = world.now + 10
    world.team('deferred', sender=None)
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("UPDATE messages SET ts=NULL WHERE id='deferred'")
    assert world.app.sync()['created'] == []
    # Reopen router state too: eligibility must not live only in memory.
    world.db.close()
    from toucan.storage import connect
    from toucan.core import App
    world.db = connect(world.db_path)
    world.app = App(world.cfg, world.db, world.fx, world.paths)
    if eligible is True:
        world.advance(3 * 86400)  # Recovery must still find deferred rows beyond the normal lookback.
    world.team('deferred', ts=creation, sender=None)
    assert world.app.sync()['created'] == []
    world.team('deferred', ts=creation)
    created = world.app.sync()['created']
    assert len(created) == int(eligible is True)
    assert world.app.sync()['created'] == []
    row = next(m for m in world.app.inbox('project-a') if m['msg_id'] == 'deferred')
    assert row['sender_key'] == 'alice'
    assert bool(row['request_id']) == (eligible is True)



def test_teams_message_and_quote_use_one_source_snapshot(world, monkeypatch):
    from toucan import sources
    world.team('parent', text='Before quote')
    world.team('child', text='Before reply', reply_to_id='parent')
    with sqlite3.connect(world.paths.bot_db) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        connect = sqlite3.connect
        changed = []
        class Cursor(sqlite3.Cursor):
            def fetchall(self):
                rows = super().fetchall()
                if self.interleave and not changed:
                    changed.append(True)
                    with writer:
                        writer.execute("UPDATE messages SET text='After quote' WHERE id='parent'")
                        writer.execute("UPDATE messages SET text='After reply' WHERE id='child'")
                return rows
        class Connection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                cursor = self.cursor(factory=Cursor)
                cursor.interleave = sql.startswith('SELECT * FROM messages WHERE conversation_id=')
                return cursor.execute(sql, parameters)
        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, **kw, factory=Connection))
        rows = sources.teams(world.paths, 'group:chat', world.now - 60, [], '')
    child = next(row for row in rows if row['msg_id'] == 'child')
    assert changed and child['text'] == 'Before reply\n\nQuote:\n> Before quote'


@pytest.mark.parametrize('eligibility', ['eligible', 'before-seed', 'before-active', 'seed-context'])
def test_newly_ingested_teams_history_bypasses_lookback_but_keeps_cutoffs(world, eligibility):
    from toucan.core import App
    from toucan.storage import connect
    creation = world.now
    if eligibility == 'seed-context':
        creation -= 3 * 86400
        world.team('recovered', ts=creation)
    world.seed()
    if eligibility == 'eligible':
        creation = world.now
    if eligibility == 'before-active':
        creation = world.now
        next(r for r in world.cfg['routes'] if r['source'] == 'teams')['active_ts'] = creation + 1
    if eligibility == 'before-seed':
        creation -= 1
    world.advance(3 * 86400)
    world.team('recovered', ts=creation, first_source='graph')
    created = world.app.sync()['created']
    assert len(created) == int(eligibility == 'eligible')
    row = world.db.execute("SELECT id FROM messages WHERE source='teams' AND msg_id='recovered'").fetchone()
    assert row is not None
    assert bool(world.db.execute('SELECT 1 FROM requests WHERE message_id=?', (row['id'],)).fetchone()) == (eligibility == 'eligible')
    # Persist discovery progress across router restarts, then discover another old row.
    world.db.close()
    world.db = connect(world.db_path)
    world.app = App(world.cfg, world.db, world.fx, world.paths)
    assert world.app.sync()['created'] == []
    if eligibility == 'eligible':
        world.team('later-recovery', ts=creation + 1, first_source='graph')
        assert len(world.app.sync()['created']) == 1
        assert world.app.sync()['created'] == []


@pytest.mark.parametrize('eligibility', ['eligible', 'before-seed', 'before-active'])
@pytest.mark.parametrize('replacement_kind', ['new-store', 'backup'])
def test_replaced_teams_store_rescans_without_replaying_requests(world, eligibility, replacement_kind):
    from datetime import datetime, timezone
    from pathlib import Path
    world.seed()
    creation = world.now
    previous = world.request('already', teams=True)
    for i in range(10):
        world.team(f'context-{i}', sender='unknown')
    world.app.sync()
    if eligibility == 'before-seed':
        creation -= 2
    if eligibility == 'before-active':
        next(r for r in world.cfg['routes'] if r['source'] == 'teams')['active_ts'] = creation + 1
    with sqlite3.connect(world.paths.bot_db) as con:
        con.row_factory = sqlite3.Row
        duplicate = dict(con.execute("SELECT * FROM messages WHERE id='already'").fetchone())
        version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        generation = con.execute("SELECT value FROM meta WHERE key='store_generation'").fetchone()[0]
    duplicate.pop('change_seq', None)
    recovered = duplicate | dict(id='recovered', ts=datetime.fromtimestamp(creation, timezone.utc).isoformat())
    replacement = world.root / 'replacement.db'
    con = sqlite3.connect(replacement)
    try:
        con.executescript(Path('tests/bot_store.sql').read_text())
        if replacement_kind == 'backup':
            con.execute("UPDATE meta SET value=? WHERE key='store_generation'", (generation,))
        con.execute("INSERT INTO meta VALUES ('schema_version',?)", (version,))
        for row in (recovered, duplicate):
            con.execute(f'INSERT INTO messages ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
        con.commit()
    finally:
        con.close()
    replacement.replace(world.paths.bot_db)
    world.advance(3 * 86400)
    created = world.app.sync()['created']
    assert len(created) == int(eligibility == 'eligible')
    assert world.db.execute("SELECT 1 FROM messages WHERE source='teams' AND msg_id='recovered'").fetchone()
    assert world.app.sync()['created'] == []
    assert world.db.execute("SELECT count(*) FROM requests r JOIN messages m ON m.id=r.message_id WHERE m.msg_id='already'").fetchone()[0] == 1
    assert world.db.execute("SELECT r.id FROM requests r JOIN messages m ON m.id=r.message_id WHERE m.msg_id='already'").fetchone()[0] == previous


@pytest.mark.parametrize('eligibility', ['eligible', 'before-seed', 'before-active'])
def test_unseen_teams_tombstone_restored_outside_lookback_is_discovered_once(world, eligibility):
    world.seed()
    creation = world.now
    if eligibility == 'before-seed':
        creation -= 2
    if eligibility == 'before-active':
        next(r for r in world.cfg['routes'] if r['source'] == 'teams')['active_ts'] = creation + 1
    world.advance(3 * 86400)
    world.team('tombstone', ts=creation, deleted=1)
    assert world.app.sync()['created'] == []
    assert not world.db.execute("SELECT 1 FROM messages WHERE msg_id='tombstone'").fetchone()
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("UPDATE messages SET deleted=0,text='Restored' WHERE id='tombstone'")
    assert len(world.app.sync()['created']) == int(eligibility == 'eligible')
    assert world.db.execute("SELECT text FROM messages WHERE msg_id='tombstone'").fetchone()[0] == 'Restored'
    assert world.app.sync()['created'] == []


def test_parent_quote_change_reaches_reply_after_removing_tracked_reads(world):
    world.team('parent', text='Before quote')
    world.seed()
    world.team('child', text='Reply', reply_to_id='parent')
    request = world.app.sync()['created'][0]
    world.advance(3 * 86400)
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("UPDATE messages SET text='After quote' WHERE id='parent'")
    world.app.sync()
    child = next(m for m in world.app.inbox('project-a') if m['request_id'] == request)
    assert child['text'] == 'Reply\n\nQuote:\n> After quote'


def test_inbox_says_when_a_teams_request_tags_only_someone_else(world, monkeypatch, capsys):
    from toucan.cli import main

    def tags(*items):
        return json.dumps([dict(text=text, kind=kind, id=ident) for text, kind, ident in items])
    bob = ('Bob Example', 'user', '00000000-0000-0000-0000-000000000002')
    world.seed()
    world.team('for-bob', mentions_json=tags(bob))
    world.team('for-both', mentions_json=tags(bob, ('Toucan', 'application', 'bot')))
    world.team('for-owner', mentions_json=tags(('Example Owner', 'user', 'owner-entra')))
    world.team('for-all', mentions_json=tags(bob, ('Everyone', 'conversation', 'group:chat')))
    world.team('unresolved', mentions_json=tags(('Carol', 'user', None)))
    world.team('untagged', mentions_json='[]')
    world.team('unknown')  # mention data the bot never had, e.g. a migrated row
    assert len(world.app.sync()['created']) == 7
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    assert main(['inbox', '--thread', 'project-a'], effects=world.fx, paths=world.paths) == 0
    flagged = [line for line in capsys.readouterr().out.splitlines() if 'and not Toucan: it is for them' in line]
    assert len(flagged) == 1 and 'tags "Bob Example"' in flagged[0]
    assert 'close it without replying unless the text also asks Toucan' in flagged[0]
    stored = {r['msg_id']: r['mentions_json'] for r in world.app.rows("SELECT msg_id,mentions_json FROM messages")}
    assert json.loads(stored['for-owner']) == [dict(name='Example Owner', who='owner')]
    assert stored['unknown'] is None
    # The bot learning about a mention later is a change the router picks up; missing data keeps it.
    world.team('unknown', mentions_json=tags(bob))
    world.app.sync()
    world.team('for-bob', mentions_json=None)
    world.app.sync()
    stored = {r['msg_id']: r['mentions_json'] for r in world.app.rows("SELECT msg_id,mentions_json FROM messages")}
    assert json.loads(stored['unknown']) == json.loads(stored['for-bob']) == [dict(name='Bob Example', who='person')]
    # Without the owner's Teams ID nobody can be told apart from the owner, so nothing is flagged.
    world.cfg['settings']['owner_teams_id'] = ''
    world.team('later-for-bob', mentions_json=tags(bob))
    world.app.sync()
    assert world.app.rows("SELECT mentions_json FROM messages WHERE msg_id='later-for-bob'")[0]['mentions_json'] == \
        json.dumps([dict(name='Bob Example', who='unknown')])


def test_routed_teams_catchup_failures_alert_once_per_route_until_recovery(world):
    from datetime import datetime, timezone

    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    def catchup(chat, **values):
        with sqlite3.connect(world.paths.bot_db) as con:
            con.execute('INSERT INTO catchup (conversation_id) VALUES (?) ON CONFLICT DO NOTHING', (chat,))
            for key, value in values.items():
                con.execute(f'UPDATE catchup SET {key}=? WHERE conversation_id=?', (value, chat))

    def alerts():
        return {a['key']: a['text'] for a in world.alerts()}
    world.seed()
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("INSERT INTO conversations VALUES ('unrouted:chat','groupChat',NULL,'',NULL,'2026-10-01T00:00:00Z','now')")
    catchup('unrouted:chat', last_error='HTTP 403: no RSC grant', http_status=403)
    assert world.app.sync()['problems'] == {}  # a chat no route uses never alerts
    # Missing consent in a routed chat: the heartbeat is fresh, yet only @mentions arrive.
    catchup('group:chat', last_error='HTTP 403: no RSC grant', http_status=403)
    world.app.sync()
    world.advance(60)
    world.app.sync()
    assert list(alerts()) == ['teams:catchup:group']
    text = alerts()['teams:catchup:group']
    assert text.startswith('group: ') and 'HTTP 403' in text and 'only sees messages that @mention it' in text
    assert 'Upgrade the Toucan Teams app to 1.1.0' in text and 'accept its permissions in this chat' in text
    assert len([p for p in world.posts if p[0].startswith('http://127.')]) == 1
    catchup('group:chat', last_error=None, http_status=None, last_success=iso(world.now))
    world.app.sync()
    assert alerts() == {}
    # Other failures alert only once the chat has gone three intervals without a success.
    catchup('group:chat', last_error='HTTP 500', http_status=500)
    world.advance(300)
    assert 'teams:catchup:group' not in world.app.sync()['problems']
    world.advance(61)
    assert 'last succeeded 6 min ago (HTTP 500)' in world.app.sync()['problems']['teams:catchup:group']


def test_never_synced_routed_chat_alerts_after_bounded_grace_or_a_catchup_pass(world):
    from datetime import datetime, timezone

    def meta(key, value):
        with sqlite3.connect(world.paths.bot_db) as con:
            con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, value))
    world.seed()
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute('DELETE FROM catchup')  # a migrated store: the chat is old, catch-up never ran
        con.execute("UPDATE conversations SET first_seen='2026-10-01T00:00:00Z'")
    meta('started_at', datetime.fromtimestamp(world.now - 60, timezone.utc).isoformat())  # the bot just restarted
    assert 'teams:catchup:group' not in world.app.sync()['problems']  # its first pass may still be running
    # The catch-up task keeps failing before it records any chat result; the heartbeat stays fresh.
    meta('task:catchup', json.dumps(dict(last_success=None, last_error='ValueError', failures=9)))
    world.advance(4 * 60)
    assert 'teams:catchup:group' not in world.app.sync()['problems']
    world.advance(2 * 60)
    problems = world.app.sync()['problems']
    assert 'health:teams:stale' not in problems
    assert problems['teams:catchup:group'].startswith('group: Teams catch-up has never succeeded')
    assert 'catch-up task failing: ValueError' in problems['teams:catchup:group']
    # A pass that ended after the bot first saw the chat reports it without waiting for the grace.
    meta('started_at', datetime.fromtimestamp(world.now, timezone.utc).isoformat())
    meta('task:catchup', json.dumps(dict(last_success=datetime.fromtimestamp(world.now, timezone.utc).isoformat(), last_error=None)))
    assert 'never succeeded' in world.app.sync()['problems']['teams:catchup:group']
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute('DELETE FROM conversations')
    assert 'add the Toucan app' in world.app.sync()['problems']['teams:catchup:group']


CODEX_LIMIT = dict(**{'class': 'usage_limit'}, code='usageLimitExceeded', retryable=None,
                   message='You’ve hit your usage limit. Visit https://example.com/usage to purchase more credits.')


@pytest.mark.parametrize('signal', ['failed-run', 'curly-message'])
def test_codex_usage_limit_is_detected_from_failed_runs_and_curly_apostrophes(world, signal):
    world.seed()
    req = world.request()
    world.t3('wake', world.now, 'Toucan woke you', run=1)
    if signal == 'failed-run':
        world.t3_run(1, 'failed', world.now, CODEX_LIMIT)  # Codex leaves no assistant message
    else:
        world.t3_run(1, 'completed', world.now)
        world.t3('error', world.now, CODEX_LIMIT['message'], role='assistant', run=1)
    assert 'limited:project-a' in world.app.sync()['problems']
    world.advance(15 * 60)
    world.app.sync()
    assert world.app.get(req)['wakes'] == 1  # the carry-on wake does not use the budget
    assert 'carry on' in world.hooks()[-1]['summary']
    # A newer turn, running and then finished, ends the limit even before it writes anything.
    world.t3_run(2, 'running', world.now)
    assert 'limited:project-a' not in world.app.sync()['problems']
    with sqlite3.connect(world.paths.t3) as db:
        db.execute("UPDATE orchestration_v2_projection_runs SET status='completed' WHERE ordinal=2")
    assert 'limited:project-a' not in world.app.sync()['problems']


def test_failed_runs_for_other_reasons_are_not_rate_limits(world):
    world.seed()
    world.t3_run(1, 'failed', world.now, {'class': 'transport_error', 'code': 'oom_kill', 'message': 'Killed: out of memory.'})
    assert 'limited:project-a' not in world.app.sync()['problems']


def test_claimed_request_is_told_when_its_mentions_are_read_differently(world):
    unresolved = json.dumps([dict(text='Bob Example', kind='user', id=None)])
    resolved = json.dumps([dict(text='Bob Example', kind='user', id='00000000-0000-0000-0000-000000000002')])
    world.seed()
    world.team('ask', mentions_json=unresolved)
    req = world.app.sync()['created'][0]
    world.app.claim([req], 'project-a')
    for mentions in (None, unresolved):  # no mention data, or the same reading: nothing to tell
        world.team('ask', mentions_json=mentions)
        world.app.sync()
        assert world.app.get(req)['change_pending'] == 0
    world.team('ask', mentions_json=resolved)  # Graph identifies the tag as someone else
    world.app.sync()
    row = world.app.get(req)
    assert row['state'] == 'claimed' and row['change_pending'] == 1 and row['source_changed'] == 1
    assert world.app.wait('project-a', 1)['requests'] == [req]


def test_screenshot_downloads_are_not_edits_and_readiness_is_told_once(world):
    image = dict(contentType='image/png', contentUrl='https://example.com/image')

    def attach(**download):
        world.team('shot', attachments_json=json.dumps([image | download]))
        world.app.sync()
        return world.app.get(req)
    world.seed()
    world.team('shot', attachments_json=json.dumps([image]))
    req = world.app.sync()['created'][0]
    world.app.claim([req], 'project-a')
    world.db.execute('UPDATE requests SET wakes=3,last_wake_at=? WHERE id=?', (world.now, req))
    for attempt in range(3):  # repeated failures only update download bookkeeping
        row = attach(download_status='pending', http_status=503, download_error='HTTPStatusError', retry_at=world.now + attempt)
        assert (row['change_pending'], row['source_changed'], row['wakes']) == (0, 0, 3)
    row = attach(download_status='available', local_path='/media/shot.png')
    assert (row['change_pending'], row['source_changed'], row['wakes']) == (1, 0, 3)
    assert json.loads(world.app.rows("SELECT attachments_json FROM messages WHERE msg_id='shot'")[0]['attachments_json'])[0]['local_path'] == '/media/shot.png'
    assert len(world.app.rows("SELECT * FROM events WHERE kind='attachment_ready'")) == 1
    world.app.inbox('project-a')
    row = attach(download_status='available', local_path='/media/shot.png')
    assert row['change_pending'] == 0  # told once
    world.team('shot', attachments_json=json.dumps([image | dict(local_path='/media/shot.png'),
                                                    dict(contentType='reference', name='brief.pdf', contentUrl='https://example.com/brief')]))
    world.app.sync()
    row = world.app.get(req)
    assert (row['change_pending'], row['source_changed'], row['wakes']) == (1, 1, 0)  # a real attachment change
