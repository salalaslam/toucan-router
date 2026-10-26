import json
import io
import stat
import subprocess
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

from toucan import configedit, services
from toucan.cli import main
from toucan.config import ValidationError, load
from toucan.effects import Effects


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / 'agent config'
    config = root / 'toucan.toml'
    config.parent.mkdir()
    configedit.save(config, dict(settings=dict(owner_name='Alice', timezone='UTC'), people={}, threads={}, routes=[]))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(root))
    monkeypatch.setenv('TOUCAN_CONFIG', str(config))
    monkeypatch.setattr(services.shutil, 'which', lambda name: '/opt/agent bin/' + name)
    monkeypatch.setattr(services.setup, 'doctor', lambda *args: [dict(id='config', status='ok', command='')])
    enabled, active, calls, extra = set(), set(), [], {}

    def run(argv, timeout=120):
        assert timeout <= 5
        calls.append(argv)
        verb = argv[2]
        if verb in ('is-enabled', 'is-active'):
            found = argv[3] in (enabled if verb == 'is-enabled' else active)
            return subprocess.CompletedProcess(argv, 0 if found else 1, ('enabled' if verb == 'is-enabled' else 'active') if found else 'inactive', '')
        if verb == 'list-units':
            return subprocess.CompletedProcess(argv, 0, '\n'.join(name + ' loaded active running Sync' for name in active), '')
        if verb == 'show':
            return subprocess.CompletedProcess(argv, 0, extra.get(argv[3], ''), '')
        if verb == 'enable':
            enabled.add(argv[-1])
            active.add(argv[-1])
        if verb in ('disable', 'stop'):
            enabled.discard(argv[-1])
            active.discard(argv[-1])
        return subprocess.CompletedProcess(argv, 0, '', '')

    return dict(config=config, directory=root / 'systemd/user', enabled=enabled, active=active, calls=calls,
                extra=extra, fx=Effects(run=run))


def optional_sources(env):
    raw = configedit.read(env['config'])
    raw['settings']['owner_whatsapp'] = '15550000000@s.whatsapp.net'
    raw['sources'] = dict(whatsapp=dict(store=str(env['config'].parent / 'WhatsApp 100% $store')))
    raw['people'] = dict(alice=dict(name='Alice', teams_id='00000000-0000-0000-0000-000000000001'))
    raw['threads'] = {'project-a': dict(hook_file='~/.config/toucan/hooks/project-a.hook')}
    raw['routes'] = [dict(name='team', source='teams', chats=['team-id'], thread='project-a', requesters=['alice'],
                          mode='draft', active_from='2026-10-17T00:00:00Z')]
    configedit.save(env['config'], raw)


def invoke(env, command, *args, code=0):
    output, error = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(error):
        actual = main([command, '--config', str(env['config']), '--json', *args], effects=env['fx'])
    assert actual == code, error.getvalue()
    return json.loads(output.getvalue()) if output.getvalue() else error.getvalue()


def test_enable_dry_run_plans_units_without_mutation(installation):
    env = installation
    optional_sources(env)
    before = env['config'].read_bytes()
    result = invoke(env, 'enable', '--dry-run')
    assert result['status'] == 'dry-run' and result['doctor'] == []
    assert len(result['units']) == 4
    contents = {u['name']: u['content'] for u in result['units']}
    assert 'ExecStart="/opt/agent bin/toucan" sync' in contents['toucan-sync.service']
    assert 'ExecStart="/opt/agent bin/wacli" --store "' + str(env['config'].parent / 'WhatsApp 100%% $$store') + '"' in contents['wacli-sync.service']
    assert ' sync --follow' in contents['wacli-sync.service']
    assert 'ExecStart="/opt/agent bin/toucan-bot" serve' in contents['toucan-bot.service']
    assert 'Environment="TOUCAN_CONFIG=' + str(env['config']) + '"' in contents['toucan-sync.service']
    assert ['systemctl', '--user', 'enable', '--now', 'toucan-sync.timer'] in result['actions']
    assert not env['directory'].exists() and env['config'].read_bytes() == before
    assert all(call[2] in ('is-enabled', 'is-active', 'list-units', 'show') for call in env['calls'])


