import json
import sqlite3
import sys

import pytest

from toucan import effects
from toucan.cli import main
from toucan.config import Refused, load
from toucan.core import App
from toucan.storage import connect
from conftest import OWNER, PHONE
from test_router import answer, ask, recorder, routed
from test_sync import LIMIT

PARTS = [dict(project='project-a', text='fix the portal'), dict(project='project-b', text='rerun billing')]
SPLIT = dict(kind='split', projects=['project-a', 'project-b'], parts=PARTS)


def live(world, commands=(), settings=''):
    """The router test config with both routes live."""
    original = world.config_text
    routed(world, commands, settings, route='live')
    world.original_text = original
    return world


def switch(world, mode):
    world.config_path.write_text(world.config_text.replace('router = "live"', f'router = "{mode}"'))
    world.cfg = load(world.config_path)
    world.app = App(world.cfg, world.db, world.fx, world.paths)


def hold_stage(world):
    """Another sync holds the router lease, so this one's router stage decides nothing."""
    world.db.execute("INSERT OR REPLACE INTO meta VALUES ('router:lease', ?)", (json.dumps(world.now + 7200),))


def history(world, req):
    return [(r['step'], r['kind'], r['decision'], json.loads(r['threads_json']), r['mode'])
            for r in world.app.rows('SELECT * FROM router_decisions WHERE request_id=? ORDER BY id', (req,))]


def request(world, req):
    row = world.app.get(req)
    return row['thread'], row['routing'], row['state']


def bot_route(world):
    route = next(r for r in world.cfg['routes'] if r['name'] == 'group')
    route.pop('reply_via_whatsapp')
    route['post_as_bot'] = True
    return route


def unsure_unless_weather():
    """A router command: offtopic for weather, otherwise unsure between both projects."""
    return [sys.executable, '-c', 'import json, sys; d = json.loads(sys.stdin.read().split("\\nDATA:\\n", 1)[1]); '
            'w = "weather" in d["new_message"]["text"]; print(json.dumps(dict(kind="offtopic" if w else "unsure", '
            'projects=[] if w else ["project-a", "project-b"], parts=[], reason="test")))']


# Live routing

def test_rules_route_inside_sync_before_the_wakes(world):
    live(world).seed()
    world.wa('m1', text='The billing page is broken')
    report = world.app.sync()
    req = report['created'][0]
    assert request(world, req) == ('project-b', None, 'new')
    assert world.app.get(req)['wakes'] == 1 and world.app.get(req)['route_reason'] == 'names Project B'
    assert [h['thread'] for h in world.hooks()] == ['project-b']
    assert history(world, req) == [(2, 'project', 'route', ['project-b'], 'live')]
    assert report['router']['decided'] == [] and report['router']['calls'] == 0
    world.advance(30 * 60)
    follow = ask(world, 'm2', 'and the totals?')
    assert request(world, follow)[0] == 'project-b' and history(world, follow)[0][:2] == (3, 'followup')


def test_a_held_request_is_not_woken_then_is_routed_and_woken_in_the_same_sync(world):
    live(world, [answer(projects=['project-b'])]).seed()
    seen = []

    def call(argv, text, timeout):
        seen.append((world.app.rows('SELECT thread,routing,wakes FROM requests'), world.hooks()))
        return effects.call(argv, text, timeout)

    world.fx.call = call
    world.wa('m1', text='hello, can you look at this?')
    report = world.app.sync()
    req = report['created'][0]
    assert seen == [([dict(thread='project-a', routing='pending', wakes=0)], [])]
    assert request(world, req) == ('project-b', None, 'new') and world.app.get(req)['wakes'] == 1
    assert [h['thread'] for h in world.hooks()] == ['project-b']
    assert history(world, req) == [(4, 'project', 'route', ['project-b'], 'live')]
    assert world.app.get(req)['route_reason'] == 'the router command said: test'
    assert world.app.wait('project-b', 1)['requests'] == [req]


