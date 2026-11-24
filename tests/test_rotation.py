import json
import sqlite3

import pytest

from toucan import sources
from toucan.cli import main
from toucan.config import ValidationError, load, warnings
from toucan.core import App
from toucan.storage import connect


@pytest.fixture
def invoke(world, monkeypatch):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    monkeypatch.setenv('TOUCAN_DB', str(world.db_path))
    return lambda argv: main(argv, effects=world.fx, paths=world.paths)


def test_hook_resolution_fallback_and_config_warnings(world, invoke, capsys):
    configured = world.cfg['threads']['project-a']['t3_thread']
    assert world.app.live_thread('project-a') == configured
    world.t3_owner()
    assert sources.hook_thread(world.paths, world.hook.read_text()) == 'owner-thread'
    assert world.app.live_thread('project-a') == 'owner-thread'
    assert invoke(['check-config', '--json']) == 0
    result = capsys.readouterr().out
    assert json.loads(result)['warnings'] == ["project-a: t3_thread is stale; Toucan uses the hook's thread"]
    assert 'SECRET-TOKEN' not in result
    world.config_path.write_text(world.config_text.replace(f't3_thread = "{configured}"', ''))
    world.app = App(load(world.config_path), world.db, world.fx, world.paths)
    assert world.app.live_thread('project-a') == 'owner-thread'
    assert invoke(['check-config', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['warnings'] == []
    with sqlite3.connect(world.paths.t3) as db:
        db.execute('DELETE FROM scheduled_tasks')
    assert world.app.live_thread('project-a') is None
    assert invoke(['check-config', '--json']) == 0
    assert 'no T3 thread resolves' in capsys.readouterr().out
    world.paths.t3.unlink()
    assert sources.hook_thread(world.paths, world.hook.read_text()) is None
    assert not world.paths.t3.exists()


@pytest.mark.parametrize('url', [
    'https://hook.invalid/SECRET-TOKEN', 'https://hook.invalid/api/hooks/task/',
    'https://hook.invalid/api/hooks//SECRET-TOKEN', 'https://hook.invalid/api/hooks/missing/SECRET-TOKEN',
    'file:///api/hooks/task/SECRET-TOKEN', 'http://[broken/api/hooks/task/SECRET-TOKEN',
    'https://hook.invalid/api/hooks/task/SECRET-TOKEN/extra',
])
def test_invalid_or_unknown_hooks_do_not_resolve(world, url):
    world.t3_owner()
    assert sources.hook_thread(world.paths, url) is None


@pytest.mark.parametrize('change,match', [
    ('rotate_stall_minutes = 60', 'positive'),
    ('t3_thread = "11111111-1111-1111-1111-111111111111"', 'non-empty text'),
])
def test_rotation_config_validation(world, change, match):
    value = '0' if change.startswith('rotate_stall') else '""'
    world.config_path.write_text(world.config_text.replace(change, change.split(' = ')[0] + ' = ' + value))
    with pytest.raises(ValidationError, match=match):
        load(world.config_path)


@pytest.mark.parametrize('where', ['settings', 'thread'])
def test_old_rotate_tokens_settings_load_but_are_ignored_with_a_warning(world, where):
    marker = '[settings]' if where == 'settings' else '[threads.project-a]'
    world.config_path.write_text(world.config_text.replace(marker, f'{marker}\nrotate_tokens = 1000', 1))
    cfg = load(world.config_path)
    prefix = 'settings' if where == 'settings' else 'threads.project-a'
    assert [w for w in warnings(cfg, world.paths) if 'rotate_tokens' in w] == [
        f'{prefix}.rotate_tokens is ignored: Toucan no longer rotates threads by itself; hand over by hand with `toucan rotate`']
    world.seed()
    world.t3_owner(used=900000)
    app = App(cfg, world.db, world.fx, world.paths)
    assert not any(w.get('rotate') for w in app.sync()['wakes'])
    assert app.rotation('project-a') is None


@pytest.mark.parametrize('status', ['completed', 'running'])
def test_a_large_owner_thread_is_never_asked_to_rotate(world, status):
    world.seed()
    req = world.request()
    world.app.claim([req], 'project-a')
    world.t3_owner(used=900000, status=status)
    for _ in range(3):
        assert not any(w.get('rotate') for w in world.app.sync()['wakes'])
        world.advance(20 * 60)
    assert world.app.rotation('project-a') is None
    assert world.app.rows("SELECT * FROM events WHERE kind='rotation_requested'") == []


def test_new_requests_get_a_fresh_wake_budget_for_the_successor(world):
    world.seed()
    req = world.request()
    world.t3_owner(status='running')
    world.app.request_rotation('project-a', 'owner-thread', 412000)
    for _ in range(world.cfg['settings']['max_wakes'] + 1):
        world.app.sync()
        world.advance(20 * 60)
    assert world.app.rotation('project-a')
    assert world.app.get(req)['state'] == 'new' and world.app.get(req)['wakes'] == world.cfg['settings']['max_wakes']
    world.t3_owner(thread='successor', task='new-task', used=1000)
    world.app.sync()
    assert world.app.rotation('project-a') is None and world.app.get(req)['wakes'] == 0
    world.advance(3 * 60)
    world.app.sync()
    assert world.app.get(req)['wakes'] == 1 and world.hooks()[-1]['count'] == 1


@pytest.mark.parametrize('mode', ['dry', 'shadow'])
def test_rotation_obeys_dry_and_shadow_delivery_rules(world, mode):
    world.seed()
    world.t3_owner()
    db = connect(world.db_path, dry=True) if mode == 'dry' else world.db
    before = list(world.db.iterdump())
    app = App(world.cfg, db, world.fx, world.paths, dry=mode == 'dry', shadow=mode == 'shadow')
    try:
        app.request_rotation('project-a', 'owner-thread', 412000)
        assert app.rotation('project-a')
        world.t3_owner(thread='successor', task='new-task')
        app.sync()
        assert app.rotation('project-a') is None
        assert world.posts == []
        if mode == 'dry':
            assert list(world.db.iterdump()) == before
    finally:
        if mode == 'dry':
            db.close()


def test_rotation_stalls_alerts_once_then_completes_with_one_notice(world, invoke, capsys):
    world.seed()
    world.t3_owner()
    world.app.request_rotation('project-a', 'owner-thread', 412000)
    world.app.sync()
    world.advance(60 * 60)
    world.app.sync()
    world.app.sync()
    assert [a['text'] for a in world.alerts()] == [
        "project-a: rotation requested at 07:00 UTC hasn't finished; the hook still points at the old thread"]
    assert len([p for p in world.posts if p[2].get('Title') == 'Toucan needs attention']) == 1
    assert invoke(['status', '--json']) == 0
    status = json.loads(capsys.readouterr().out)['threads']['project-a']
    assert status['t3_thread'] == 'owner-thread' and status['context']['used_tokens'] == 412000
    assert status['rotation']['old'] == 'owner-thread'
    world.t3_owner(thread='successor', task='new-task', used=1000)
    world.app.sync()
    world.app.sync()
    assert world.app.rotation('project-a') is None and world.alerts() == []
    event = world.app.rows("SELECT * FROM events WHERE kind='rotated'")
    assert len(event) == 1
    assert json.loads(event[0]['detail_json']) == dict(old='owner-thread', new='successor', tokens=412000)
    notices = [p for p in world.posts if p[2].get('Title') == 'Toucan rotated a thread']
    assert len(notices) == 1 and notices[0][1] == b'project-a moved to a fresh thread; the old one had 412k tokens'


def test_manual_rotation_output_and_checks_preserve_open_work_without_tokens(world, invoke, capsys):
    world.seed()
    claimed, done, escalated = [world.request(str(i)) for i in range(3)]
    claim = world.app.claim([claimed, done, escalated], 'project-a')
    world.app.deliver(claimed, claim, text='On it', promise='send results', due=world.now + 7200)
    world.app.transition(done, claim, 'done', 'Work ready')
    world.app.deliver(escalated, claim, text='Need approval', escalation=True)
    world.t3_owner()
    assert invoke(['rotate', '--thread', 'project-a', '--check', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['context']['used_tokens'] == 412000
    assert world.app.rotation('project-a') is None
    assert invoke(['rotate', '--thread', 'project-a', '--json']) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result['old'] == 'owner-thread' and result['task_id'] == 'task:/project-a'
    assert result['origin'] == 'https://hook.invalid' and result['hook_file'] == str(world.hook)
    assert result['t3_db'] == str(world.paths.t3)
    assert [(r['id'], r['claim_id'], r['state']) for r in result['requests']] == [
        (claimed, claim, 'claimed'), (done, claim, 'done'), (escalated, None, 'escalated')]
    assert result['requests'][0]['promise'] == 'send results'
    assert result['requests'][0]['promise_due'] == world.now + 7200
    assert claim in result['instructions'] and 'task:/project-a' in result['successor_setup']
    assert 'send its result to the successor' in result['instructions']
    assert 'SECRET-TOKEN' not in output and world.hook.read_text() not in output
    assert invoke(['rotate', '--thread', 'project-a']) == 0
    assert capsys.readouterr().out.strip() == result['instructions']
    assert invoke(['rotate', '--thread', 'project-a', '--check', '--json']) == 3
    assert not json.loads(capsys.readouterr().out)['done']
    world.t3_owner(thread='successor', task='new-task', used=1000)
    world.hook.chmod(0o644)
    assert invoke(['rotate', '--thread', 'project-a', '--check', '--json']) == 3
    assert 'mode 600' in json.loads(capsys.readouterr().out)['message']
    world.hook.chmod(0o600)
    assert invoke(['rotate', '--thread', 'project-a', '--check', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['done']
    assert invoke(['rotate', '--thread', 'project-a', '--reset', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['reset']
    assert world.app.rotation('project-a') is None
    assert len(world.app.rows("SELECT * FROM events WHERE kind='rotation_reset'")) == 1


def test_unresolved_hook_cannot_complete_rotation_using_a_stale_configured_thread(world, invoke, capsys):
    world.seed()
    world.t3_owner()
    world.app.request_rotation('project-a', 'owner-thread', 412000)
    with sqlite3.connect(world.paths.t3) as db:
        db.execute('DELETE FROM scheduled_tasks')
    assert invoke(['rotate', '--thread', 'project-a', '--check', '--json']) == 3
    assert 'does not resolve' in json.loads(capsys.readouterr().out)['message']
    world.app.sync()
    assert world.app.rotation('project-a')['old'] == 'owner-thread'
    assert world.app.rows("SELECT * FROM events WHERE kind='rotated'") == []


@pytest.mark.parametrize('json_output', [False, True])
def test_wait_ignores_an_unavailable_hook_then_refuses_the_successor(world, invoke, capsys, json_output):
    world.seed()
    world.t3_owner()
    started = world.now

    def sleep(seconds):
        world.advance(seconds)
        if world.now - started < 60:
            world.hook.write_text('https://hook.invalid/SECRET-TOKEN')
        else:
            world.t3_owner(thread='successor', task='new-task', used=1000)

    world.fx.sleep = sleep
    assert invoke(['wait', '--thread', 'project-a', *(['--json'] if json_output else [])]) == 3
    output = capsys.readouterr()
    assert 'replaced by successor' in (output.out if json_output else output.err)
    assert world.now - started == 60 and not world.app.waiter_running('project-a')


def test_wait_refuses_replaced_owner_and_checks_at_most_every_thirty_seconds(world, invoke, capsys, monkeypatch):
    world.seed()
    world.t3_owner()
    checked = []
    resolve = sources.hook_file_thread

    def read(paths, path):
        checked.append(world.now)
        return resolve(paths, path)

    def sleep(seconds):
        world.advance(seconds)
        world.t3_owner(thread='successor', task='new-task', used=1000)

    monkeypatch.setattr(sources, 'hook_file_thread', read)
    world.fx.sleep = sleep
    assert invoke(['wait', '--thread', 'project-a', '--json']) == 3
    result = json.loads(capsys.readouterr().out)
    assert result['replaced'] == 'successor' and 'Do not start' in result['next_steps']
    assert 'stop now' in result['next_steps'] and 'no longer owns project-a' in result['next_steps']
    assert len(checked) == 2 and checked[1] - checked[0] == 30
    assert not world.app.waiter_running('project-a')


def test_rate_limits_and_resume_use_the_hook_owner(world):
    world.seed()
    req = world.request()
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Need approval', escalation=True)
    world.t3_owner(used=1000)
    world.t3('answer', world.now + 1, 'go ahead', thread='owner-thread')
    assert world.app.resume(req, 'project-a')['owner_said'] == ['go ahead']
    world.t3('limit', world.now + 2, 'API Error: 429 rate_limit_error', thread='owner-thread', role='assistant')
    assert 'limited:project-a' in world.app.sync()['problems']
