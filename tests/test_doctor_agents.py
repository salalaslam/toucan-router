import json
import subprocess

import pytest

from toucan.cli import main
from toucan.effects import Effects
from toucan.setup import doctor


@pytest.mark.parametrize('providers,claude,codex,expected', [
    ((), 0, 0, {'host.claude': 'todo', 'host.codex': 'todo', 'host.provider': 'todo', 'host.fallback': 'skip'}),
    (('claude',), 0, 0, {'host.claude': 'ok', 'host.codex': 'skip', 'host.provider': 'ok', 'host.fallback': 'warn'}),
    (('codex',), 0, 1, {'host.claude': 'skip', 'host.codex': 'todo', 'host.provider': 'ok', 'host.fallback': 'warn'}),
    (('claude', 'codex'), 1, 0, {'host.claude': 'todo', 'host.codex': 'ok', 'host.provider': 'ok', 'host.fallback': 'ok'}),
])
def test_doctor_reports_provider_install_auth_and_fallback_commands(world, monkeypatch, capsys, providers, claude, codex, expected):
    world.config_path.write_text(world.config_text.replace('[threads.project-a]', '[threads.project-a]\nfallback = "project-b"') +
                                 '\n[threads.project-b]\nhook_file = "missing.hook"\n')
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: '/example/bin/' + name if name in providers else None)
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    calls = []

    def run(argv, timeout):
        calls.append((argv, timeout))
        code = claude if argv[0] == 'claude' else codex if argv[0] == 'codex' else 0
        return subprocess.CompletedProcess(argv, code, '', 'SECRET-PROVIDER-OUTPUT')

    assert main(['doctor', '--json'], effects=Effects(now=world.fx.now, run=run), paths=world.paths) == 1
    output = capsys.readouterr().out
    items = {item['id']: item for item in json.loads(output)}
    assert {key: items[key]['status'] for key in expected} == expected
    assert 'SECRET-PROVIDER-OUTPUT' not in output and 'SECRET-TOKEN' not in output
    assert all(item['command'] if item['status'] in ('todo', 'warn') else item['command'] == '' for item in items.values())
    assert items['thread:project-b:hook']['command'] == 'toucan hook set project-b  # pipe the T3 webhook URL on stdin'
    if codex and 'codex' in providers:
        assert items['host.codex']['command'] == 'codex login'
    assert calls and all(timeout == 5 for _, timeout in calls)


@pytest.mark.parametrize('kind,status', [('unsupported', 'ok'), ('signed-out', 'todo'), ('timeout', 'warn')])
def test_doctor_claude_status_is_optional_on_old_versions_and_bounded(world, monkeypatch, kind, status):
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: '/example/bin/claude' if name == 'claude' else None)

    def run(argv, timeout):
        if argv[0] == 'claude':
            assert timeout == 5
            if kind == 'timeout':
                raise subprocess.TimeoutExpired(argv, timeout)
            return subprocess.CompletedProcess(argv, 1, '', "error: unknown command 'auth'" if kind == 'unsupported' else 'not authenticated')
        return subprocess.CompletedProcess(argv, 0, '', '')

    items = {i['id']: i for i in doctor(world.config_path, Effects(now=world.fx.now, run=run), world.paths)}
    assert items['host.claude']['status'] == status


@pytest.mark.parametrize('command,process,expected', [
    (False, '', 'todo'), (True, '', 'ok'), (False, 'node /example/t3code/server/index.js', 'ok'),
])
def test_doctor_detects_t3_without_compatible_database(world, monkeypatch, command, process, expected):
    world.paths.t3.rename(world.root / 'old.sqlite')
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: '/example/bin/t3code' if command and name == 't3code' else None)

    def run(argv, timeout):
        return subprocess.CompletedProcess(argv, 0, process if argv[0] == 'ps' else '', '')

    items = {i['id']: i for i in doctor(world.config_path, Effects(now=world.fx.now, run=run), world.paths)}
    assert items['host.t3']['status'] == expected
    assert items['t3.schema']['status'] == 'warn'
    assert not world.paths.t3.exists()
    if expected == 'todo':
        assert 'see https://github.com/pingdotgg/t3code' in items['host.t3']['message']


def test_doctor_email_probe_uses_configured_notmuch_via_effects(world, monkeypatch):
    world.config_path.write_text(world.config_text + '''
[[routes]]
name = "email"
source = "email"
chats = ["support@example.com"]
thread = "project-a"
mode = "draft"
requesters = ["alice"]
active_from = "2026-10-17T00:00:00Z"
''')
    world.paths.notmuch_config = str(world.root / 'notmuch-config')
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: '/example/bin/notmuch' if name == 'notmuch' else None)
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps([{'timestamp': world.now}]) if argv[0] == 'notmuch' else '', '')

    items = {i['id']: i for i in doctor(world.config_path, Effects(now=world.fx.now, run=run), world.paths)}
    assert items['email.fresh']['status'] == 'ok'
    assert next(argv for argv in calls if argv[0] == 'notmuch')[1] == '--config=' + world.paths.notmuch_config
