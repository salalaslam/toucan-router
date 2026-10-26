import json
from pathlib import Path

import pytest

from toucan.cli import main
from toucan.config import load
from toucan.core import App
from conftest import OTHER, OWNER, PHONE

OWNER_ROUTE = f'''
[people.owner]
name = "the owner"
whatsapp = ["{OWNER}"]

[[routes]]
name = "owner-whatsapp"
source = "whatsapp"
chats = ["{OWNER}"]
thread = "project-a"
mode = "act"
requesters = ["owner"]
active_from = "2026-10-17T00:00:00+05:00"
'''


def configure(world, extra='', transcribe=True):
    text = world.config_text + extra
    if transcribe:
        text = text.replace('[settings]\n', '[settings]\ntranscribe = "transcribe-test"\n', 1)
    world.config_path.write_text(text)
    world.cfg = load(world.config_path)
    world.app = App(world.cfg, world.db, world.fx, world.paths)


def voice(world, msg_id, **values):
    world.wa(msg_id, text='', display_text='[Audio]', media_type='audio', **values)


def sync(world):
    world.advance(60)
    return world.app.sync()


def escalate(world):
    req = world.request()
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Need your decision', escalation=True)
    world.wa('sent-id', chat_jid=OWNER, sender=None, from_me=1, text='Escalation from thread project-a: ...')
    return req


def test_voice_note_becomes_a_request_once_transcribed_and_is_transcribed_once(world):
    configure(world)
    world.seed()
    world.transcript = (1, '')
    voice(world, 'vn')
    assert sync(world)['created'] == [] and world.downloads == ['vn']
    world.transcript = (0, 'Please check the invoice\n')
    req = sync(world)['created'][0]
    assert world.downloads == ['vn']  # the first attempt's file is reused
    [message] = [m for m in world.app.inbox('project-a') if m['request_id'] == req]
    assert message['text'] == 'Voice note, transcribed automatically:\nPlease check the invoice'
    [attachment] = json.loads(message['attachments_json'])
    assert Path(attachment['local_path']).parent == world.paths.media
    sync(world)
    assert len(world.transcribed) == 2 and world.app.get(req)['source_changed'] == 0


def test_voice_note_that_cannot_be_downloaded_goes_ahead_after_three_tries(world):
    configure(world)
    world.seed()
    world.download_ok = False
    voice(world, 'vn')
    assert [sync(world)['created'] for _ in range(2)] == [[], []]
    req = sync(world)['created'][0]
    assert 'automatic transcription failed' in world.app.inbox('project-a')[-1]['text']
    sync(world)
    assert world.downloads == ['vn'] * 3 and world.transcribed == [] and world.app.get(req)['state'] == 'new'


def test_voice_notes_from_people_who_are_not_requesters_are_left_alone(world):
    configure(world)
    world.seed()
    voice(world, 'stranger', sender=OTHER)
    voice(world, 'mine', sender=None, from_me=1)
    assert sync(world)['voice_notes'] == 0 and world.downloads == []


def test_without_a_transcriber_voice_notes_are_requests_at_once(world):
    configure(world, transcribe=False)
    world.seed()
    voice(world, 'vn')
    assert len(sync(world)['created']) == 1 and world.downloads == []


def test_owner_messages_on_an_owner_route_are_requests_answered_in_that_chat(world, monkeypatch, capsys):
    configure(world, OWNER_ROUTE)
    world.seed()
    world.wa('ask', chat_jid=OWNER, sender=OWNER, sender_name='Example Owner', text='Update the banner')
    result = sync(world)
    [req] = result['created']
    assert result['answers'] == []
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    monkeypatch.setenv('TOUCAN_DB', str(world.db_path))
    assert main(['inbox', '--thread', 'project-a'], effects=world.fx, paths=world.paths) == 0
    assert '----- OWNER REQUEST from "Example Owner"' in capsys.readouterr().out
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Done')
    assert world.sends[-1][5] == OWNER and world.app.get(req)['state'] == 'closed'


def test_on_an_owner_route_only_quoted_or_immediate_replies_answer_an_escalation(world):
    configure(world, OWNER_ROUTE)
    world.seed()
    req = escalate(world)
    world.wa('report', chat_jid=OWNER, sender=None, from_me=1, text='The report is ready')
    world.wa('ask', chat_jid=OWNER, sender=OWNER, sender_name='Example Owner', text='Also build the page')
    result = sync(world)
    assert result['answers'] == [] and len(result['created']) == 1
    assert world.app.get(req)['state'] == 'escalated'
    world.wa('answer', chat_jid=OWNER, sender=OWNER, sender_name='Example Owner', text='send it', quoted_msg_id='sent-id')
    result = sync(world)
    assert result['answers'] == [req] and result['created'] == []
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Need one more thing', escalation=True)
    world.wa('sent-id-2', chat_jid=OWNER, sender=None, from_me=1, text='Escalation 2')
    world.app.rows("UPDATE outbox SET external_id='sent-id-2' WHERE id=(SELECT max(id) FROM outbox)")
    world.wa('right-after', chat_jid=OWNER, sender=OWNER, sender_name='Example Owner', text='yes')
    result = sync(world)
    assert result['answers'] == [req] and result['created'] == []


def test_voice_answer_to_an_escalation_waits_for_its_transcript(world):
    configure(world)
    world.seed()
    req = escalate(world)
    world.transcript = (1, '')
    voice(world, 'answer', chat_jid=OWNER, sender=OWNER, sender_name='Example Owner')
    assert sync(world)['answers'] == [] and world.app.get(req)['state'] == 'escalated'
    world.transcript = (0, 'send it to him')
    assert sync(world)['answers'] == [req]
    [answer] = [m for m in world.app.inbox('project-a') if m['answers_request']]
    assert answer['text'] == 'Voice note, transcribed automatically:\nsend it to him'


def test_teams_only_escalation_accepts_owner_lid_answer(world):
    import sqlite3
    owner_lid = '100000000000000@lid'
    world.cfg['routes'] = [r for r in world.cfg['routes'] if r['source'] == 'teams']
    with sqlite3.connect(world.paths.wa / 'session.db') as db:
        db.execute('INSERT INTO whatsmeow_lid_map VALUES (?,?)', (owner_lid.split('@')[0], OWNER.split('@')[0]))
    world.seed()
    req = world.request(teams=True)
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Need a decision', escalation=True)
    world.advance(1)
    world.wa('lid-answer', chat_jid=owner_lid, sender=owner_lid, text='Go ahead', quoted_msg_id='sent-id')
    assert world.app.sync()['answers'] == [req]
    assert world.app.get(req)['state'] == 'new'
    assert [m['text'] for m in world.app.inbox('project-a') if m['answers_request']] == ['Go ahead']


@pytest.mark.parametrize('alias', [False, True])
def test_owner_request_label_uses_whatsapp_jid_without_requiring_a_person(world, alias):
    from argparse import Namespace
    from toucan.cli import dispatch
    world.seed()
    sender = OWNER
    if alias:
        configure(world, OWNER_ROUTE.replace(f'whatsapp = ["{OWNER}"]', f'whatsapp = ["{OWNER}", "100000000000000@lid"]'))
        sender = '100000000000000@lid'
    # The owner can also appear as context in someone else's route.
    world.wa('owner-context', sender=sender, text='Owner context')
    world.app.sync()
    result = dispatch(Namespace(command='inbox', thread='project-a', json=False), world.app)
    assert '----- OWNER REQUEST' in result
