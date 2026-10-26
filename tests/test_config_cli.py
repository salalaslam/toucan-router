import json
import subprocess

import pytest

from toucan.cli import main
from toucan.config import ValidationError, load
from conftest import OWNER, PHONE


@pytest.fixture
def send_cli(world, monkeypatch):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    monkeypatch.setenv('TOUCAN_DB', str(world.db_path))
    return lambda argv: main(argv, effects=world.fx, paths=world.paths)


@pytest.mark.parametrize('route,mode,bot,expected', [
    ('group', 'act', True,
     ['toucan-bot', '--json', 'send', '--conversation', 'group:chat', '--text', 'A new message']),
    ('alice-whatsapp', 'act', False,
     ['wacli', '--json', 'send', 'text', '--to', PHONE, '--message', 'A new message']),
    ('alice-whatsapp', 'draft', False,
     ['wacli', '--json', 'send', 'text', '--to', OWNER, '--message', 'Draft for WhatsApp: Alice:\n\nA new message']),
    ('group', 'act', False,
     ['wacli', '--json', 'send', 'text', '--to', OWNER, '--message', 'Draft for group:chat:\n\nA new message']),
    ('group', 'draft', True,
     ['wacli', '--json', 'send', 'text', '--to', OWNER, '--message', 'Draft for group:chat:\n\nA new message']),
])
def test_send_routes_without_a_request_and_is_visible_in_log_and_status(world, send_cli, capsys, route, mode, bot, expected):
    world.config_path.write_text(world.config_text.replace('reply_via_whatsapp = true\n', '').replace('mode = "act"', f'mode = "{mode}"')
                                 .replace('\nsource = "teams"\n', f'\nsource = "teams"\npost_as_bot = {str(bot).lower()}\n'))
    chat = ['--chat', PHONE] if route == 'alice-whatsapp' else []
    assert send_cli(['send', '--route', route, '--thread', 'project-a', '--text', 'A new message', '--json', *chat]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['state'] == 'sent' and result['external_id'] == 'sent-id'
    assert world.sends == [expected]
    out = world.app.rows('SELECT * FROM outbox')[0]
    assert out['id'] == result['outbox'] and out['request_id'] is None
    recipient = 'group:chat' if expected[0] == 'toucan-bot' else expected[5]
    assert out['state'] == 'sent' and out['to_chat'] == recipient
    assert out['source'] == ('teams-bot' if expected[0] == 'toucan-bot' else 'whatsapp')
    assert out['kind'] == ('draft' if recipient == OWNER else 'send')
    assert world.app.rows('SELECT * FROM requests') == []
    assert send_cli(['log', '--thread', 'project-a', '--json']) == 0
    events = json.loads(capsys.readouterr().out)
    assert [e['kind'] for e in events] == ['outbox_pending', 'delivered']
    detail = json.loads(events[0]['detail_json'])
    assert detail['route'] == route and detail['chat'] == (PHONE if chat else 'group:chat')
    assert send_cli(['status', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['outbox'] == [out]


@pytest.mark.parametrize('args,code,error', [
    (['--route', 'group', '--thread', 'wrong'], 3, 'owner thread'),
    (['--route', 'alice-whatsapp', '--thread', 'project-a'], 2, '--chat'),
    (['--route', 'group', '--thread', 'project-a', '--chat', 'another:chat'], 2, 'chat'),
    (['--route', 'missing', '--thread', 'project-a'], 2, 'unknown route'),
    (['--route', 'group', '--thread', 'project-a', '--caption', 'caption'], 2, '--caption requires --file'),
])
def test_send_refuses_wrong_owner_and_invalid_destination_before_delivery(world, send_cli, capsys, args, code, error):
    assert send_cli(['send', *args, '--text', 'A new message']) == code
    assert error in capsys.readouterr().err
    assert world.sends == [] and world.app.rows('SELECT * FROM outbox') == []


def test_send_requires_thread_and_nonempty_text(world, send_cli, capsys):
    with pytest.raises(SystemExit) as error:
        send_cli(['send', '--route', 'group', '--text', 'A new message'])
    assert error.value.code == 2 and '--thread' in capsys.readouterr().err
    assert send_cli(['send', '--route', 'group', '--thread', 'project-a', '--text', '  ']) == 2
    assert 'empty' in capsys.readouterr().err
    assert world.sends == [] and world.app.rows('SELECT * FROM outbox') == []


@pytest.mark.parametrize('bot', [False, True])
def test_send_files_follow_transport_capabilities(world, send_cli, capsys, bot):
    world.config_path.write_text(world.config_text.replace('\nsource = "teams"\n', f'\nsource = "teams"\npost_as_bot = {str(bot).lower()}\n'))
    file = world.root / 'proof.txt'
    file.write_text('Evidence')
    args = ['send', '--route', 'group' if bot else 'alice-whatsapp', '--thread', 'project-a',
            '--file', str(file), '--caption', 'Evidence']
    if bot:
        assert send_cli(args) == 2
        assert 'text only' in capsys.readouterr().err
        assert world.sends == [] and world.app.rows('SELECT * FROM outbox') == []
    else:
        assert send_cli([*args, '--chat', PHONE]) == 0
        assert world.sends == [['wacli', '--json', 'send', 'file', '--to', PHONE,
                                '--file', str(file), '--caption', 'Evidence']]


@pytest.mark.parametrize('interrupted', [False, True])
def test_send_failure_is_recorded_and_returns_operational_exit(world, send_cli, capsys, interrupted):
    world.send_result = subprocess.CompletedProcess([], 1, '', 'disconnected')
    if interrupted:
        world.send_error = subprocess.TimeoutExpired(['wacli'], 120)
    assert send_cli(['send', '--route', 'alice-whatsapp', '--chat', PHONE, '--thread', 'project-a', '--text', 'Hello']) == 1
    assert ('uncertain' if interrupted else 'wacli failed') in capsys.readouterr().err
    assert send_cli(['status', '--json']) == 0
    out = json.loads(capsys.readouterr().out)['outbox'][0]
    assert out['state'] == ('uncertain' if interrupted else 'failed')
    assert out['error'] == ('sender interrupted' if interrupted else 'disconnected')
    assert out['request_id'] is None and len(world.sends) == 1


@pytest.mark.parametrize('old,new,match', [
    ('requesters = ["alice"]', 'requesters = ["missing"]', 'unknown person'),
    ('thread = "project-a"', 'thread = "missing"', 'unknown thread'),
    ('mode = "act"', 'mode = "auto"', 'source and mode'),
    ('mode = "act"', '', 'source and mode'),
    ('source = "whatsapp"', 'source = "teams-bot"', 'source and mode'),
    ('source = "whatsapp"', '', 'source and mode'),
    ('2026-10-17T00:00:00+00:00', 'yesterday', 'invalid timestamp'),
    ('2026-10-17T00:00:00+00:00', '2026-10-17T00:00:00', 'timezone'),
    ('rate_per_hour = 20', 'rate_per_hour = 0', 'positive'),
    ('max_wakes = 3', 'max_wakes = 1.5', 'integer'),
    ('source = "whatsapp"', 'source = "whatsapp"\npost_as_bot = true', 'only on Teams routes'),
    ('source = "teams"', 'source = "teams"\npost_as_bot = "yes"', 'post_as_bot'),
    ('source = "whatsapp"', 'source = "whatsapp"\nowner_reply_closes = true', 'owner_reply_closes'),
    ('[threads.project-a]\n', '[threads.project-a]\nfallback = "project-a"\n', 'fallback must be another'),
    ('source = "whatsapp"', 'source = "whatsapp"\nreply_to = "missing"', 'reply_to'),
    ('chats = ["group:chat"]', 'chats = ["group:chat"]\nreply_to = "other"', 'only on WhatsApp act routes'),
    ('source = "whatsapp"', 'source = "whatsapp"\nreply_prefix = 1', 'reply_prefix'),
])
def test_invalid_config_fails_clearly(world, old, new, match):
    world.config_path.write_text(world.config_text.replace(old, new, 1))
    with pytest.raises(ValidationError, match=match):
        load(world.config_path)


def test_duplicate_route_chat_is_refused(world):
    world.config_path.write_text(world.config_text + '''
[[routes]]
name = "duplicate"
source = "whatsapp"
chats = ["15550000001@s.whatsapp.net"]
thread = "project-a"
mode = "draft"
requesters = ["alice"]
active_from = "2026-10-17T00:00:00+00:00"
''')
    with pytest.raises(ValidationError, match='two routes'):
        load(world.config_path)


def test_check_config_warns_without_opening_state_or_exposing_hook(world, monkeypatch, capsys):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    monkeypatch.setenv('TOUCAN_DB', str(world.root / 'must-not-exist' / 'state.db'))
    world.hook.chmod(0o644)
    assert main(['check-config', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert 'mode 600' in result['warnings'][0]
    assert result['people'] == ['alice', 'other']
    assert 'SECRET-TOKEN' not in json.dumps(result)
    assert not (world.root / 'must-not-exist').exists()
    world.hook.unlink()
    assert main(['check-config']) == 0
    assert 'must exist' in capsys.readouterr().out


def test_cli_lifecycle_json_and_exit_codes(world, monkeypatch, capsys):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    monkeypatch.setenv('TOUCAN_DB', str(world.db_path))
    world.seed()
    req = world.request()
    invoke = lambda argv: main(argv, effects=world.fx, paths=world.paths)
    assert invoke(['claim', str(req), '--thread', 'wrong']) == 3
    assert 'belong' in capsys.readouterr().err
    assert invoke(['inbox', '--thread', 'project-a']) == 0
    content = capsys.readouterr().out
    assert 'toucan claim' in content
    assert '----- UNTRUSTED CONTENT' in content
    assert '| Please investigate PRIVATE MESSAGE' in content
    assert '----- END -----' in content
    assert invoke(['claim', str(req), '--thread', 'project-a', '--json']) == 0
    claim = json.loads(capsys.readouterr().out)['claim_id']
    assert invoke(['reply', str(req), '--claim', 'wrong', '--text', 'Answer', '--final']) == 3
    assert not world.sends
    capsys.readouterr()
    # A reply must say whether it finishes the request; an acknowledgement used to close it.
    with pytest.raises(SystemExit):
        invoke(['reply', str(req), '--claim', claim, '--text', 'Will do'])
    assert invoke(['reply', str(req), '--claim', claim, '--text', 'Will do', '--promise', 'send results']) == 2
    assert not world.sends
    capsys.readouterr()
    assert invoke(['reply', str(req), '--claim', claim, '--text', 'Will do', '--promise', 'send results', '--due', '2h']) == 0
    capsys.readouterr()
    assert invoke(['status', '--json']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['requests'] == [{'thread': 'project-a', 'state': 'claimed', 'count': 1}]
    assert [(p['promise'], p['promise_due']) for p in status['promises']] == [('send results', world.now + 7200)]
    assert invoke(['inbox', '--thread', 'project-a']) == 0
    assert 'promised "send results"' in capsys.readouterr().out
    assert invoke(['reply', str(req), '--claim', claim, '--text', 'Answer', '--final', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'sent'
    assert invoke(['status', '--json']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['requests'] == [] and status['promises'] == []
    assert invoke(['log', '--thread', 'project-a', '--request', str(req), '--since', '1d', '--json']) == 0
    kinds = [e['kind'] for e in json.loads(capsys.readouterr().out)]
    assert 'claimed' in kinds and 'delivered' in kinds
    assert invoke(['log', '--since', 'bad']) == 2


def test_check_config_names_unknown_keys(world, monkeypatch, capsys):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    monkeypatch.setenv('TOUCAN_DB', str(world.root / 'must-not-exist' / 'state.db'))
    assert main(['check-config', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['warnings'] == []
    world.config_path.write_text(world.config_text.replace('mode = "act"', 'mode = "act"\nowner_reply_close = true', 1))
    assert main(['check-config', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['warnings'] == ['unknown key routes.alice-whatsapp.owner_reply_close; check the spelling']


def test_invalid_config_cli_returns_validation_exit(world, monkeypatch, capsys):
    world.config_path.write_text('broken[')
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    assert main(['check-config']) == 2
    assert 'TOML' in capsys.readouterr().err


def test_wait_returns_when_a_request_arrives_mid_wait(world, monkeypatch, capsys):
    # T3 queues webhook runs behind a busy turn; a background `toucan wait` is what reaches it.
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    monkeypatch.setenv('TOUCAN_DB', str(world.db_path))
    world.seed()
    invoke = lambda argv: main(argv, effects=world.fx, paths=world.paths)
    assert invoke(['inbox', '--thread', 'project-a']) == 0
    assert 'No `toucan wait --thread project-a` is running' in capsys.readouterr().out
    seen = []

    def sleep(seconds):
        seen.append(world.app.waiter_running('project-a'))
        world.advance(seconds)
        if len(seen) == 3:
            world.wa('mid-wait')
            world.app.sync()

    world.fx.sleep = sleep
    assert invoke(['wait', '--thread', 'project-a', '--json']) == 0
    found = json.loads(capsys.readouterr().out)
    assert found['fired'] and len(found['requests']) == 1
    assert seen == [True, True, True]
    assert not world.app.waiter_running('project-a')
    assert invoke(['wait', '--thread', 'project-a', '--timeout', '1']) == 0
    assert 'requests' in capsys.readouterr().out  # still unclaimed, so it fires again at once
    world.app.claim(found['requests'], 'project-a')
    assert invoke(['wait', '--thread', 'project-a', '--timeout', '12', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['fired'] is False
    world.wa('mid-wait', text='edited', edited=1)
    world.app.sync()
    assert invoke(['wait', '--thread', 'project-a', '--timeout', '1', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['requests'] == found['requests']  # an edit on claimed work
    assert invoke(['inbox', '--thread', 'project-a']) == 0
    capsys.readouterr()
    assert invoke(['wait', '--thread', 'project-a', '--timeout', '1', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['fired'] is False  # seen in the inbox: no repeat
