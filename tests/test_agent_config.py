import io
import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from toucan import tomlw
from toucan.cli import main
from toucan.config import load
from toucan.effects import Effects


@pytest.fixture
def agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('TOUCAN_CONFIG', str(tmp_path / 'config/toucan/toucan.toml'))
    monkeypatch.setenv('TOUCAN_DB', str(tmp_path / 'state/toucan.db'))
    path = tmp_path / 'config/toucan/toucan.toml'
    fx = Effects(now=lambda: 1792213200)

    def invoke(*args, code=0):
        assert main([*args, '--json'], effects=fx) == code
        result = capsys.readouterr()
        return json.loads(result.out) if result.out else result.err

    assert invoke('init', '--owner-name', 'Alice', '--timezone', 'Asia/Tokyo')['status'] == 'created'
    return path, invoke


def test_toml_roundtrip_example_and_supported_values(tmp_path):
    original = Path('examples/toucan.example.toml')
    path = tmp_path / 'config.toml'
    data = tomllib.loads(original.read_text())
    path.write_text(tomlw.dumps(data))
    assert load(path) == load(original)
    data = dict(settings={'quoted.key': '"\\\n\t\r\b\f\x00 café', 'count': 3, 'delay': 1.5, 'enabled': True, 'ids': ['a', 'b']})
    assert tomllib.loads(tomlw.dumps(data)) == data


def test_minimal_init_idempotency_and_private_backup(agent):
    path, invoke = agent
    raw = tomllib.loads(path.read_text())
    assert raw == dict(settings=dict(owner_name='Alice', timezone='Asia/Tokyo'), sources={}, t3={}, people={}, threads={}, routes=[])
    before = path.read_bytes()
    assert invoke('init', '--owner-name', 'Alice', '--timezone', 'Asia/Tokyo')['status'] == 'unchanged'
    assert not path.with_suffix('.toml.bak').exists()
    assert invoke('init', '--owner-name', 'Bob', '--timezone', 'UTC', code=2)
    assert path.read_bytes() == before
    assert invoke('init', '--owner-name', 'Bob', '--timezone', 'UTC', '--force')['status'] == 'updated'
    backup = path.with_suffix('.toml.bak')
    assert backup.read_bytes() == before
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (path, backup))
    assert stat.S_IMODE((path.parent / 'hooks').stat().st_mode) == 0o700
    assert stat.S_IMODE((path.parents[2] / 'state').stat().st_mode) == 0o700


@pytest.mark.parametrize('args', [[], ['--owner-name', 'Alice'], ['--owner-name', 'Alice', '--timezone', 'Mars/Bad']])
def test_invalid_init_leaves_no_files(tmp_path, monkeypatch, args, capsys):
    path = tmp_path / 'missing/toucan.toml'
    monkeypatch.setenv('TOUCAN_DB', str(tmp_path / 'state/db'))
    assert main(['init', '--config', str(path), *args]) == 2
    assert not path.parent.exists() and not (tmp_path / 'state').exists()
    assert capsys.readouterr().err


