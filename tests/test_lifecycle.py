import concurrent.futures
import json
import subprocess
import threading

import pytest

from toucan.cli import main
from toucan.config import Refused, ValidationError, load
from toucan.core import App
from toucan.storage import connect
from conftest import OTHER, OWNER, PHONE


def test_concurrent_claims_exactly_one_wins_and_batch_is_atomic(world):
    world.seed()
    first = world.request('first')
    second = world.request('second')
    barrier = threading.Barrier(2)

    def worker():
        db = connect(world.db_path)
        try:
            app = App(world.cfg, db, world.fx, world.paths)
            barrier.wait()
            try:
                return app.claim([first, second], 'project-a')
            except Refused:
                return None
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    winners = [r for r in results if r]
    assert len(winners) == 1
    assert {world.app.get(r)['claim_id'] for r in (first, second)} == set(winners)
    third = world.request('third')
    with pytest.raises(Refused):
        world.app.claim([third, first], 'project-a')
    assert world.app.get(third)['state'] == 'new'


def test_release_invalidates_claim_and_allows_rewake(world):
    world.seed()
    req = world.request()
    old = world.app.claim([req], 'project-a')
    world.app.transition(req, old, 'release')
    world.advance(15 * 60)
    assert world.app.sync()['wakes']
    current = world.app.claim([req], 'project-a')
    with pytest.raises(Refused, match='current claim'):
        world.app.deliver(req, old, text='Wrong claim')
    assert not world.sends
    with pytest.raises(Refused, match='thread'):
        world.app.deliver(req, current, text='Wrong thread', thread='another')
    world.app.transition(req, current, 'done', 'Fixed')
    assert world.app.get(req)['done_note'] == 'Fixed'
    world.app.transition(req, current, 'close', 'No reply needed')
    assert world.app.get(req)['close_reason'] == 'No reply needed'


def test_whatsapp_text_explicit_frozen_jid_and_keep_open(world):
    world.seed()
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    world.cfg['people']['alice']['whatsapp'] = ['15550000003@s.whatsapp.net']
    world.app.deliver(req, claim, text='Resolved', keep_open=True)
    assert world.sends == [['wacli', '--json', 'send', 'text', '--to', PHONE, '--message', 'Resolved']]
    assert world.app.get(req)['state'] == 'claimed'
    result = world.app.deliver(req, claim, text='Final answer')
    assert result['external_id'] == 'sent-id'
    assert world.app.get(req)['state'] == 'closed'
    assert world.app.get(req)['close_reason'] == 'replied'
    with pytest.raises(Refused):
        world.app.deliver(req, claim, text='Duplicate')
    assert len(world.sends) == 2


def test_file_send_uses_caption_and_validates_before_outbox(world):
    world.seed()
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    with pytest.raises(ValidationError):
        world.app.deliver(req, claim, file=world.root / 'absent')
    assert not world.app.rows('SELECT * FROM outbox')
    file = world.root / 'proof.txt'
    file.write_text('Proof')
    world.app.deliver(req, claim, file=file, caption='Evidence')
    assert world.sends == [['wacli', '--json', 'send', 'file', '--to', PHONE, '--file', str(file), '--caption', 'Evidence']]


def test_nonzero_sender_does_not_advance_request_and_can_be_retried(world):
    world.seed()
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    world.send_result = subprocess.CompletedProcess([], 1, '', 'disconnected')
    with pytest.raises(RuntimeError, match='wacli failed'):
        world.app.deliver(req, claim, text='Answer')
    assert world.app.get(req)['state'] == 'claimed'
    out = world.app.rows('SELECT * FROM outbox')[0]
    assert out['state'] == 'failed' and out['error'] == 'disconnected'
    world.send_result = subprocess.CompletedProcess([], 0, '{}', '')
    world.app.deliver(req, claim, text='Answer')
    assert world.app.get(req)['state'] == 'closed'


