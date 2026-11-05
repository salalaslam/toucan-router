"""Project delegates share decision handling, with one durable owner fallback."""
import json
import subprocess

import pytest

from conftest import OWNER, OTHER, PHONE
from test_cards import CARD, escalate, stored_answer
from toucan.cli import main
from toucan.config import ValidationError, load
from toucan.core import App
from toucan.storage import connect, insert


def configure(world, **values):
    world.cfg['projects']['project-a'] = dict(name='Project A', thread='project-a',
                                             escalate_to='other', escalate_wait_minutes=10, **values)
    world.cfg['people']['other']['name'] = 'Bob Example'


def say(world, sender, msg, text, **values):
    world.wa(msg, chat_jid=sender, sender=sender, text=text, **values)
    world.advance(1)
    return world.app.sync()


def test_activation_and_internal_routing_cards(world):
    world.seed()
    configure(world, escalate_from='2026-10-18T00:00:00+05:00')
    before, _ = escalate(world, 'before')
    assert world.sends[-1][5] == OWNER
    world.advance(86400)
    after, _ = escalate(world, 'after')
    assert world.sends[-1][5] == OTHER
    assert '10 minutes' in world.sends[-1][7]
    req = world.request('internal')
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='No thread available', escalation=True)
    assert world.sends[-1][5] == OWNER
    assert before != after


@pytest.mark.parametrize('answer', ['B', 'tagged', 'quoted'])
def test_delegate_answers_and_more_get_receipts_in_their_chat(world, answer):
    world.seed()
    configure(world)
    req, _ = escalate(world)
    assert say(world, OTHER, 'more', f'more T{req}')['answers'] == []
    assert world.sends[-1][5] == OTHER and 'detail' in world.sends[-1][7]
    text = f'T{req} B' if answer == 'tagged' else 'B'
    quote = dict(quoted_msg_id='sent-id') if answer == 'quoted' else {}
    assert say(world, OTHER, 'reply', text, **quote)['answers'] == [req]
    assert stored_answer(world, req) == ['B) Wait for PR 110']
    assert world.sends[-1][5] == OTHER and 'Wait for PR 110' in world.sends[-1][7]
    world.advance(601)
    world.app.sync()
    assert not world.app.rows('SELECT * FROM cards WHERE forward_outbox IS NOT NULL')


def test_stranger_cannot_answer_even_in_delegate_chat(world):
    world.seed()
    configure(world)
    req, _ = escalate(world)
    world.wa('stranger', sender=PHONE, chat_jid=OTHER, text=f'T{req} B')
    world.advance(1)
    assert world.app.sync()['answers'] == []
    assert stored_answer(world, req) == []
    assert world.app.get(req)['state'] == 'escalated'


@pytest.mark.parametrize('winner,loser,name', [(OWNER, OTHER, 'Example Owner'), (OTHER, OWNER, 'Bob Example')])
def test_timeout_survives_restart_and_first_answer_wins(world, winner, loser, name):
    world.seed()
    configure(world)
    req, _ = escalate(world)
    world.advance(599)
    world.app.sync()
    assert len(world.sends) == 1
    world.advance(1)
    world.send_result = subprocess.CompletedProcess([], 0, '{"data":{"MsgID":"owner-copy"}}', '')
    world.app.sync()
    assert len(world.sends) == 2 and world.sends[-1][5] == OWNER
    assert 'Bob Example did not answer within 10 minutes' in world.sends[-1][7]
    world.db.close()
    world.db = connect(world.db_path)
    world.app = App(world.cfg, world.db, world.fx, world.paths)
    world.app.sync()
    assert len(world.sends) == 2
    # Quotes on the owner copy survive loss of the convenience alias after sending.
    world.db.execute("DELETE FROM meta WHERE key='quote:owner-copy'")
    # Both arrive before one sync; archive timestamp order decides the winner.
    world.wa('winner', sender=winner, chat_jid=winner, text='A' if winner == OWNER else f'T{req} A',
             quoted_msg_id='owner-copy' if winner == OWNER else None)
    world.advance(1)
    world.wa('loser', sender=loser, chat_jid=loser, text=f'T{req} B')
    world.advance(1)
    assert world.app.sync()['answers'] == [req]
    assert stored_answer(world, req) == ['A) Yes, release now']
    assert world.sends[-1][5] == loser
    assert f'already answered by {name}' in world.sends[-1][7]
    count = len(world.sends)
    world.app.sync()
    assert len(world.sends) == count


def test_replaced_and_closed_cards_never_forward(world):
    world.seed()
    configure(world)
    req, _ = escalate(world)
    say(world, OWNER, 'answer', f'T{req} A')
    claim = world.app.claim([req], 'project-a')
    world.app.escalate(req, claim, **{**CARD, 'question': 'Roll back the release?'})
    world.db.execute("UPDATE requests SET state='closed',closed_at=? WHERE id=?", (world.now, req))
    world.advance(601)
    world.app.sync()
    assert not world.app.rows('SELECT * FROM cards WHERE forward_outbox IS NOT NULL')