def test_a_request_not_decided_in_time_goes_to_the_route_thread_and_alerts(world, monkeypatch, capsys):
    live(world, [answer(projects=['project-b'])]).seed()
    hold_stage(world)
    world.wa('m1', text='hello')
    req = world.app.sync()['created'][0]
    assert request(world, req) == ('project-a', 'pending', 'new') and world.hooks() == []
    with pytest.raises(Refused):
        world.app.claim([req], 'project-a')
    with pytest.raises(Refused, match='being routed'):
        world.app.forward(req, 'project-b', 'project-a')
    assert world.app.pending('project-a') == []  # so `toucan wait` doesn't fire
    assert {m['msg_id']: m['request_id'] for m in world.app.inbox('project-a')} == {'m1': None}
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    assert main(['status', '--json'], effects=world.fx, paths=world.paths) == 0
    status = json.loads(capsys.readouterr().out)
    assert [(h['id'], h['routing']) for h in status['held']] == [(req, 'pending')]
    assert status['router']['held'] == 1 and status['router']['live_routes'] == ['alice-whatsapp', 'group']
    assert status['requests'] == []
    world.advance(4 * 60)
    world.app.sync()
    assert request(world, req)[1] == 'pending'
    world.advance(60)
    report = world.app.sync()
    assert request(world, req) == ('project-a', None, 'new')
    assert world.app.get(req)['route_reason'] == 'router timed out'
    assert [h['thread'] for h in world.hooks()] == ['project-a']
    assert f'router:timeout:{req}' in report['problems'] and f'router:timeout:{req}' in {a['key'] for a in world.alerts()}
    assert history(world, req) == [(5, 'timeout', 'route', ['project-a'], 'live')]


@pytest.mark.parametrize('mode', ['shadow', 'off'])
def test_switching_a_route_off_live_hands_held_requests_to_its_thread(world, mode):
    live(world, [answer(projects=['project-b'])]).seed()
    hold_stage(world)
    world.wa('m1', text='hello')
    req = world.app.sync()['created'][0]
    world.advance(60)
    switch(world, mode)
    world.app.sync()
    assert request(world, req) == ('project-a', None, 'new')
    assert [h['thread'] for h in world.hooks()] == ['project-a']
    assert history(world, req) == [(0, 'released', 'route', ['project-a'], 'live')]
    assert world.app.get(req)['route_reason'] == f'router switched to {mode}'
    world.db.execute("DELETE FROM meta WHERE key='router:lease'")
    world.advance(60)
    world.app.sync()
    assert len(history(world, req)) == 1  # shadow doesn't decide it again


def test_held_requests_are_not_handed_over_while_the_route_thread_is_rate_limited(world):
    backup = world.root / 'backup.hook'
    backup.write_text('https://hook.invalid/api/hooks/backup/BACKUP')
    backup.chmod(0o600)
    world.config_text = (world.config_text.replace('[threads.project-a]\n', '[threads.project-a]\nfallback = "backup"\n') +
                         f'\n[threads.backup]\nt3_thread = "backup-thread"\nhook_file = "{backup}"\n')
    live(world, [answer(projects=['project-a'])]).seed()
    world.t3('error', world.now, LIMIT, role='assistant')
    hold_stage(world)
    world.wa('m1', text='hello')
    req = world.app.sync()['created'][0]
    world.advance(4 * 60 + 30)
    world.app.sync()
    assert request(world, req) == ('project-a', 'pending', 'new')
    world.db.execute("DELETE FROM meta WHERE key='router:lease'")
    world.app.sync()
    assert request(world, req) == ('project-a', None, 'new')
    world.advance(5 * 60)
    world.app.sync()
    assert request(world, req)[0] == 'backup'  # the handover applies once it is assigned


def test_dry_run_on_a_live_route_sends_and_writes_nothing(world, tmp_path):
    live(world, [recorder(tmp_path / 'prompt.txt', kind='unsure')]).seed()
    world.wa('m1', text='billing broke')
    world.wa('m2', text='portal or billing?', ts=world.now + 1)
    db = connect(world.db_path, dry=True)
    report = App(world.cfg, db, world.fx, world.paths, dry=True).sync()
    db.close()
    assert report['router']['dry_run'] is True and len(report['router']['would_call_router']) == 1
    assert world.sends == [] and not (tmp_path / 'prompt.txt').exists()
    assert world.app.rows('SELECT * FROM requests') == []


def test_shadow_routes_never_ask(world):
    routed(world, [answer(kind='unsure', projects=['project-a', 'project-b'])]).seed()
    req = ask(world, 'm1', 'portal or billing?')
    assert world.sends == [] and request(world, req) == ('project-a', None, 'new')
    assert history(world, req) == [(4, 'unsure', 'ask', [], 'shadow')]