def test_rate_limit_counts_sent_sending_and_uncertain_per_recipient(world):
    world.seed()
    requests = [world.request(str(i)) for i in range(4)]
    claim = world.app.claim(requests, 'project-a')
    world.cfg['settings']['rate_per_hour'] = 3
    for req in requests[:3]:
        world.app.deliver(req, claim, text='Answer')
    outs = world.app.rows('SELECT * FROM outbox')
    world.db.execute("UPDATE outbox SET state='sending' WHERE id=?", (outs[1]['id'],))
    world.db.execute("UPDATE outbox SET state='uncertain' WHERE id=?", (outs[2]['id'],))
    with pytest.raises(Refused, match='rate limit'):
        world.app.deliver(requests[3], claim, text='Too many')
    assert len(world.sends) == 3
    world.advance(3601)
    world.app.deliver(requests[3], claim, text='Allowed now')
    assert len(world.sends) == 4


def test_crash_after_sending_becomes_uncertain_and_never_resends(world):
    world.seed()
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    world.send_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        world.app.deliver(req, claim, text='Possibly delivered')
    assert world.app.rows('SELECT state FROM outbox') == [{'state': 'sending'}]
    assert world.app.get(req)['state'] == 'claimed'
    world.advance(121)
    world.app.sync()
    world.app.sync()
    assert world.app.rows('SELECT state FROM outbox') == [{'state': 'uncertain'}]
    assert len(world.sends) == 1
    assert world.alerts()[0]['key'].startswith('uncertain:')
    with pytest.raises(Refused, match='uncertain'):
        world.app.deliver(req, claim, text='No automatic or accidental duplicate')
    assert len(world.sends) == 1


def test_timeout_is_uncertain_rather_than_safe_to_retry(world):
    world.seed()
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    world.send_error = subprocess.TimeoutExpired(['wacli'], 120)
    with pytest.raises(RuntimeError, match='uncertain'):
        world.app.deliver(req, claim, text='Answer')
    assert world.app.rows('SELECT state FROM outbox') == [{'state': 'uncertain'}]


def test_teams_reply_goes_to_askers_whatsapp(world):
    world.seed()
    req = world.request(teams=True)
    claim = world.app.claim([req], 'project-a')
    world.app.deliver(req, claim, text='Finished the fix')
    assert world.sends == [['wacli', '--json', 'send', 'text', '--to', PHONE, '--message', 'Finished the fix']]
    assert world.app.get(req)['state'] == 'closed'


def test_teams_reply_on_bot_route_posts_in_the_chat_as_toucan(world):
    next(r for r in world.cfg['routes'] if r['name'] == 'group')['post_as_bot'] = True
    world.send_result = subprocess.CompletedProcess([], 0, '{"conversation_id": "group:chat", "activity_id": "1730"}', '')
    world.seed()
    req = world.request(teams=True)
    claim = world.app.claim([req], 'project-a')
    assert world.app.deliver(req, claim, text='Finished the fix')['external_id'] == '1730'
    assert world.sends == [['toucan-bot', '--json', 'send', '--conversation', 'group:chat', '--text', 'Finished the fix']]
    assert world.app.rows('SELECT source, kind, to_chat FROM outbox') == [
        {'source': 'teams-bot', 'kind': 'send', 'to_chat': 'group:chat'}]
    assert world.app.get(req)['state'] == 'closed'


@pytest.mark.parametrize('teams,mode,bot', [(False, 'act', False), (True, 'act', False), (True, 'draft', True)])
def test_reaction_refuses_routes_that_cannot_post_as_bot(world, teams, mode, bot):
    route = next(r for r in world.cfg['routes'] if r['name'] == ('group' if teams else 'alice-whatsapp'))
    route.update(mode=mode, post_as_bot=bot)
    world.seed()
    req = world.request(teams=teams)
    claim = world.app.claim([req], 'project-a')
    with pytest.raises(Refused, match='Teams act route with post_as_bot'):
        world.app.react(req, claim, 'eyes')
    assert world.sends == []
    assert world.app.rows("SELECT * FROM events WHERE kind LIKE 'reaction_%'") == []


