"""Ledger timeline, Preflight checks before sends, and the GitHub collector."""
import json
import re
import subprocess

import pytest

from conftest import OTHER, PHONE
from toucan import ledger
from toucan.cli import main
from toucan.config import Refused, ValidationError, load
from toucan.core import App

WIREFRAMES = 'Sprint 1.2 wireframes: https://excalidraw.com/#json=abc'


def setup(world, mode='shadow', routes='', **extra):
    """alice is Products, Bob (person `other`) is the PEL. Both have WhatsApp chats routed to project-a."""
    options = dict(mode=mode, people=['alice', 'other'], pel='other', review_hosts=['excalidraw.com']) | extra
    body = '\n'.join(f'{k} = {json.dumps(v)}' for k, v in options.items())
    text = re.sub(r'(\[people\.other\]\n(?:[^\[\n].*\n)*?)name = "Alice Example"', r'\1name = "Bob Example"',
                  world.config_text) + f'''
[[routes]]
name = "bob-whatsapp"
source = "whatsapp"
chats = ["{OTHER}"]
thread = "project-a"
mode = "act"
requesters = ["other"]
active_from = "2026-10-17T00:00:00+05:00"
{routes}
[projects.project-a]
name = "Project A"
thread = "project-a"

[projects.project-a.ledger]
{body}
'''
    world.config_path.write_text(text)
    world.cfg = load(world.config_path)
    world.app = App(world.cfg, world.db, world.fx, world.paths)
    world.seed()


def to_alice(world, text, **kw):
    return world.app.send('alice-whatsapp', 'project-a', text=text, chat=PHONE, **kw)


def to_bob(world, text, **kw):
    return world.app.send('bob-whatsapp', 'project-a', text=text, **kw)


def checks(world):
    found = [json.loads(e['detail_json']) for e in world.app.rows("SELECT detail_json FROM events WHERE kind='preflight' ORDER BY id")]
    return [{k: v for k, v in c.items() if k != 'outbox'} for c in found]


def rules(check):
    return [r['rule'] for r in check['reasons']]


def test_shadow_records_a_hold_but_sends(world):
    setup(world)
    to_alice(world, WIREFRAMES)
    assert world.sends[-1][5] == PHONE
    [check] = checks(world)
    assert check['verdict'] == 'hold' and rules(check) == ['pel_first'] and check['mode'] == 'shadow'
    assert check['people'] == ['alice'] and check['topics'] == ['sprint:1.2']


def test_enforce_refuses_until_overridden(world):
    setup(world, mode='enforce')
    with pytest.raises(Refused, match='has not gone to Bob Example'):
        to_alice(world, WIREFRAMES)
    assert world.sends == [] and world.app.rows('SELECT * FROM outbox') == []
    to_alice(world, WIREFRAMES, override='client call in five minutes')
    assert world.sends[-1][5] == PHONE
    assert checks(world)[-1]['override'] == 'client call in five minutes'


def test_pel_first_then_reply_or_wait(world):
    setup(world, mode='enforce')
    to_bob(world, WIREFRAMES)
    assert checks(world)[-1]['verdict'] == 'pass'
    world.advance(60)
    with pytest.raises(Refused, match='has not replied'):
        to_alice(world, 'Sprint 1.2 wireframes for your review: https://excalidraw.com/#json=abc')
    world.wa('bob-ok', chat_jid=OTHER, sender=OTHER, text='Looks fine')
    world.advance(1)
    world.app.sync()
    to_alice(world, 'Sprint 1.2 wireframes for your review: https://excalidraw.com/#json=abc')
    assert checks(world)[-1]['verdict'] == 'pass'


def test_pel_wait_passes_without_reply(world):
    setup(world, mode='enforce', pel_wait_minutes=30)
    to_bob(world, WIREFRAMES)
    world.advance(31 * 60)
    to_alice(world, WIREFRAMES)
    assert checks(world)[-1]['verdict'] == 'pass'


def test_same_link_counts_without_a_topic(world):
    setup(world, mode='enforce')
    to_bob(world, 'Wireframes: https://excalidraw.com/#json=xyz')
    world.advance(31 * 60)
    to_alice(world, 'Wireframes for you: https://excalidraw.com/#json=xyz')
    with pytest.raises(Refused, match='pel|PEL'):
        to_alice(world, 'Other wireframes: https://excalidraw.com/#json=different')