@pytest.mark.parametrize('recorded', [False, True])
def test_original_project_is_used_on_standby(world, recorded):
    world.seed()
    configure(world)
    world.cfg['threads']['project-b'] = dict(hook_file=str(world.hook))
    world.cfg['threads']['project-a']['fallback'] = 'project-b'
    req = world.request('standby')
    world.db.execute("UPDATE requests SET thread='project-b' WHERE id=?", (req,))
    if recorded:
        world.cfg['threads']['project-a']['project'] = 'unrelated'
        insert(world.db, 'router_decisions', request_id=req, mode='live', projects_json='["project-a"]', parts_json='[]')
    world.app.escalate(req, world.app.claim([req], 'project-b'), **CARD)
    assert world.sends[-1][5] == OTHER


@pytest.mark.parametrize('field,value', [('escalate_to', 'missing'), ('escalate_to', 'bob'),
                                          ('escalate_from', '2026-10-17T12:00:00'),
                                          ('escalate_wait_minutes', 0), ('escalate_wait_minutes', True)])
def test_invalid_project_escalation_is_rejected(world, field, value):
    import copy
    from toucan.config import validate
    cfg = copy.deepcopy(world.cfg)
    cfg['people']['bob'] = dict(name='Bob Example', whatsapp=['100000000000002@lid'])
    cfg['projects']['project-a'] = dict(name='Project A', thread='project-a', **{field: value})
    with pytest.raises(ValidationError):
        validate(cfg)


def test_config_command_validates_before_writing_and_clears_atomically(world, monkeypatch, capsys):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    world.config_path.write_text(world.config_text + '\n[projects.project-a]\nname="Project A"\nthread="project-a"\n')
    def command(*args):
        return main(['config', 'escalation', 'project-a', *args, '--json'], effects=world.fx, paths=world.paths)
    original = world.config_path.read_bytes()
    assert command('--to', 'missing') == 2
    assert world.config_path.read_bytes() == original
    assert command('--to', 'other', '--from', '2026-10-17T12:00:00+05:00', '--wait-minutes', '15') == 0
    capsys.readouterr()
    project = load(world.config_path)['projects']['project-a']
    assert project['escalate_to'] == 'other' and project['escalate_wait_minutes'] == 15
    assert project['escalate_from'] == '2026-10-17T12:00:00+05:00'
    assert command('--to', 'other') == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'updated'
    assert load(world.config_path)['projects']['project-a']['escalate_wait_minutes'] == 10
    assert command('--to', 'other') == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'unchanged'
    assert command('--clear') == 0
    assert 'escalate_to' not in load(world.config_path)['projects']['project-a']
    assert world.sends == []


@pytest.mark.parametrize('tagged', [False, True])
def test_delegate_can_pass_to_owner_immediately_without_timer_duplicate(world, tagged):
    world.seed()
    configure(world)
    req, _ = escalate(world)
    assert 'reply pass to send it to Example Owner now' in world.sends[-1][7]
    assert say(world, OTHER, 'pass', f'T{req} pass' if tagged else 'pass')['answers'] == []
    assert world.sends[-2][5] == OWNER
    assert 'Bob Example passed this to you' in world.sends[-2][7]
    assert world.sends[-1][5] == OTHER and 'sent to Example Owner' in world.sends[-1][7]
    assert world.app.get(req)['state'] == 'escalated'
    count = len(world.sends)
    world.advance(601)
    world.app.sync()
    world.app.sync()
    assert len(world.sends) == count
    assert say(world, OWNER, 'answer', f'T{req} B')['answers'] == [req]


def test_bare_pass_disambiguates_multiple_delegate_cards(world):
    world.seed()
    configure(world)
    req, _ = escalate(world)
    escalate(world, 'second')
    say(world, OTHER, 'ambiguous', 'pass')
    assert 'Which card?' in world.sends[-1][7] and world.sends[-1][5] == OTHER
    assert not world.app.rows('SELECT * FROM cards WHERE forward_outbox IS NOT NULL')
    say(world, OTHER, 'specific', f'T{req} pass')
    assert len(world.app.rows('SELECT * FROM cards WHERE forward_outbox IS NOT NULL')) == 1


def test_pass_on_a_delegate_requester_route_is_consumed(world):
    world.seed()
    configure(world)
    world.cfg['projects']['project-a']['escalate_to'] = 'alice'
    req, _ = escalate(world)
    count = len(world.app.rows('SELECT * FROM requests'))
    assert say(world, PHONE, 'pass', f'T{req} pass')['created'] == []
    world.app.sync()
    assert len(world.app.rows('SELECT * FROM requests')) == count