@pytest.mark.parametrize('claim,thread', [(None, None), ('', None), ('wrong', None), ('current', 'wrong')])
def test_reaction_requires_current_claim_and_checks_thread(world, claim, thread):
    next(r for r in world.cfg['routes'] if r['name'] == 'group')['post_as_bot'] = True
    world.seed()
    req = world.request(teams=True)
    current = world.app.claim([req], 'project-a')
    with pytest.raises(Refused, match='claim|thread'):
        world.app.react(req, current if claim == 'current' else claim, 'eyes', thread=thread)
    assert world.sends == []


def test_react_cli_targets_trigger_or_override_and_logs_without_closing(world, monkeypatch, capsys):
    world.config_path.write_text(world.config_text.replace('\nsource = "teams"\n', '\nsource = "teams"\npost_as_bot = true\n'))
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    monkeypatch.setenv('TOUCAN_DB', str(world.db_path))
    world.seed()
    req = world.request('1730', teams=True)
    claim = world.app.claim([req], 'project-a')
    base = ['react', str(req), '--claim', claim, '--thread', 'project-a', '--json']
    assert main([*base, '--emoji', 'eyes'], effects=world.fx, paths=world.paths) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == dict(request=req, conversation_id='group:chat', message_id='1730', emoji='eyes', removed=False)
    world.app.transition(req, claim, 'done', 'Work finished')
    assert main([*base, '--message', '1731', '--emoji', 'check', '--remove'], effects=world.fx, paths=world.paths) == 0
    assert json.loads(capsys.readouterr().out)['removed']
    assert world.sends == [
        ['toucan-bot', '--json', 'react', '--conversation', 'group:chat', '--message', '1730', '--emoji', 'eyes'],
        ['toucan-bot', '--json', 'react', '--conversation', 'group:chat', '--message', '1731', '--emoji', 'check', '--remove']]
    assert world.app.get(req)['state'] == 'done'
    assert world.app.rows('SELECT * FROM outbox') == []
    assert main(['log', '--request', str(req), '--json'], effects=world.fx, paths=world.paths) == 0
    events = [e for e in json.loads(capsys.readouterr().out) if e['kind'].startswith('reaction_')]
    assert [e['kind'] for e in events] == ['reaction_pending', 'reaction_added', 'reaction_pending', 'reaction_removed']
    assert json.loads(events[-1]['detail_json']) == dict(conversation_id='group:chat', message_id='1731', emoji='check', removed=True)


@pytest.mark.parametrize('interrupted', [False, True])
def test_reaction_failure_is_logged_without_closing_or_retrying(world, interrupted):
    next(r for r in world.cfg['routes'] if r['name'] == 'group')['post_as_bot'] = True
    world.seed()
    req = world.request(teams=True)
    claim = world.app.claim([req], 'project-a')
    world.send_result = subprocess.CompletedProcess([], 1, '', 'HTTP 404: message deleted')
    if interrupted:
        world.send_error = subprocess.TimeoutExpired(['toucan-bot'], 120)
    with pytest.raises(RuntimeError, match='uncertain' if interrupted else 'inspect request log'):
        world.app.react(req, claim, 'eyes')
    assert len(world.sends) == 1 and world.app.get(req)['state'] == 'claimed'
    events = world.app.rows("SELECT kind,detail_json FROM events WHERE kind LIKE 'reaction_%'")
    assert [e['kind'] for e in events] == ['reaction_pending', 'reaction_uncertain' if interrupted else 'reaction_failed']
    if not interrupted:
        assert json.loads(events[-1]['detail_json'])['error'] == 'HTTP 404: message deleted'


def test_teams_reply_without_asker_whatsapp_becomes_owner_draft_then_owner_post_closes(world):
    world.cfg['people']['alice']['whatsapp'] = []
    world.seed()
    req = world.request(teams=True)
    claim = world.app.claim([req], 'project-a')
    # An earlier owner message and messages by other people do not acknowledge a draft.
    world.team('old-owner', sender='owner-entra', ts=world.now - 1)
    world.app.deliver(req, claim, text='Finished the fix')
    assert world.sends == [['wacli', '--json', 'send', 'text', '--to', OWNER, '--message',
                            'Draft for group:chat → reply to Alice Example:\n\nFinished the fix']]
    assert world.app.get(req)['state'] == 'done'
    assert world.app.rows('SELECT kind FROM outbox') == [{'kind': 'draft'}]
    world.advance(1)
    world.team('not-owner', sender='other-entra')
    world.app.sync()
    assert world.app.get(req)['state'] == 'done'
    world.team('owner-post', sender='owner-entra')
    world.app.sync()
    assert world.app.get(req)['state'] == 'closed'
    assert world.app.get(req)['close_reason'] == 'owner posted'