def test_review_link_off_the_review_hosts(world):
    setup(world)
    to_bob(world, 'Sprint 1.2 mock-ups: https://demo-1.example.app and proof https://github.com/example/repo/issues/1')
    check = checks(world)[-1]
    assert rules(check) == ['review_link'] and 'demo-1.example.app' in check['reasons'][0]['text']
    assert 'github.com' not in check['reasons'][0]['text']


def test_supersede_and_burst(world):
    setup(world, burst_per_hour=2)
    to_alice(world, 'These replace the page I sent earlier')
    assert rules(checks(world)[-1]) == ['supersede']
    to_bob(world, 'These replace the frames you held')
    assert checks(world)[-1]['verdict'] == 'pass'
    to_alice(world, 'Second update')
    assert checks(world)[-1]['verdict'] == 'pass'
    to_alice(world, 'Third update')
    assert rules(checks(world)[-1]) == ['burst']
    world.advance(3601)
    to_alice(world, 'Next hour')
    assert checks(world)[-1]['verdict'] == 'pass'


def test_a_conversation_is_not_a_burst(world):
    setup(world, burst_per_hour=2)
    to_alice(world, 'First answer')
    to_alice(world, 'Second answer')
    world.advance(5)
    world.wa('alice-more', text='One more question')
    world.advance(1)
    world.app.sync()
    to_alice(world, 'Third answer')
    assert checks(world)[-1]['verdict'] == 'pass'


def test_other_threads_sends_are_not_this_projects(world):
    from toucan.storage import event, insert
    setup(world, burst_per_hour=1)
    out = insert(world.db, 'outbox', attempt_id='x', kind='send', source='whatsapp', to_chat=PHONE, text='Another project',
                 state='sent', created_at=world.now, updated_at=world.now)
    event(world.db, world.now, 'outbox_pending', 'project-b', None, outbox=out, delivery_kind='send')
    to_alice(world, 'Only message from this project')
    assert checks(world)[-1]['verdict'] == 'pass'
    assert [i['text'] for i in ledger.timeline(world.app, 'project-a', 0)] == ['Only message from this project']
    assert ledger.replay(world.app, 'project-a', 0)['checked'] == 1


def test_sends_to_people_outside_the_project_are_not_checked(world):
    setup(world, people=['other'], pel='other')
    to_alice(world, WIREFRAMES)
    assert checks(world) == []


def test_a_broken_check_never_blocks(world, monkeypatch):
    setup(world, mode='enforce')
    monkeypatch.setattr(ledger, 'evaluate', lambda *a, **k: 1 / 0)
    to_alice(world, WIREFRAMES)
    assert world.sends[-1][5] == PHONE
    assert checks(world)[-1] == dict(project='project-a', verdict='error', error='ZeroDivisionError', mode='enforce',
                                     override=None)


def test_router_questions_skip_preflight(world):
    setup(world, mode='enforce', burst_per_hour=1)
    to_alice(world, 'one')
    out = world.app._queue_delivery(None, 'project-a', 'send', 'whatsapp', PHONE, 'Which project?', None, asks_request=1)
    assert out and len(checks(world)) == 1


def test_teams_addressing():
    cfg = dict(settings=dict(owner_teams_id='owner-id'), people=dict(
        alice=dict(name='A', teams_id='alice-id', whatsapp=[]), bob=dict(name='B', teams_id='bob-id', whatsapp=[]),
        me=dict(name='Owner', teams_id='owner-id', whatsapp=[])))
    assert ledger.addressed(cfg, '19:owner-id_alice-id@unq.gbl.spaces', 'hi') == ['alice']
    assert sorted(ledger.addressed(cfg, '19:group@thread.v2', '@[bob-id] and @[alice] see this')) == ['alice', 'bob']
    assert ledger.addressed(cfg, '19:group@thread.v2', 'nobody tagged', ['bob']) == ['bob']


def test_replay_and_show(world):
    setup(world)
    to_alice(world, WIREFRAMES)
    world.advance(60)
    to_bob(world, WIREFRAMES)
    world.wa('alice-q', text='What about Sprint 1.2 dates?')
    world.advance(1)
    world.app.sync()
    result = ledger.replay(world.app, 'project-a', world.now - 86400)
    assert result['checked'] == 2 and result['held'] == 1 and result['sends'][0]['rules'] == ['pel_first']
    items = ledger.show(world.app, 'project-a', world.now - 86400, topic='1.2')
    assert [(i['direction'], i['people']) for i in items] == [('out', ['alice']), ('out', ['other']), ('in', ['alice'])]
    assert ledger.show(world.app, 'project-a', world.now - 86400, person='other')[0]['direction'] == 'out'
    assert ledger.recent(world.app, 'project-a') == [
        'Alice Example: 1 message(s) from Toucan in the last 2 hours about sprint:1.2',
        'Bob Example: 1 message(s) from Toucan in the last 2 hours about sprint:1.2']