def test_enable_installs_private_units_records_services_and_repeats_unchanged(installation):
    env = installation
    optional_sources(env)
    result = invoke(env, 'enable')
    assert result['status'] == 'updated' and result['doctor'] == [dict(id='config', status='ok', command='')]
    assert len(result['units']) == 4 and all(u['status'] == 'installed' for u in result['units'])
    cfg = load(env['config'])
    assert cfg['sources']['whatsapp']['service_unit'] == 'wacli-sync.service'
    assert cfg['sources']['teams']['service_unit'] == 'toucan-bot.service'
    for file in env['directory'].iterdir():
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    before = env['config'].read_bytes(), env['config'].with_suffix('.toml.bak').read_bytes()
    env['calls'].clear()
    again = invoke(env, 'enable')
    assert again['status'] == 'unchanged' and again['actions'] == []
    assert before == (env['config'].read_bytes(), env['config'].with_suffix('.toml.bak').read_bytes())
    assert not any(call[2] in ('enable', 'daemon-reload') for call in env['calls'])


def test_enable_refuses_existing_units_and_their_dependent_timer(installation):
    env = installation
    env['directory'].mkdir(parents=True)
    foreign = env['directory'] / 'toucan-sync.service'
    foreign.write_text('[Service]\nExecStart=/bin/true\n')
    result = invoke(env, 'enable', code=3)
    assert result['status'] == 'refused' and result['actions'] == []
    assert all(u['status'] == 'refused' for u in result['units'])
    assert foreign.read_text() == '[Service]\nExecStart=/bin/true\n'
    assert not (env['directory'] / 'toucan-sync.timer').exists()


@pytest.mark.parametrize('name,execstart', [('wacli-sync.service', ''), ('other-sync.service', '{ path=/opt/wacli ; argv[]=/opt/wacli --store /tmp/example sync --follow ; }')])
def test_enable_preserves_an_existing_whatsapp_sync(installation, name, execstart):
    env = installation
    optional_sources(env)
    env['active'].add(name)
    env['extra'][name] = execstart
    result = services.enable(effects=env['fx'])
    wa = next(u for u in result['units'] if u['name'] == 'wacli-sync.service')
    assert wa['status'] == 'skipped' and name in wa['reason']
    assert not (env['directory'] / 'wacli-sync.service').exists()
    assert load(env['config'])['sources']['whatsapp']['service_unit'] == ''
    assert not any(call[2] == 'enable' and call[-1] == 'wacli-sync.service' for call in env['calls'])


def test_enable_skips_unavailable_optional_commands(installation, monkeypatch):
    env = installation
    optional_sources(env)
    monkeypatch.setattr(services.shutil, 'which', lambda name: '/opt/bin/' + name if name in ('toucan', 'systemctl') else None)
    result = services.enable(effects=env['fx'])
    assert {u['name'] for u in result['units'] if u['status'] == 'installed'} == {'toucan-sync.service', 'toucan-sync.timer'}
    assert all(u['status'] == 'skipped' for u in result['units'] if u['name'] in ('wacli-sync.service', 'toucan-bot.service'))


def test_disable_stops_only_owned_units_and_is_idempotent(installation):
    env = installation
    optional_sources(env)
    invoke(env, 'enable')
    env['calls'].clear()
    result = invoke(env, 'disable')
    assert result['status'] == 'updated'
    assert {call[-1] for call in env['calls'] if call[2] == 'disable'} == {'toucan-sync.timer', 'wacli-sync.service', 'toucan-bot.service'}
    assert len(list(env['directory'].iterdir())) == 4
    assert invoke(env, 'disable')['status'] == 'unchanged'
    foreign = env['directory'] / 'toucan-bot.service'
    foreign.write_text('[Service]\nExecStart=/bin/true\n')
    env['active'].add('toucan-bot.service')
    assert invoke(env, 'disable', code=3)['status'] == 'refused'
    assert 'toucan-bot.service' in env['active']


@pytest.mark.parametrize('action', [services.enable, services.disable])
def test_service_commands_validate_before_effects(installation, action):
    env = installation
    env['config'].write_text('[settings]\ntimezone="invalid/zone"\n')
    with pytest.raises(ValidationError):
        action(effects=env['fx'])
    assert env['calls'] == [] and not env['directory'].exists()


def test_enable_reports_failure_without_provider_output(installation):
    env = installation
    run = env['fx'].run

    def failing(argv, timeout=5):
        if argv[2] == 'daemon-reload':
            return subprocess.CompletedProcess(argv, 1, '', 'SECRET https://example.com/private')
        return run(argv, timeout=timeout)

    env['fx'].run = failing
    with pytest.raises(RuntimeError) as error:
        services.enable(effects=env['fx'])
    assert 'SECRET' not in str(error.value) and 'example.com' not in str(error.value)