def test_whatsapp_draft_stays_done_until_manually_closed(world):
    world.seed()
    world.cfg['routes'][0]['mode'] = 'draft'
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    file = world.root / 'reply.txt'
    file.write_text('Answer')
    world.app.deliver(req, claim, file=file, caption='A document')
    assert world.sends[0][5] == OWNER
    assert world.sends[0][-1] == 'Draft for WhatsApp: Alice → reply to Alice Example:\n\nA document'
    assert world.app.get(req)['state'] == 'done'
    world.advance(1)
    world.wa('owner', from_me=1, sender=None)
    world.app.sync()
    assert world.app.get(req)['state'] == 'done'
    world.app.transition(req, claim, 'close', 'Delivered manually')
    assert world.app.get(req)['state'] == 'closed'


def test_escalation_sends_owner_excerpt_and_waits_for_answer(world):
    world.seed()
    world.wa('long', text='x' * 600)
    req = world.app.sync()['created'][0]
    claim = world.app.claim([req], 'project-a')
    world.app.deliver(req, claim, text='Need your decision', escalation=True)
    assert world.sends == [['wacli', '--json', 'send', 'text', '--to', OWNER, '--message',
                            'Escalation from thread project-a: Alice Example asked: ' + 'x' * 500 + '\nNote: Need your decision'
                            '\n\nReply to this message with your answer.']]
    row = world.app.get(req)
    assert (row['state'], row['closed_at'], row['claim_id']) == ('escalated', None, None)
    assert world.app.rows('SELECT kind FROM outbox') == [{'kind': 'escalation'}]


def escalate(world):
    world.seed()
    req = world.request()
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Need your decision', escalation=True)
    # wacli's own copy of the escalation, under the ID that send returned
    world.wa('sent-id', chat_jid=OWNER, sender=None, from_me=1, text='Escalation from thread project-a: ...')
    world.advance(3 * 60)
    return req


def owner_says(world, msg_id, text, **values):
    world.wa(msg_id, chat_jid=OWNER, sender=OWNER, sender_name='Example Owner', text=text, **values)
    world.advance(3 * 60)
    return world.app.sync()


def test_owner_answer_reopens_escalation_wakes_thread_and_allows_reply(world):
    req = escalate(world)
    result = owner_says(world, 'answer', 'send it')
    assert result['answers'] == [req] and world.hooks()[-1]['thread'] == 'project-a'
    assert world.app.get(req)['state'] == 'new'
    answers = [m for m in world.app.inbox('project-a') if m['answers_request']]
    assert [(m['text'], m['answers_request'], m['chat']) for m in answers] == [('send it', req, OWNER)]
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Done')
    assert world.sends[-1][5] == PHONE and world.app.get(req)['state'] == 'closed'
    assert owner_says(world, 'late', 'thanks')['answers'] == []


def test_unquoted_answer_after_other_outbound_goes_to_the_only_open_escalation(world):
    req = escalate(world)
    world.wa('report', chat_jid=OWNER, sender=None, from_me=1, text='The report is ready to forward')
    assert owner_says(world, 'other-quote', 'nice', quoted_msg_id='report')['answers'] == []
    assert world.app.get(req)['state'] == 'escalated'
    assert owner_says(world, 'answer', 'send this to him')['answers'] == [req]
    assert world.app.get(req)['state'] == 'new'