# Bounce

def test_bounce_reroutes_without_the_thread_that_bounced_then_escalates(world, tmp_path):
    live(world, [recorder(tmp_path / 'prompt.txt', projects=['project-a'])]).seed()
    req = ask(world, 'm1', 'billing is broken')
    assert request(world, req)[0] == 'project-b'
    claim = world.app.claim([req], 'project-b')
    with pytest.raises(Refused, match='claim'):
        world.app.bounce(req, 'project-b', 'not billing')
    with pytest.raises(Refused, match='not found'):
        world.app.bounce(req, 'project-a', 'not mine', claim)
    assert world.app.bounce(req, 'project-b', 'this is the portal', claim) == dict(request=req, bounced=1, state='routing')
    assert request(world, req) == ('project-a', 'pending', 'new') and world.app.get(req)['claim_id'] is None
    world.advance(3 * 60)
    world.app.sync()
    # Step 2 names project-b, which bounced it, so the router command decides without it.
    data = json.loads((tmp_path / 'prompt.txt').read_text().split('\nDATA:\n', 1)[1])
    assert data['excluded_projects'] == ['project-b']
    assert request(world, req) == ('project-a', None, 'new') and world.hooks()[-1]['thread'] == 'project-a'
    assert [h[:2] for h in history(world, req)] == [(2, 'project'), (4, 'project')]
    result = world.app.bounce(req, 'project-a', 'not the portal either')
    assert result == dict(request=req, bounced=2, state='escalated', thread='project-a')
    assert world.sends[-1][5] == OWNER and 'project-b, project-a said it is not theirs' in world.sends[-1][7]
    assert request(world, req) == ('project-a', None, 'escalated')
    assert [json.loads(e['detail_json'])['count'] for e in world.app.rows("SELECT * FROM events WHERE kind='bounced'")] == [1, 2]


def test_an_answer_that_picks_a_thread_that_bounced_becomes_a_question(world):
    live(world, [answer(kind='offtopic', reason='weather')]).seed()
    req = ask(world, 'm1', 'how is the weather?')
    assert request(world, req)[0] == 'project-b'  # the general thread
    world.app.bounce(req, 'project-b', 'not a project question')
    world.app.sync()
    assert world.sends == [['wacli', '--json', 'send', 'text', '--to', PHONE, '--message',
                            'Which project is this about? Project A or Project B?']]
    assert request(world, req) == ('project-a', 'asked', 'new')
    assert history(world, req)[-1][:3] == (4, 'offtopic', 'ask')


def test_bounce_is_refused_where_the_router_is_not_live(world, monkeypatch, capsys):
    routed(world).seed()
    req = world.request('m1')
    with pytest.raises(Refused, match='toucan forward'):
        world.app.bounce(req, 'project-a', 'not mine')
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    assert main(['bounce', str(req), '--thread', 'project-a', '--reason', 'not mine', '--json'],
                effects=world.fx, paths=world.paths) == 3
    assert 'toucan forward' in capsys.readouterr().err
    assert world.app.rows("SELECT * FROM events WHERE kind='bounced'") == []


def test_bounce_cli_and_inbox_hint(world, monkeypatch, capsys):
    live(world).seed()
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    req = ask(world, 'm1', 'billing is broken')
    assert main(['inbox', '--thread', 'project-b'], effects=world.fx, paths=world.paths) == 0
    text = capsys.readouterr().out
    assert 'toucan bounce REQ --thread project-b' in text and 'routed here because "names Project B"' in text
    assert main(['bounce', str(req), '--thread', 'project-b', '--reason', 'portal', '--json'], effects=world.fx, paths=world.paths) == 0
    assert json.loads(capsys.readouterr().out) == dict(request=req, bounced=1, state='routing')


# Split