def gh_world(world, pages, code=0):
    """Answer `gh api` calls from pages; anything else goes to the world's usual runner."""
    calls = []
    plain = world.fx.run

    def run(argv, timeout=None):
        if argv[0] != 'gh':
            return plain(argv)
        calls.append(argv)
        kind = 'issue' if '/issues?' in argv[-1] else 'comment' if '/issues/comments?' in argv[-1] else 'review_comment'
        return subprocess.CompletedProcess(argv, code, json.dumps(pages.get(kind, [])) if code == 0 else '', '')
    world.fx.run = run
    return calls


def test_github_collector_is_keyed_and_paced(world):
    world.config_text = world.config_text.replace('[people.other]', '[people.other]\ngithub = ["Bob-gh"]')
    setup(world, github=['example/repo'], self_github=['owner-gh'])
    stamp = '2026-10-17T06:00:00Z'
    pages = dict(issue=[dict(id=1, number=12, title='Sprint 1.2 bug', body='@bob-gh please look', user=dict(login='owner-gh'),
                             created_at=stamp, updated_at=stamp, html_url='https://github.com/example/repo/issues/12')],
                 comment=[dict(id=7, issue_url='https://api.github.com/repos/example/repo/issues/12', body='Fixed on dev',
                               user=dict(login='bob-gh'), created_at=stamp, updated_at=stamp, html_url='u')])
    calls = gh_world(world, pages)
    world.advance(601)
    assert world.app.sync()['ledger'] == dict(stored=2, errors=[])
    assert len(calls) == 3 and 'since=' in calls[0][-1]
    assert ledger.github_sync(world.app) == dict(skipped='ran recently')
    assert ledger.github_sync(world.app, force=True)['stored'] == 2
    assert world.app.db.execute('SELECT count(*) FROM ledger_github').fetchone()[0] == 2
    assert world.app.db.execute("SELECT value FROM meta WHERE key='ledger-github:example/repo:comment'").fetchone()[0] == stamp
    items = [i for i in ledger.timeline(world.app, 'project-a', 0) if i['platform'] == 'github']
    assert [(i['direction'], i['people'], i['topics']) for i in items] == [
        ('out', ['other'], ['sprint:1.2', 'issue:12']), ('in', ['other'], ['issue:12'])]


def test_github_failure_is_logged_not_raised(world):
    setup(world, github=['example/repo'])
    gh_world(world, {}, code=1)
    report = ledger.github_sync(world.app, force=True)
    assert len(report['errors']) == 3 and 'exit 1' in report['errors'][0]['error']
    assert world.app.rows("SELECT * FROM events WHERE kind='ledger_github_error'")
    assert ledger.status(world.app)['github_last_ok'] is None


@pytest.mark.parametrize('extra, message', [
    ('wobble = 1', 'unknown ledger key wobble'),
    ('pel = "alice"\npeople = ["other"]', 'pel must be one of ledger.people'),
    ('topics = {sprint = "(a)(b)"}', 'at most one group'),
    ('supersede = ["("]', 'not a valid regular expression'),
    ('mode = "loud"', 'mode must be'),
    ('github = ["not a repo"]', 'owner/name'),
])
def test_config_validation(world, extra, message):
    world.config_path.write_text(world.config_text + f'\n[projects.project-a]\nname = "A"\nthread = "project-a"\n\n'
                                 f'[projects.project-a.ledger]\n{extra}\n')
    with pytest.raises(ValidationError, match=message):
        load(world.config_path)


def test_defaults_take_the_escalation_delegate_as_pel(world):
    world.config_path.write_text(world.config_text + '\n[projects.project-a]\nname = "A"\nthread = "project-a"\n'
                                 'escalate_to = "other"\n\n[projects.project-a.ledger]\npeople = ["alice", "other"]\n')
    project = load(world.config_path)['projects']['project-a']
    assert project['ledger']['pel'] == 'other' and project['ledger']['mode'] == 'shadow'


def test_github_logins_are_unique(world):
    world.config_path.write_text(world.config_text.replace('[people.other]', '[people.other]\ngithub = ["same"]')
                                 .replace('[people.alice]', '[people.alice]\ngithub = ["Same"]'))
    with pytest.raises(ValidationError, match='multiple people'):
        load(world.config_path)