def test_unquoted_answer_with_several_open_escalations_alerts_and_waits_for_a_quote(world):
    first = escalate(world)
    second = world.request('second')
    world.send_result = subprocess.CompletedProcess([], 0, '{"data":{"MsgID":"second-id"}}', '')
    world.app.deliver(second, world.app.claim([second], 'project-a'), text='Second decision', escalation=True)
    world.wa('second-id', chat_jid=OWNER, sender=None, from_me=1, text='Escalation 2')
    world.wa('bot', chat_jid=OWNER, sender=None, from_me=1, text='Which items are sold out?')
    assert owner_says(world, 'stock', '3, 14')['answers'] == []
    assert not world.db.execute("SELECT 1 FROM messages WHERE msg_id='stock'").fetchone()
    assert [a['key'] for a in world.alerts()] == ['ambiguous:stock']
    assert owner_says(world, 'quoted', 'send it', quoted_msg_id='sent-id')['answers'] == [first]
    assert world.app.get(second)['state'] == 'escalated'


def test_resume_needs_owner_to_have_written_in_the_owner_thread_since_the_escalation(world):
    req = escalate(world)
    world.t3('before', world.now - 3600, 'unrelated')
    world.t3('wake', world.now, 'Toucan woke you', created_by='agent')
    world.t3('compact', world.now, '/compact')
    with pytest.raises(Refused, match='not written'):
        world.app.resume(req, 'project-a')
    world.t3('go', world.now + 1, 'go ahead, send it')
    result = world.app.resume(req, 'project-a')
    assert result['owner_said'] == ['go ahead, send it']
    world.app.deliver(req, result['claim_id'], text='Done')
    assert world.app.get(req)['state'] == 'closed'
    with pytest.raises(Refused, match='only an escalated'):
        world.app.resume(req, 'project-a')


def test_follow_up_answer_rewakes_claimed_request(world):
    req = escalate(world)
    owner_says(world, 'answer', 'send it')
    claim = world.app.claim([req], 'project-a')
    world.advance(15 * 60)
    assert owner_says(world, 'change', 'actually say tomorrow')['answers'] == [req]
    assert world.app.get(req)['claim_id'] == claim
    assert world.hooks()[-1]['count'] == 1


def test_answer_matches_escalation_text_when_send_returned_no_id(world):
    world.send_result = subprocess.CompletedProcess([], 0, 'not json', '')
    world.seed()
    req = world.request()
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Need your decision', escalation=True)
    body = world.app.rows('SELECT text FROM outbox')[0]['text']
    world.wa('wa-copy', chat_jid=OWNER, sender=None, from_me=1, text=body)
    assert owner_says(world, 'answer', 'close it')['answers'] == [req]


@pytest.mark.parametrize('teams', [False, True])
def test_import_pending_reads_history_and_skips_already_answered(world, teams):
    message = world.team if teams else world.wa
    message('answered', ts=world.now - 1000)
    owner_kwargs = {'sender': 'owner-entra'} if teams else {'sender': None, 'from_me': 1}
    message('reply', ts=world.now - 900, **owner_kwargs)
    message('pending', ts=world.now - 800)
    message('unknown', sender='unapproved-entra' if teams else '15550000003@s.whatsapp.net', ts=world.now - 700)
    route = 'group' if teams else 'alice-whatsapp'
    world.seed()
    created = world.app.import_pending(route, world.now - 1100)
    assert len(created) == 1
    req = world.app.get(created[0])
    m = world.db.execute('SELECT msg_id FROM messages WHERE id=?', (req['message_id'],)).fetchone()
    assert m['msg_id'] == 'pending'
    assert world.app.import_pending(route, world.now - 1100) == []


@pytest.mark.parametrize('claim', [None, '', 'wrong'])
def test_delivery_requires_current_claim_even_at_application_boundary(world, claim):
    world.seed()
    req = world.request()
    with pytest.raises(Refused, match='claim'):
        world.app.deliver(req, claim, text='Must not send')
    assert world.sends == []
    assert world.app.rows('SELECT * FROM outbox') == []