def test_a_split_makes_one_request_per_part_and_replies_say_which_project(world, monkeypatch, capsys):
    live(world, [answer(**SPLIT)]).seed()
    world.wa('m1', text='fix the portal and rerun billing')
    first = world.app.sync()['created'][0]
    rows = world.app.rows('SELECT id,thread,part,part_label,message_id,routing FROM requests ORDER BY id')
    assert [(r['thread'], r['part'], r['part_label'], r['routing']) for r in rows] == [
        ('project-a', 'fix the portal', 'Project A', None), ('project-b', 'rerun billing', 'Project B', None)]
    assert rows[0]['id'] == first and rows[0]['message_id'] == rows[1]['message_id']
    second = rows[1]['id']
    assert sorted(h['thread'] for h in world.hooks()) == ['project-a', 'project-b']
    assert history(world, second) == [(4, 'split', 'route', ['project-b'], 'live')]
    mine = [m for m in world.app.inbox('project-a') if m['msg_id'] == 'm1']
    assert [(m['request_id'], m['part'], m['other_request'], m['other_thread']) for m in mine] == [
        (first, 'fix the portal', second, 'project-b')]
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    assert main(['inbox', '--thread', 'project-b'], effects=world.fx, paths=world.paths) == 0
    text = capsys.readouterr().out
    assert '| This request is only this part of the message: rerun billing' in text
    assert 'replies start with "Project B:" automatically; thread project-a has another part' in text
    a = world.app.claim([first], 'project-a')
    b = world.app.claim([second], 'project-b')
    world.app.deliver(first, a, text='On it', promise='portal fix', due=world.now + 3600)
    result = world.app.deliver(second, b, text='Will do', promise='billing rerun', due=world.now + 3600)
    assert result['state'] == 'not_sent' and world.app.get(second)['promise'] == 'billing rerun'
    world.app.deliver(second, b, text='Project B: rerun done')
    world.app.deliver(first, a, text='Fixed')
    assert [s[7] for s in world.sends] == ['Project A: On it', 'Project B: rerun done', 'Project A: Fixed']
    assert [request(world, r)[2] for r in (first, second)] == ['closed', 'closed']
    review = {r['request']: r for r in main_json(world, monkeypatch, capsys, 'router', 'review')}
    assert review[second]['part'] == 'rerun billing' and review[first]['mode'] == 'live'


def main_json(world, monkeypatch, capsys, *args):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    assert main([*args, '--json'], effects=world.fx, paths=world.paths) == 0
    return json.loads(capsys.readouterr().out)


def test_parts_on_one_thread_stay_one_request(world):
    live(world, [answer(**SPLIT)])
    world.cfg['projects']['project-b']['thread'] = 'project-a'  # both projects on one thread
    world.seed()
    world.wa('m1', text='fix the portal and rerun billing')
    req = world.app.sync()['created'][0]
    assert world.app.rows('SELECT thread,part FROM requests') == [dict(thread='project-a', part=None)]
    assert request(world, req) == ('project-a', None, 'new')


def test_split_parts_share_one_teams_reaction(world):
    live(world, [answer(**SPLIT)]).seed()
    bot_route(world)
    world.team('t1', text='fix the portal and rerun billing')
    world.app.sync()
    first, second = [r['id'] for r in world.app.rows('SELECT id FROM requests ORDER BY id')]
    a = world.app.claim([first], 'project-a')
    b = world.app.claim([second], 'project-b')
    world.app.react(first, a, 'eyes', thread='project-a')
    assert world.app.react(second, b, 'eyes', thread='project-b')['skipped'] == 'already reacted'
    assert world.app.react(first, a, 'eyes', remove=True, thread='project-a')['skipped'] == 'another part of this message is still open'
    world.app.transition(first, a, 'close', 'done', 'project-a')
    world.app.react(second, b, 'eyes', remove=True, thread='project-b')
    assert [(s[2], '--remove' in s) for s in world.sends] == [('react', False), ('react', True)]


def test_edits_and_deletes_of_a_split_message_reach_every_part(world):
    live(world, [answer(**SPLIT)]).seed()
    world.wa('m1', text='fix the portal and rerun billing')
    world.app.sync()
    with sqlite3.connect(world.wa_path) as db:
        db.execute("UPDATE messages SET text='fix the portal only',edited=1 WHERE msg_id='m1'")
    world.advance(60)
    world.app.sync()
    rows = world.app.rows('SELECT source_changed,change_pending FROM requests ORDER BY id')
    assert rows == [dict(source_changed=1, change_pending=1)] * 2
    assert len(world.app.rows("SELECT * FROM events WHERE kind='source_changed'")) == 2
    with sqlite3.connect(world.wa_path) as db:
        db.execute("UPDATE messages SET revoked=1 WHERE msg_id='m1'")
    world.advance(60)
    world.app.sync()
    deleted = [json.loads(e['detail_json'])['deleted'] for e in world.app.rows("SELECT * FROM events WHERE kind='source_changed'")]
    assert deleted == [False, False, True, True]