def test_add_replace_and_remove_config_entities(agent):
    path, invoke = agent
    person = ('add', 'person', 'alice', '--name', 'Alice', '--role', 'pm', '--whatsapp', '15550000001@s.whatsapp.net', '--email', 'alice@example.com', '--teams-id', '00000000-0000-0000-0000-000000000001')
    assert invoke(*person)['status'] == 'updated'
    assert invoke(*person)['status'] == 'unchanged'
    assert invoke('add', 'person', 'alice', '--name', 'Bob', code=3)
    assert invoke('add', 'person', 'alice', '--name', 'Bob', '--replace')['status'] == 'updated'
    assert invoke(*person, '--replace')['status'] == 'updated'
    assert invoke('add', 'thread', 'project-b')['status'] == 'updated'
    args = ('add', 'thread', 'project-a', '--project', 'project-a', '--fallback', 'project-b', '--t3-thread', '00000000-0000-0000-0000-000000000000', '--rotate-tokens', '1000')
    assert invoke(*args)['status'] == 'updated'
    assert invoke(*args)['status'] == 'unchanged'
    route = ('add', 'route', 'inbox', '--source', 'whatsapp', '--chat', '15550000001@s.whatsapp.net', '--thread', 'project-a', '--requester', 'alice', '--mode', 'draft', '--label', 'Inbox', '--reply-prefix', 'Reply')
    assert invoke(*route)['status'] == 'updated'
    raw = tomllib.loads(path.read_text())
    assert raw['routes'][0]['active_from'].endswith('+09:00')
    assert invoke(*route)['status'] == 'unchanged'
    assert tomllib.loads(path.read_text()) == raw
    for kind, key, ref in [('person', 'alice', 'routes.inbox'), ('thread', 'project-a', 'routes.inbox'), ('thread', 'project-b', 'threads.project-a.fallback')]:
        assert ref in invoke('remove', kind, key, code=3)
    assert invoke('remove', 'route', 'inbox')['status'] == 'updated'
    assert invoke('remove', 'route', 'inbox')['status'] == 'unchanged'
    assert invoke('remove', 'person', 'alice')['status'] == 'updated'
    assert invoke('remove', 'thread', 'project-a')['status'] == 'updated'
    assert invoke('remove', 'thread', 'project-b')['status'] == 'updated'
    assert load(path)['routes'] == []


@pytest.mark.parametrize('source,flags', [
    ('teams', ['--post-as-bot', '--owner-reply-closes', '--reply-via-whatsapp']),
    ('whatsapp', ['--reply-to', 'alice']),
    ('email', []),
])
def test_add_route_source_flags(agent, source, flags):
    path, invoke = agent
    invoke('add', 'person', 'alice', '--name', 'Alice', '--whatsapp', '15550000001@s.whatsapp.net')
    invoke('add', 'thread', 'project-a')
    chat = dict(teams='chat-a', whatsapp='15550000001@s.whatsapp.net', email='support@example.com')[source]
    result = invoke('add', 'route', 'inbox', '--source', source, '--chat', chat, '--requester', 'alice', '--thread', 'project-a', '--mode', 'draft' if source == 'email' else 'act', '--active-from', '2026-10-17T00:00:00Z', *flags)
    assert result['status'] == 'updated'
    cfg = load(path)
    assert cfg['routes'][0]['chats'] == [chat]
    if source == 'whatsapp':
        assert 'routes.inbox' in invoke('remove', 'person', 'alice', code=3)


@pytest.mark.parametrize('key,value', [
    ('settings.timezone', 'Mars/Bad'), ('settings.max_wakes', '0'), ('settings.typo', 'true'),
    ('routes', '["broken"]'), ('people', '1'), ('settings', 'false'),
    ('settings.max_wakes', '[1,2]'), ('settings.owner_name', '2026-10-17'),
    ('routes.inbox.active_ts', '1'),
])
def test_bad_edit_never_changes_config_or_backup(agent, key, value):
    path, invoke = agent
    invoke('config', 'set', 'settings.owner_name', 'Bob')
    before, backup = path.read_bytes(), path.with_suffix('.toml.bak').read_bytes()
    assert invoke('config', 'set', key, value, code=2)
    assert path.read_bytes() == before and path.with_suffix('.toml.bak').read_bytes() == backup
    assert not list(path.parent.glob('.toucan-*'))