def test_cli_show_and_override(world, monkeypatch, capsys):
    setup(world, mode='enforce')
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    run = lambda *argv: main(list(argv), effects=world.fx, paths=world.paths)
    assert run('send', '--thread', 'project-a', '--route', 'alice-whatsapp', '--chat', PHONE, '--text', WIREFRAMES) == 3
    assert 'Preflight held this send' in capsys.readouterr().err
    assert run('send', '--thread', 'project-a', '--route', 'alice-whatsapp', '--chat', PHONE, '--text', WIREFRAMES,
                 '--override', 'agreed with the PEL on a call') == 0
    capsys.readouterr()
    assert run('ledger', 'show', '--project', 'project-a') == 0
    assert 'Alice Example  [sprint:1.2]' in capsys.readouterr().out
    assert run('ledger', 'replay', '--project', 'project-a', '--json') == 0
    assert json.loads(capsys.readouterr().out)['held'] == 1


GROUP = '155500000099@g.us'
GROUP_ROUTE = f'''
[[routes]]
name = "wa-group"
source = "whatsapp"
chats = ["{GROUP}"]
thread = "project-a"
mode = "act"
requesters = ["alice"]
active_from = "2026-10-17T00:00:00+05:00"
'''


def test_whatsapp_tags_are_credited_to_the_tagged_person(world):
    setup(world, mode='enforce', routes=GROUP_ROUTE)
    world.app.send('wa-group', 'project-a', text='@[other] ' + WIREFRAMES)
    stored = world.app.rows('SELECT text FROM outbox')[-1]['text']
    assert '@[' not in stored and stored.startswith('Bob Example')
    assert [i['people'] for i in ledger.timeline(world.app, 'project-a', 0)] == [['other']]
    world.advance(31 * 60)
    to_alice(world, WIREFRAMES)
    assert checks(world)[-1]['verdict'] == 'pass'
    assert ledger.replay(world.app, 'project-a', 0)['held'] == 0


def test_an_empty_pel_turns_the_check_off(world):
    world.config_path.write_text(world.config_text + '\n[projects.project-a]\nname = "A"\nthread = "project-a"\n'
                                 'escalate_to = "other"\n\n[projects.project-a.ledger]\npeople = ["alice", "other"]\npel = ""\n')
    assert load(world.config_path)['projects']['project-a']['ledger']['pel'] == ''


def test_a_refused_send_is_recorded(world):
    setup(world, mode='enforce')
    with pytest.raises(Refused):
        to_alice(world, WIREFRAMES)
    [check] = checks(world)
    assert check['verdict'] == 'hold' and check['refused'] is True and world.app.rows('SELECT * FROM outbox') == []
    assert ledger.status(world.app)['preflight_7d'] == [dict(project='project-a', verdict='hold', count=1)]


def stamp(minute):
    return f'2026-10-17T06:{minute:02d}:00Z'


def test_github_reads_on_from_the_cursor_not_by_page(world):
    setup(world, github=['example/repo'])
    first = [dict(id=i, number=1, title='t', body='', user=dict(login='x'), created_at=stamp(0), updated_at=stamp(i // 10),
                  html_url='u') for i in range(100)]
    rest = [dict(id=200, number=2, title='t', body='', user=dict(login='x'), created_at=stamp(0), updated_at=stamp(30),
                 html_url='u')]
    calls = []

    def run(argv, timeout=None):
        if argv[0] != 'gh':
            return subprocess.CompletedProcess(argv, 0, '', '')
        calls.append(argv[-1])
        assert world.app.db.execute("SELECT 1 FROM meta WHERE key='ledger-github:last'").fetchone()
        page = [] if '/issues?' not in argv[-1] else first if len(calls) == 1 else rest
        return subprocess.CompletedProcess(argv, 0, json.dumps(page), '')
    world.fx.run = run
    assert ledger.github_sync(world.app, force=True) == dict(stored=101, errors=[])
    assert '&page=' not in calls[0] and calls[1].endswith('since=' + stamp(9))


def test_github_stops_at_its_time_budget(world):
    setup(world, github=['example/repo'])

    def run(argv, timeout=None):
        world.now += 61
        return subprocess.CompletedProcess(argv, 0, '[]', '')
    world.fx.run = run
    report = ledger.github_sync(world.app, force=True)
    assert report['stored'] == 0 and [e['kind'] for e in report['errors']] == ['comment', 'review_comment']