def test_owner_reply_in_chat_stands_toucan_down_until_it_has_sent(world):
    route = next(r for r in world.cfg['routes'] if r['name'] == 'group')
    route['owner_reply_closes'] = True
    route['post_as_bot'] = True
    world.send_result = subprocess.CompletedProcess([], 0, '{"conversation_id": "group:chat", "activity_id": "1"}', '')
    world.seed()
    answered = world.request('answered', teams=True)
    world.advance(1)
    world.team('owner-answer', sender='owner-entra')
    world.app.sync()
    assert world.app.get(answered)['state'] == 'closed'
    assert world.app.get(answered)['close_reason'] == 'owner replied'
    # Once Toucan has replied, a later owner message leaves the request alone.
    world.advance(1)
    promised = world.request('promised', teams=True)
    claim = world.app.claim([promised], 'project-a')
    world.app.deliver(promised, claim, text='On it', promise='fix it', due=world.now + 2 * 3600)
    world.advance(1)
    world.team('owner-thanks', sender='owner-entra')
    world.app.sync()
    assert world.app.get(promised)['state'] == 'claimed'


def test_reply_to_route_sends_every_reply_to_that_person_with_the_prefix(world):
    group = '120363000000000001@g.us'
    world.config_path.write_text(world.config_text + f'''
[[routes]]
name = "client-group"
source = "whatsapp"
chats = ["{group}"]
thread = "project-a"
mode = "act"
reply_to = "other"
reply_prefix = "Automated reply."
requesters = ["alice"]
active_from = "2026-10-17T00:00:00+05:00"
''')
    world.cfg = load(world.config_path)
    world.app = App(world.cfg, world.db, world.fx, world.paths)
    world.seed()
    world.wa('in-group', chat_jid=group)
    req = world.app.sync()['created'][0]
    claim = world.app.claim([req], 'project-a')
    world.app.deliver(req, claim, text='The answer')
    world.app.send('client-group', 'project-a', text='A follow-up')
    assert world.sends == [['wacli', '--json', 'send', 'text', '--to', OTHER, '--message', 'Automated reply.\n\nThe answer'],
                           ['wacli', '--json', 'send', 'text', '--to', OTHER, '--message', 'Automated reply.\n\nA follow-up']]
    assert world.app.get(req)['state'] == 'closed'


@pytest.mark.parametrize('observe', ['wait', 'inbox'])
def test_edit_survives_successful_webhook_until_thread_observes_it(world, observe):
    import sqlite3
    world.seed()
    req = world.request()
    world.app.claim([req], 'project-a')
    world.advance(121)
    with sqlite3.connect(world.wa_path) as db:
        db.execute("UPDATE messages SET text='Changed request',edited=1 WHERE msg_id='req'")
    assert world.app.sync()['wakes']
    assert world.app.get(req)['change_pending'] == 1
    world.fx.sleep = world.advance
    if observe == 'wait':
        assert world.app.wait('project-a', 1)['requests'] == [req]
    else:
        assert any(m['text'] == 'Changed request' for m in world.app.inbox('project-a'))
    assert world.app.get(req)['change_pending'] == 0
    assert world.app.wait('project-a', 1) is None


@pytest.mark.parametrize('requesters,phones,opt_in,expected', [
    (['alice'], True, True, PHONE),
    (['alice', 'other'], True, True, OWNER),
    (['alice'], False, True, OWNER),
    (['alice'], True, False, OWNER),
])
def test_proactive_teams_send_uses_opted_in_single_requester_phone(world, requesters, phones, opt_in, expected):
    route = next(r for r in world.cfg['routes'] if r['source'] == 'teams')
    route['requesters'] = requesters
    route['reply_via_whatsapp'] = opt_in
    if not phones:
        world.cfg['people']['alice']['whatsapp'] = ['100000000000001@lid']
    world.app.send(route['name'], route['thread'], text='Update')
    assert world.sends[-1][0] == 'wacli'
    assert world.sends[-1][5] == expected
    assert world.app.rows('SELECT kind FROM outbox')[0]['kind'] == ('send' if expected == PHONE else 'draft')