def test_config_literals_lists_dotted_routes_get_and_unset(agent, capsys):
    path, invoke = agent
    invoke('add', 'person', 'alice', '--name', 'Alice')
    invoke('add', 'thread', 'project-a')
    invoke('add', 'route', 'inbox', '--source', 'teams', '--chat', 'conversation-a', '--requester', 'alice', '--thread', 'project-a', '--mode', 'draft')
    assert invoke('config', 'set', 'people.alice.email', '--value', 'alice@example.com', '--value', 'bob@example.com')['status'] == 'updated'
    assert invoke('config', 'set', 'people.alice.whatsapp', '["15550000001@s.whatsapp.net"]')['status'] == 'updated'
    assert invoke('config', 'set', 'people.alice.whatsapp', '--value', '15550000001@s.whatsapp.net')['status'] == 'unchanged'
    invoke('config', 'set', 'routes.inbox.mode', 'act')
    assert invoke('config', 'get', 'routes.inbox.mode') == dict(key='routes.inbox.mode', value='act')
    assert invoke('config', 'get')['value']['people']['alice']['email'] == ['alice@example.com', 'bob@example.com']
    assert invoke('config', 'unset', 'people.alice.email')['status'] == 'updated'
    assert invoke('config', 'unset', 'people.alice.email')['status'] == 'unchanged'
    assert invoke('config', 'get', 'people.alice.email', code=2)
    assert main(['config', 'get', 'people.alice.whatsapp']) == 0
    output = capsys.readouterr().out
    assert '15550000001' not in output and '…' in output
    assert main(['config', 'get', 'people.alice.whatsapp', '--show-ids']) == 0
    assert '15550000001' in capsys.readouterr().out
    invoke('config', 'set', 'settings.alert_url', 'https://alerts.example.com/SECRET')
    assert 'SECRET' not in json.dumps(invoke('config', 'get'))
    assert invoke('config', 'get', 'settings.alert_url')['value'] == '[redacted]'


def test_hook_stdin_validation_idempotency_and_check(world, monkeypatch, capsys):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    world.t3_owner(thread='00000000-0000-0000-0000-000000000001')
    url = world.hook.read_text()
    world.hook.chmod(0o644)
    monkeypatch.setattr('sys.stdin', io.StringIO(url))
    assert main(['hook', 'set', 'project-a', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'updated'
    assert stat.S_IMODE(world.hook.stat().st_mode) == 0o600
    monkeypatch.setattr('sys.stdin', io.StringIO(url))
    assert main(['hook', 'set', 'project-a', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'unchanged'
    assert main(['hook', 'check', 'project-a', '--json'], paths=world.paths) == 0
    output = capsys.readouterr().out
    assert json.loads(output)['resolved_thread'] == '…0001'
    assert 'SECRET' not in output and 'https://' not in output
    monkeypatch.setattr('sys.stdin', io.StringIO('https://example.com/wrong/SECRET'))
    assert main(['hook', 'set', 'project-a', '--json']) == 2
    assert 'SECRET' not in capsys.readouterr().err and world.hook.read_text().strip() == url
    world.hook.chmod(0o644)
    assert main(['hook', 'check', 'project-a', '--json'], paths=world.paths) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'invalid'


def test_setup_prompt_and_standby_use_owner_template(agent):
    path, invoke = agent
    invoke('add', 'thread', 'project-b')
    result = invoke('setup-prompt', 'project-b')
    assert result['title'] == 'Toucan requests (project-b)'
    assert result['schedule'] == {'type': 'webhook'} and result['bindToCurrentThread'] is True
    assert result['hook_command'] == 'toucan hook set project-b'
    assert '{{body.summary}}' in result['prompt'] and 'If Alice answers' in result['prompt']
    assert 'project-a' not in result['prompt']
    standby = invoke('setup-prompt', 'project-b', '--standby')
    assert 'toucan log --request REQ' in standby['prompt'] and 'handed_over' in standby['prompt']
    assert invoke('setup-prompt', 'missing', code=2)


def test_guide_checkout_topics_and_unknown(agent):
    _, invoke = agent
    topics = invoke('guide', '--list')
    assert 'owner-thread' in topics and 'configuration' in topics
    assert invoke('guide') == Path('AGENTS.md').read_text()
    for topic in topics:
        expected = Path('skills/toucan-owner-thread/SKILL.md' if topic == 'owner-thread' else f'docs/{topic}.md').read_text()
        assert invoke('guide', topic) == expected
    teams = ' '.join(invoke('guide', 'setup-teams').split())
    assert 'messages modified in the last seven days' in teams
    assert 'older messages edited recently' in teams
    assert invoke('guide', 'missing', code=2)