def test_a_split_message_is_no_single_thread_to_quote_or_follow(world):
    live(world, [answer(**SPLIT)]).seed()
    world.wa('m1', text='fix the portal and rerun billing')
    world.app.sync()
    world.cfg['settings']['router_commands'] = [answer(kind='offtopic', reason='weather')]
    world.advance(60)
    follow = ask(world, 'm2', 'and how is the weather?')
    assert history(world, follow) == [(4, 'offtopic', 'route', ['project-b'], 'live')]
    quote = ask(world, 'm3', 'any news?', quoted_msg_id='m1')
    assert history(world, quote)[0][:2] == (3, 'followup')  # not step 1: the quoted message went to two threads


def test_an_escalated_part_comes_back_alone(world):
    live(world, [answer(**SPLIT)]).seed()
    world.wa('m1', text='fix the portal and rerun billing')
    world.app.sync()
    first, second = [r['id'] for r in world.app.rows('SELECT id FROM requests ORDER BY id')]
    world.app.deliver(second, world.app.claim([second], 'project-b'), text='Which environment?', escalation=True)
    assert world.sends[-1][7].startswith('Escalation from thread project-b: Alice Example asked: rerun billing\n')
    world.wa('sent-id', chat_jid=OWNER, sender=None, from_me=1, text='Escalation from thread project-b: ...')
    world.advance(3 * 60)
    world.wa('answer', chat_jid=OWNER, sender=OWNER, sender_name='Example Owner', text='staging')
    world.advance(3 * 60)
    assert world.app.sync()['answers'] == [second]
    assert request(world, second) == ('project-b', None, 'new') and request(world, first) == ('project-a', None, 'new')


# Asking the person

def test_unsure_asks_once_and_the_answer_routes_the_request_without_a_new_one(world):
    live(world, [unsure_unless_weather()]).seed()
    world.wa('m1', text='can you look at the thing')
    req = world.app.sync()['created'][0]
    assert world.sends == [['wacli', '--json', 'send', 'text', '--to', PHONE, '--message',
                            'Which project is this about: Project A or Project B?']]
    assert request(world, req) == ('project-a', 'asked', 'new') and world.hooks() == []
    assert world.app.rows('SELECT request_id,kind,state FROM outbox') == [dict(request_id=None, kind='send', state='sent')]
    world.advance(60)
    world.wa('m2', text='and how is the weather?')
    other = world.app.sync()['created']
    assert len(other) == 1 and request(world, other[0])[:2] == ('project-b', None)
    assert request(world, req)[1] == 'asked'
    world.advance(60)
    world.wa('m3', text='Project A please')
    report = world.app.sync()
    assert report['created'] == []
    assert request(world, req) == ('project-a', None, 'new') and world.app.get(req)['route_reason'] == 'the person answered Project A'
    assert 'project-a' in [h['thread'] for h in world.hooks()]
    assert history(world, req) == [(4, 'unsure', 'ask', [], 'live'), (6, 'answer', 'route', ['project-a'], 'live')]
    assert len(world.sends) == 1
    assert {m['msg_id']: m['request_id'] for m in world.app.inbox('project-a')}['m3'] is None


def test_no_answer_in_time_goes_to_the_route_thread_without_asking_again(world):
    live(world, [answer(kind='unsure', projects=['project-a', 'project-b'])]).seed()
    world.wa('m1', text='can you look at the thing')
    req = world.app.sync()['created'][0]
    world.advance(14 * 60)
    world.app.sync()
    assert request(world, req)[1] == 'asked'
    world.advance(60)
    world.app.sync()
    assert request(world, req) == ('project-a', None, 'new') and world.app.get(req)['route_reason'] == 'no answer to the question'
    assert [h['thread'] for h in world.hooks()] == ['project-a'] and len(world.sends) == 1
    world.advance(60)
    late = ask(world, 'm2', 'it was billing')
    assert request(world, late)[0] == 'project-b'  # a normal request now, not an answer