def test_crash_before_sender_marks_pending_never_attempted_and_allows_retry(world, monkeypatch):
    world.seed()
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    send = world.app._send_outbox

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(world.app, '_send_outbox', interrupted)
    with pytest.raises(KeyboardInterrupt):
        world.app.deliver(req, claim, text='Answer')
    assert world.app.rows('SELECT state FROM outbox') == [{'state': 'pending'}]
    world.advance(301)
    report = world.app.sync()
    [out] = world.app.rows('SELECT * FROM outbox')
    assert out['state'] == 'failed'
    assert 'never attempted' in out['error']
    assert f'failed:{out["id"]}' in report['problems']
    assert not world.sends
    with pytest.raises(Refused, match='no longer pending'):
        send(out['id'], 'project-a', req)
    assert not world.sends
    monkeypatch.setattr(world.app, '_send_outbox', send)
    world.app.deliver(req, claim, text='Answer')
    assert len(world.sends) == 1
    assert f'failed:{out["id"]}' not in world.app.sync()['problems']


@pytest.mark.parametrize('kind', ['draft', 'escalation'])
def test_owner_deliveries_share_recipient_hourly_limit(world, kind):
    world.cfg['people']['alice']['whatsapp'] = []
    world.seed()
    first, second = world.request('first', teams=True), world.request('second', teams=True)
    claim = world.app.claim([first, second], 'project-a')
    world.cfg['settings']['rate_per_hour'] = 1
    world.app.deliver(first, claim, text='Draft answer')
    with pytest.raises(Refused, match='hourly rate limit'):
        world.app.deliver(second, claim, text='Another answer', escalation=kind == 'escalation')
    assert len(world.sends) == 1
    world.advance(3601)
    world.app.deliver(second, claim, text='Answer', escalation=kind == 'escalation')
    assert len(world.sends) == 2


def test_failed_delivery_alerts_once_until_successful_retry(world):
    world.seed()
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    world.send_result = subprocess.CompletedProcess([], 1, '', 'PRIVATE sender error')
    with pytest.raises(RuntimeError):
        world.app.deliver(req, claim, text='Answer')
    out = world.app.rows('SELECT id FROM outbox')[0]['id']
    key = f'failed:{out}'
    report = world.app.sync()
    assert key in report['problems']
    assert 'PRIVATE' not in report['problems'][key]
    posts = len(world.posts)
    world.app.sync()
    assert len(world.posts) == posts
    assert len(world.app.rows("SELECT * FROM events WHERE kind='alert' AND detail_json LIKE ?", ('%"' + key + '"%',))) == 1
    world.send_result = subprocess.CompletedProcess([], 0, '{}', '')
    world.app.deliver(req, claim, text='Answer')
    assert key not in world.app.sync()['problems']
    assert not any(a['key'] == key for a in world.alerts())


def test_ambiguous_bot_exit_blocks_a_duplicate_reply(world):
    route = next(r for r in world.cfg['routes'] if r['name'] == 'group')
    route['post_as_bot'] = True
    world.seed()
    req = world.request(teams=True)
    claim = world.app.claim([req], 'project-a')
    world.send_result = subprocess.CompletedProcess([], 4, '', 'delivery uncertain')
    with pytest.raises(RuntimeError, match='uncertain'):
        world.app.deliver(req, claim, text='hello')
    assert world.app.rows('SELECT state FROM outbox') == [{'state': 'uncertain'}]
    with pytest.raises(Refused, match='uncertain'):
        world.app.deliver(req, claim, text='hello again')
    assert len(world.sends) == 1


@pytest.mark.parametrize('state,closed', [('failed', True), ('sent', False), ('sending', False),
                                         ('uncertain', False), ('pending', False)])
def test_owner_reply_stands_down_after_only_failed_sends(world, state, closed):
    route = next(r for r in world.cfg['routes'] if r['name'] == 'group')
    route['post_as_bot'] = route['owner_reply_closes'] = True
    world.seed()
    req = world.request(teams=True)
    claim = world.app.claim([req], 'project-a')
    world.send_result = subprocess.CompletedProcess([], 1, '', 'forbidden')
    with pytest.raises(RuntimeError):
        world.app.deliver(req, claim, text='hello')
    world.db.execute('UPDATE outbox SET state=?', (state,))
    world.db.commit()
    world.advance(1)
    world.team('owner-answer', sender='owner-entra')
    world.app.sync()
    assert (world.app.get(req)['state'] == 'closed') == closed