def test_the_question_lists_the_persons_projects_or_else_all(world):
    live(world, [answer(kind='unsure')]).seed()
    ask(world, 'm0', 'billing please')
    world.advance(3 * 3600)
    ask(world, 'm1', 'something else')
    assert world.sends[-1][7] == 'Which project is this about? Project B?'


def test_a_question_counts_against_the_hourly_limit(world):
    live(world, [answer(kind='unsure', projects=['project-a', 'project-b'])]).seed()
    world.cfg['settings']['rate_per_hour'] = 1
    first = ask(world, 'm0', 'billing please')
    world.app.deliver(first, world.app.claim([first], 'project-b'), text='Done')
    req = ask(world, 'm1', 'portal or billing?')
    assert request(world, req) == ('project-a', None, 'new') and len(world.sends) == 1
    assert world.app.get(req)['route_reason'] == 'test; could not ask: recipient hourly rate limit reached'
    assert history(world, req)[-1][:3] == (6, 'fallback', 'route')


def test_a_failed_question_routes_the_request_at_once(world):
    live(world, [answer(kind='unsure', projects=['project-a', 'project-b'])]).seed()
    world.send_result = world.send_result.__class__([], 1, '', 'not connected')
    world.wa('m1', text='portal or billing?')
    req = world.app.sync()['created'][0]
    assert request(world, req) == ('project-a', None, 'new')
    assert world.app.get(req)['route_reason'] == 'the question could not be sent'
    assert world.hooks()[-1]['thread'] == 'project-a'


def test_routes_that_cannot_ask_fall_back_to_their_thread(world):
    live(world, [answer(kind='unsure', projects=['project-a', 'project-b'])]).seed()
    world.team('t1', text='portal or billing?')  # replies go to the asker's WhatsApp, not this chat
    req = world.app.sync()['created'][0]
    assert world.sends == [] and request(world, req) == ('project-a', None, 'new')
    assert "this route can't ask the person" in world.app.get(req)['route_reason']


def test_teams_bot_routes_ask_in_the_chat_and_read_the_answer_there(world):
    live(world, [answer(kind='unsure', projects=['project-a', 'project-b'])]).seed()
    bot_route(world)
    world.send_result = world.send_result.__class__([], 0, '{"activity_id":"question-1"}', '')
    world.team('t1', text='portal or billing?')
    req = world.app.sync()['created'][0]
    assert world.sends == [['toucan-bot', '--json', 'send', '--conversation', 'group:chat', '--text',
                            'Which project is this about: Project A or Project B?']]
    world.advance(60)
    world.team('t2', text='the billing one')
    assert world.app.sync()['created'] == []
    assert request(world, req) == ('project-b', None, 'new')


# Status, doctor and the migration

def test_doctor_reports_live_routes(world, monkeypatch, capsys):
    live(world, [[sys.executable]]).seed()
    hold_stage(world)
    world.wa('m1', text='hello')
    world.app.sync()
    world.advance(10 * 60)
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    main(['doctor', '--json'], effects=world.fx, paths=world.paths)
    item = {i['id']: i for i in json.loads(capsys.readouterr().out)}['router.health']
    assert item['status'] == 'warn' and 'live on alice-whatsapp, group' in item['message'] and '1 held' in item['message']


OLD_SCHEMA = '''
CREATE TABLE messages (
 id INTEGER PRIMARY KEY, source TEXT, account TEXT, chat TEXT, msg_id TEXT, route TEXT,
 sender_key TEXT, sender_raw TEXT, sender_name TEXT, is_owner INTEGER, ts REAL, text TEXT,
 attachments_json TEXT, edited INTEGER, deleted INTEGER, first_seen REAL, last_seen REAL,
 deferred_cutoff REAL, mentions_json TEXT, quoted_ids TEXT, own_text TEXT,
 UNIQUE(source,account,chat,msg_id));
CREATE TABLE requests (
 id INTEGER PRIMARY KEY, message_id INTEGER UNIQUE REFERENCES messages(id), route TEXT, thread TEXT,
 state TEXT CHECK(state IN ('new','claimed','done','closed','escalated')), claim_id TEXT,
 claimed_at REAL, done_at REAL, done_note TEXT, closed_at REAL, close_reason TEXT,
 reply_source TEXT, reply_chat TEXT, reply_person TEXT, mode TEXT, source_changed INTEGER DEFAULT 0,
 change_pending INTEGER DEFAULT 0, wakes INTEGER DEFAULT 0, last_wake_at REAL, created_at REAL{promise});
CREATE TABLE outbox (
 id INTEGER PRIMARY KEY, attempt_id TEXT UNIQUE, request_id INTEGER REFERENCES requests(id),
 kind TEXT CHECK(kind IN ('send','draft','escalation')), source TEXT, to_chat TEXT, text TEXT, file TEXT,
 state TEXT CHECK(state IN ('pending','sending','sent','failed','uncertain')),
 error TEXT, created_at REAL, updated_at REAL, external_id TEXT);
CREATE TABLE events (
 id INTEGER PRIMARY KEY, ts REAL, kind TEXT, thread TEXT, request_id INTEGER, detail_json TEXT);
CREATE TABLE owner_answers (
 message_id INTEGER PRIMARY KEY REFERENCES messages(id), request_id INTEGER REFERENCES requests(id));
CREATE TABLE router_decisions (
 request_id INTEGER PRIMARY KEY REFERENCES requests(id), mode TEXT, step INTEGER, kind TEXT, decision TEXT,
 projects_json TEXT, threads_json TEXT, parts_json TEXT, reason TEXT, command_index INTEGER, latency_ms INTEGER,
 created_at REAL, verdict TEXT CHECK(verdict IN ('right','wrong')), verdict_note TEXT, verdict_at REAL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX request_thread ON requests(thread,state);
'''


@pytest.mark.parametrize('promise', [True, False])
def test_migration_keeps_every_row_and_id_and_allows_several_requests_per_message(tmp_path, promise):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.executescript(OLD_SCHEMA.replace('{promise}', ', promise TEXT, promise_due REAL' if promise else ''))
        db.execute('PRAGMA foreign_keys=ON')
        for n in (3, 7):
            db.execute("INSERT INTO messages (id,source,account,chat,msg_id,route,text,ts) VALUES (?,'whatsapp','wa',?,?,'r',?,?)",
                       (n, PHONE, f'm{n}', f'text {n}', 1000 + n))
        db.execute("INSERT INTO requests (id,message_id,route,thread,state,reply_person,created_at,wakes) VALUES (5,3,'r','project-a','closed','alice',1,2)")
        db.execute("INSERT INTO requests (id,message_id,route,thread,state,reply_person,created_at,claim_id) VALUES (9,7,'r','project-b','claimed','bob',2,'c')")
        db.execute("INSERT INTO outbox (id,attempt_id,request_id,kind,state,to_chat) VALUES (4,'a',9,'send','sent',?)", (PHONE,))
        db.execute("INSERT INTO owner_answers VALUES (7,9)")
        db.execute("INSERT INTO router_decisions (request_id,mode,step,kind,decision,threads_json,verdict) VALUES (5,'shadow',2,'project','route','[\"project-a\"]','right')")
        db.execute("INSERT INTO events (id,kind,request_id) VALUES (11,'request_created',9)")
    before = {t: sqlite3.connect(path).execute(f'SELECT count(*) FROM {t}').fetchone()[0]
              for t in ('messages', 'requests', 'outbox', 'owner_answers', 'router_decisions', 'events')}
    db = connect(path)
    db2 = connect(path)  # a second process starting at the same time finds nothing to do
    db2.close()
    after = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in before}
    assert after == before
    rows = [dict(r) for r in db.execute('SELECT id,message_id,thread,state,wakes,claim_id,routing,part FROM requests ORDER BY id')]
    assert rows == [dict(id=5, message_id=3, thread='project-a', state='closed', wakes=2, claim_id=None, routing=None, part=None),
                    dict(id=9, message_id=7, thread='project-b', state='claimed', wakes=0, claim_id='c', routing=None, part=None)]
    decision = dict(db.execute('SELECT * FROM router_decisions').fetchone())
    assert (decision['id'], decision['request_id'], decision['verdict']) == (1, 5, 'right')
    db.execute("INSERT INTO requests (message_id,route,thread,state) VALUES (3,'r','project-b','new')")
    db.execute("INSERT INTO router_decisions (request_id,mode) VALUES (5,'live')")
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1 and db.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO requests (message_id,route,thread,state) VALUES (999,'r','project-b','new')")
    indexes = {r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {'request_thread', 'request_message', 'decision_request'} <= indexes
    assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_rebuild'").fetchall() == []
    db.close()
