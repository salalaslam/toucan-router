import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from toucan.cli import main
from toucan.config import ValidationError, load, warnings
from toucan.core import App
from toucan.effects import Effects
from toucan.sources import Paths
from toucan.storage import connect
from conftest import OWNER, PHONE


def test_init_private_permissions_refusal_and_doctor_todos(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('TOUCAN_DB', str(tmp_path / 'state/toucan.db'))
    path = tmp_path / '.config/toucan/toucan.toml'
    previous = os.umask(0)
    try:
        assert main(['init', '--example']) == 0
    finally:
        os.umask(previous)
    assert 'run toucan doctor' in capsys.readouterr().out.lower()
    for directory in (path.parent, path.parent / 'hooks', tmp_path / 'state'):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_text()
    assert main(['init', '--example']) == 0
    assert path.read_text() == before
    capsys.readouterr()
    fx = Effects(run=lambda argv: subprocess.CompletedProcess(argv, 1, '', ''))
    assert main(['doctor', '--json'], effects=fx) == 1
    items = json.loads(capsys.readouterr().out)
    assert all(set(i) == {'id', 'area', 'status', 'message', 'fix', 'command'} for i in items)
    assert all(i['status'] in {'ok', 'todo', 'warn', 'skip'} for i in items)
    assert next(i for i in items if i['id'] == 'owner_name')['status'] == 'todo'
    assert next(i for i in items if i['id'] == 'thread:project-a:hook')['status'] == 'todo'
    path.write_text('invalid')
    assert main(['init', '--example', '--config', str(path), '--force']) == 0
    assert path.read_text() == before


def test_doctor_fully_configured_commands_and_redaction(world, tmp_path, monkeypatch, capsys):
    import sqlite3
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("INSERT INTO conversations VALUES ('group:chat','groupChat',NULL,'',NULL,'now','now')")
        con.execute("INSERT INTO catchup (conversation_id,cursor,last_success,last_error,http_status) VALUES ('group:chat',NULL,'2026-10-17T07:00:00Z',NULL,NULL)")
        for name in ('heartbeat', 'maintenance'):
            con.execute('INSERT INTO meta VALUES (?,?)', ('task:' + name, json.dumps(dict(
                last_success='2026-10-17T07:00:00Z', last_error=None, last_failure=None,
                failures=0, interval_seconds=120))))
    world.t3_owner(thread='11111111-1111-1111-1111-111111111111', used=100)
    text = world.config_text.replace('\nsource = "teams"\n', '\nsource = "teams"\npost_as_bot = true\n')
    text = text.replace('# transcribe = "chatgpt-transcribe"', 'transcribe = "transcribe-test"')
    text += '''
[[routes]]
name = "email"
source = "email"
chats = ["support@example.com"]
thread = "project-a"
mode = "draft"
requesters = ["alice"]
active_from = "2026-10-17T00:00:00Z"
'''
    world.config_path.write_text(text)
    commands = tmp_path / 'bin'
    commands.mkdir()
    script = f'#!{sys.executable}\n' + f'''import json, sys
from pathlib import Path
name = Path(sys.argv[0]).name
if name == 'notmuch': print(json.dumps([{{'timestamp': {world.now}}}]))
elif name == 'toucan-bot': print(json.dumps({{'ok': True}}))
elif name == 'toucan-upload-loom': print(json.dumps({{'logged_in': True}}))
elif 'is-enabled' in sys.argv: print('enabled')
else: print('Result=success')
'''
    for name in ('notmuch', 'wacli', 'toucan-bot', 'toucan-upload-loom', 'transcribe-test', 'systemctl', 'codex'):
        command = commands / name
        command.write_text(script)
        command.chmod(0o700)
    monkeypatch.setenv('PATH', str(commands))
    fx = Effects(now=lambda: world.now)
    argv = ['doctor', '--config', str(world.config_path), '--json']
    assert main(argv, effects=fx, paths=world.paths) == 0
    output = capsys.readouterr().out
    items = json.loads(output)
    assert all(i['status'] in ('ok', 'skip') for i in items)
    assert 'SECRET-TOKEN' not in output and 'http://127.' not in output
    assert '11111111-1111-1111-1111-111111111111' not in output
    assert main([*argv, '--show-ids'], effects=fx, paths=world.paths) == 0
    assert '11111111-1111-1111-1111-111111111111' in capsys.readouterr().out
    (commands / 'toucan-upload-loom').write_text(f'#!{sys.executable}\nimport json\nprint(json.dumps({{"logged_in": False}}))\n')
    assert main(argv, effects=fx, paths=world.paths) == 0
    assert next(i for i in json.loads(capsys.readouterr().out) if i['id'] == 'upload.login')['status'] == 'skip'


def test_check_config_hides_chat_ids_unless_requested(world, monkeypatch, capsys):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    assert main(['check-config', '--json'], paths=world.paths) == 0
    output = capsys.readouterr().out
    assert PHONE not in output and 'group:chat' not in output
    assert main(['check-config', '--json', '--show-ids'], paths=world.paths) == 0
    assert PHONE in capsys.readouterr().out


@pytest.mark.parametrize('opt_in', [False, True])
def test_teams_whatsapp_delivery_requires_explicit_opt_in(world, opt_in):
    route = next(r for r in world.cfg['routes'] if r['source'] == 'teams')
    route.pop('reply_via_whatsapp', None)
    if opt_in:
        route['reply_via_whatsapp'] = True
    world.seed()
    req = world.request(teams=True)
    claim = world.app.claim([req], 'project-a')
    world.app.deliver(req, claim, text='Ready')
    out = world.app.rows('SELECT kind,to_chat FROM outbox')[0]
    assert out == {'kind': 'send' if opt_in else 'draft', 'to_chat': PHONE if opt_in else OWNER}


def test_config_alias_secret_file_paths_and_unknown_warnings(world, tmp_path):
    secret = tmp_path / 'alert-url'
    secret.write_text('https://alerts.example.com/private-topic\n')
    secret.chmod(0o600)
    text = world.config_text.replace('alert_url = "http://127.0.0.1:8090/alerts"', 'ntfy = "https://alerts.example.com/old"\nalert_url_file = "' + str(secret) + '"')
    text = text.replace('store = "~/.local/state/wacli"', 'store = "' + str(world.paths.wa) + '"\naccount = "archive-a"\ntypo = true').replace('account = "wa"', '')
    text = text.replace('# bot_db = "~/.local/state/toucan-bot/bot.db"', 'bot_db = "' + str(world.paths.bot_db) + '"')
    text = text.replace('db = "~/.t3/userdata/statev2.sqlite"', 'db = "' + str(world.paths.t3) + '"')
    world.config_path.write_text(text)
    cfg = load(world.config_path)
    assert cfg['settings']['alert_url'] == secret.read_text().strip()
    notes = warnings(cfg, world.paths)
    assert 'settings.ntfy is renamed to alert_url' in notes
    assert any('sources.whatsapp.typo' in w for w in notes)
    paths = Paths.from_config(cfg)
    assert (paths.wa, paths.bot_db, paths.t3, paths.wa_account) == (world.paths.wa, world.paths.bot_db, world.paths.t3, 'archive-a')
    world.wa('configured-account')
    app = App(cfg, world.db, world.fx)
    app.paths = paths
    app.sync()
    assert world.app.rows('SELECT account FROM messages')[0]['account'] == 'archive-a'
    secret.chmod(0o644)
    with pytest.raises(ValidationError, match='mode-600'):
        load(world.config_path)


@pytest.mark.parametrize('change,match', [
    ('timezone = "Not/AZone"', 'IANA timezone'),
    ('timezone = 3', 'must be text'),
])
def test_invalid_timezone_is_rejected(world, change, match):
    world.config_path.write_text(world.config_text.replace('timezone = "UTC"', change))
    with pytest.raises(ValidationError, match=match):
        load(world.config_path)


def test_email_identity_uniqueness_ignores_case(world):
    world.config_path.write_text(world.config_text.replace('[people.other]', '[people.other]\nemail = ["ALICE@example.com"]'))
    with pytest.raises(ValidationError, match='identity assigned'):
        load(world.config_path)


def test_state_permissions_ignore_umask(tmp_path):
    path = tmp_path / 'state/toucan.db'
    previous = os.umask(0)
    try:
        db = connect(path)
        db.close()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((path.parent / 'media').stat().st_mode) == 0o700


def test_upload_command_contract_and_safe_failure(world, tmp_path, monkeypatch, capsys):
    file = tmp_path / 'clip.mp4'
    file.write_bytes(b'movie')
    command = tmp_path / 'custom uploader'
    args_file = tmp_path / 'argv.json'
    command.write_text(f'#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\nPath({str(args_file)!r}).write_text(json.dumps(sys.argv[1:]))\nprint(json.dumps({{"url": "https://example.com/watch/1", "id": "1"}}))\n')
    command.chmod(0o700)
    text = world.config_text.replace('upload_command = "toucan-upload-loom"', 'upload_command = "\'' + str(command) + '\'"')
    world.config_path.write_text(text)
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    assert main(['upload', str(file), '--title', 'A title', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == {'url': 'https://example.com/watch/1', 'id': '1'}
    assert json.loads(args_file.read_text()) == ['upload', str(file), '--title', 'A title']
    command.write_text(f'#!{sys.executable}\nimport sys\nprint("authorization failed SECRET https://private.example.com/token", file=sys.stderr)\nsys.exit(1)\n')
    assert main(['upload', str(file)]) == 1
    error = capsys.readouterr().err
    assert 'authentication failed' in error and 'SECRET' not in error and 'private.example.com' not in error


def test_wakes_use_configured_timezone_and_owner_name_in_handoff(world):
    world.cfg['settings']['timezone'] = 'America/New_York'
    world.cfg['settings']['owner_name'] = 'Example Owner'
    world.seed()
    req = world.request()
    assert '03:00 EDT' in world.hooks()[-1]['summary']
    claim = world.app.claim([req], 'project-a')
    world.t3_owner()
    result = world.app.rotate('project-a')
    assert "Example Owner's OK" in result['instructions']
    assert 'Example Owner corrected' in result['instructions']
    assert claim in result['instructions']


def test_init_preserves_existing_custom_parent_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('TOUCAN_DB', str(tmp_path / 'state/toucan.db'))
    custom = tmp_path / 'existing'
    custom.mkdir(mode=0o755)
    assert main(['init', '--example', '--config', str(custom / 'router.toml')]) == 0
    assert stat.S_IMODE(custom.stat().st_mode) == 0o755
    assert main(['add', 'thread', 'project-b', '--config', str(custom / 'router.toml')]) == 0
    assert load(custom / 'router.toml')['threads']['project-b']['hook_file'] == str(tmp_path / '.config/toucan/hooks/project-b.hook')
    assert stat.S_IMODE((tmp_path / '.config/toucan/hooks').stat().st_mode) == 0o700


def test_custom_whatsapp_store_is_used_for_download_and_delivery(world, monkeypatch):
    monkeypatch.setenv('TOUCAN_DB', str(world.db_path))
    world.cfg['sources']['whatsapp']['store'] = str(world.paths.wa)
    world.cfg['sources']['teams']['bot_db'] = str(world.paths.bot_db)
    world.cfg['t3']['db'] = str(world.paths.t3)
    world.cfg['settings']['transcribe'] = 'transcribe-test'
    calls, run = [], world.fx.run

    def capture(argv):
        if argv[0] == 'wacli':
            calls.append(argv)
            if '--store' in argv:
                index = argv.index('--store')
                argv = argv[:index] + argv[index + 2:]
        return run(argv)

    world.fx.run = capture
    app = App(world.cfg, world.db, world.fx)
    app.sync()
    world.advance(1)
    world.wa('voice', text='', display_text='[Audio]', media_type='audio')
    req = app.sync()['created'][0]
    app.deliver(req, app.claim([req], 'project-a'), text='Ready')
    assert len(calls) == 2
    assert all('--store' in argv and argv[argv.index('--store') + 1] == str(world.paths.wa) for argv in calls)


def test_manual_rotation_and_resume_are_disabled_on_unknown_t3_schema(world):
    import sqlite3
    from toucan.config import Refused
    world.seed()
    req = world.request()
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Need a decision', escalation=True)
    world.t3_owner()
    with sqlite3.connect(world.paths.t3) as db:
        db.execute('DROP TABLE orchestration_v2_projection_runs')
    with pytest.raises(Refused, match='schema'):
        world.app.rotate('project-a')
    assert world.app.rotation('project-a') is None
    with pytest.raises(Refused, match='history unavailable'):
        world.app.resume(req, 'project-a')
    assert world.app.rotate('project-a', reset=True)['reset'] is True


def test_account_label_mismatch_refuses_sync_and_is_a_setup_todo(world, monkeypatch, capsys):
    world.seed()
    req = world.request()
    claim = world.app.claim([req], 'project-a')
    world.app.deliver(req, claim, text='Done')
    world.db.execute("UPDATE messages SET account='archive-old' WHERE source='whatsapp'")
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    monkeypatch.setenv('TOUCAN_DB', str(world.db_path))
    with pytest.raises(ValidationError, match='sources.whatsapp.account.*archive-old'):
        world.app.sync()
    assert main(['sync', '--json'], effects=world.fx, paths=world.paths) == 2
    error = capsys.readouterr().err
    assert 'archive-old' in error and 'PRIVATE MESSAGE' not in error
    assert main(['check-config', '--json'], paths=world.paths) == 0
    result = json.loads(capsys.readouterr().out)
    assert any(i['id'] == 'whatsapp.account' and i['status'] == 'todo' for i in result['todos'])
    from toucan.setup import doctor
    items = doctor(world.config_path, world.fx, world.paths)
    assert next(i for i in items if i['id'] == 'whatsapp.account')['status'] == 'todo'
    world.paths.wa_account = 'archive-old'
    assert world.app.sync()['created'] == []
    assert world.app.get(req)['state'] == 'closed'


@pytest.mark.parametrize('target', ['state', 'database'])
def test_doctor_warns_about_existing_state_permissions_without_chmod(world, monkeypatch, target):
    from toucan.setup import doctor, private_dir
    monkeypatch.setenv('TOUCAN_DB', str(world.db_path))
    path = world.db_path.parent if target == 'state' else world.db_path
    path.chmod(0o755 if target == 'state' else 0o644)
    if target == 'state':
        private_dir(path)
        other = connect(world.db_path)
        other.close()
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
    items = doctor(world.config_path, world.fx, world.paths)
    assert next(i for i in items if i['id'] == 'state.permissions')['status'] == 'warn'
    if target == 'database':
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        other = connect(world.db_path)
        other.close()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize('source', ['teams', 'email'])
def test_doctor_checks_whatsapp_for_outgoing_only_routes(world, monkeypatch, source):
    from toucan.setup import doctor
    cfg = world.cfg
    cfg['routes'] = [r for r in cfg['routes'] if r['source'] == 'teams']
    if source == 'email':
        cfg['routes'][0].update(source='email', mode='draft')
    monkeypatch.setattr('toucan.setup.load', lambda path: cfg)
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: None)
    items = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}
    assert items['whatsapp.command']['status'] == 'todo'
    assert items['whatsapp.store']['status'] == 'ok'
    assert 'whatsapp.fresh' not in items


@pytest.mark.parametrize('content', ['', 'not a URL', 'https://example.com/wrong/SECRET-TOKEN'])
def test_doctor_rejects_invalid_hook_and_warns_about_fallback_only_resolution(world, monkeypatch, content):
    from toucan.setup import doctor
    world.t3_owner(thread=world.cfg['threads']['project-a']['t3_thread'])
    world.hook.write_text(content)
    items = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}
    assert items['thread:project-a:hook']['status'] == 'todo'
    assert items['thread:project-a:resolve']['status'] == 'warn'
    assert 'SECRET-TOKEN' not in json.dumps(items)


@pytest.mark.parametrize('content', ['', 'not a URL', 'https://', 'http://[invalid'])
def test_alert_url_file_must_contain_a_valid_url(world, content):
    secret = world.root / 'alerts.url'
    secret.write_text(content)
    secret.chmod(0o600)
    world.config_path.write_text(world.config_text.replace('[settings]', '[settings]\nalert_url_file = "' + str(secret) + '"'))
    with pytest.raises(ValidationError, match='settings.alert_url_file must contain a valid HTTP URL'):
        load(world.config_path)


@pytest.mark.parametrize('available,custom', [(False, False), (True, False), (False, True)])
def test_doctor_treats_default_upload_as_optional(world, monkeypatch, available, custom):
    from toucan.setup import doctor
    if custom:
        world.config_path.write_text(world.config_text.replace('toucan-upload-loom', 'custom-uploader'))
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: '/fictional/command' if available else None)
    monkeypatch.setattr(world.fx, 'run', lambda argv: subprocess.CompletedProcess(argv, 1, '{}', ''))
    items = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}
    assert items['upload.command']['status'] == ('todo' if custom else 'ok' if available else 'skip')
    if not custom:
        if available:
            assert items['upload.login']['status'] == 'skip'
        else:
            assert 'upload.login' not in items
            assert items['upload.command']['message'] == 'Uploads not set up; optional (install toucan-router-tools for Loom)'
    assert 'sheets.login' not in items


def test_init_leaves_existing_standard_directories_alone(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('TOUCAN_DB', str(tmp_path / 'state/toucan.db'))
    directories = [tmp_path / '.config/toucan', tmp_path / '.config/toucan/hooks', tmp_path / 'state']
    for directory in directories:
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    assert main(['init', '--example']) == 0
    assert all(stat.S_IMODE(directory.stat().st_mode) == 0o755 for directory in directories)


@pytest.mark.parametrize('heartbeat,store_status,fresh_status', [
    ('fresh', 'ok', 'ok'), ('stale', 'ok', 'todo'), ('missing', 'ok', 'todo'),
    ('invalid', 'ok', 'todo'), ('unsupported', 'todo', 'todo'), ('absent', 'todo', 'todo'),
])
def test_teams_doctor_uses_bot_schema_and_idle_heartbeat(world, monkeypatch, heartbeat, store_status, fresh_status):
    import sqlite3
    from toucan.setup import doctor
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: None)
    if heartbeat == 'absent':
        world.paths.bot_db.rename(world.root / 'unavailable.db')
    elif heartbeat != 'fresh':
        with sqlite3.connect(world.paths.bot_db) as con:
            if heartbeat == 'missing':
                con.execute("DELETE FROM meta WHERE key='heartbeat'")
            elif heartbeat == 'unsupported':
                con.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
            else:
                con.execute("UPDATE meta SET value=? WHERE key='heartbeat'",
                            ('2026-10-17T06:54:59Z' if heartbeat == 'stale' else 'invalid',))
    items = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}
    assert items['teams.store']['status'] == store_status
    assert items['teams.fresh']['status'] == fresh_status
    assert items['bot.command']['status'] == items['bot.status']['status'] == 'todo'
    assert 'teams.state' not in items
    if heartbeat == 'unsupported':
        assert world.app.sync()['created'] == []
        assert not world.db.execute("SELECT 1 FROM meta WHERE key='seed:group'").fetchone()
    if heartbeat == 'absent':
        assert not world.paths.bot_db.exists()  # read-only probes must not create a store


def test_removed_teams_key_warns_and_owner_identity_is_required(world, monkeypatch):
    from toucan.setup import doctor
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: None)
    text = world.config_text.replace('owner_teams_id = "owner-entra"', 'owner_teams_id = ""')
    text = text.replace('[sources.teams]', '[sources.teams]\nreader_state = "unused"')
    world.config_path.write_text(text)
    cfg = load(world.config_path)
    notes = warnings(cfg, world.paths)
    assert "sources.teams.reader_state is gone; Teams now reads the Toucan bot's store" in notes
    assert any(n.startswith('unknown key sources.teams.reader_state;') for n in notes)
    assert any('settings.owner_teams_id' in n for n in notes)
    items = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}
    assert items['owner_teams_id']['status'] == 'todo'
    world.cfg['settings']['owner_teams_id'] = ''
    world.team('owner', sender='owner-entra')
    world.app.sync()
    assert world.app.rows("SELECT is_owner FROM messages WHERE source='teams'") == [{'is_owner': 0}]


def test_teams_doctor_reports_catchup_failure_staleness_and_push_gaps(world, monkeypatch):
    import sqlite3
    from datetime import datetime, timezone
    from toucan.setup import doctor
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: None)
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("INSERT OR REPLACE INTO conversations VALUES ('group:chat','groupChat',NULL,'',NULL,'now','now')")
        con.execute("INSERT INTO catchup (conversation_id,cursor,last_success,last_error,http_status) VALUES ('group:chat',NULL,NULL,'HTTP 403: no RSC grant or unsupported chat type',403)")
    items = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}
    assert items['teams.fresh']['status'] == 'ok'  # heartbeat cannot hide catch-up failure
    assert items['teams.catchup']['status'] == 'warn'
    assert 'HTTP 403' in items['teams.catchup']['message']
    assert items['teams.catchup']['command'] == 'toucan-bot status --json'
    world.team('missed', first_source='graph', graph_seen_at=datetime.fromtimestamp(world.now - 600, timezone.utc).isoformat())
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("UPDATE catchup SET last_error=NULL,http_status=NULL,last_success=?",
                    (datetime.fromtimestamp(world.now, timezone.utc).isoformat(),))
    items = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}
    assert items['teams.catchup']['status'] == 'ok'
    assert items['teams.delivery']['status'] == 'warn' and 'catch-up latency' in items['teams.delivery']['message']
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("UPDATE catchup SET last_success=?", (datetime.fromtimestamp(world.now - 601, timezone.utc).isoformat(),))
        con.execute("UPDATE messages SET live_seen_at=?", (datetime.fromtimestamp(world.now, timezone.utc).isoformat(),))
    items = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}
    assert items['teams.catchup']['status'] == 'warn' and '601s ago' in items['teams.catchup']['message']
    assert items['teams.delivery']['status'] == 'ok'


@pytest.mark.parametrize('state', ['failed', 'stale', 'recovered'])
def test_doctor_reports_background_failure_and_recovery_from_local_store(world, monkeypatch, state):
    import sqlite3
    from datetime import datetime, timezone
    from toucan.setup import doctor
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: None)
    stamp = datetime.fromtimestamp(world.now - (601 if state == 'stale' else 1), timezone.utc).isoformat()
    task = dict(last_success=stamp, last_error='OperationalError' if state == 'failed' else None,
                last_failure=stamp, failures=1, interval_seconds=120)
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('task:maintenance', json.dumps(task)))
    items = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}
    item = items['teams.background']
    assert set(item) == {'id', 'area', 'status', 'message', 'fix', 'command'}
    assert item['status'] == ('ok' if state == 'recovered' else 'warn')
    assert item['command'] == ('' if state == 'recovered' else 'toucan-bot status --json')
    if state != 'recovered':
        assert 'maintenance' in item['message']
        assert ('OperationalError' if state == 'failed' else '601s') in item['message']


def test_doctor_reports_per_chat_roster_failure_and_recovery(world, monkeypatch):
    import sqlite3
    from toucan.setup import doctor
    monkeypatch.setattr('toucan.setup.shutil.which', lambda name: None)
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("INSERT INTO meta VALUES ('roster_error:group:chat','HTTP 401')")
    item = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}['teams.roster']
    assert set(item) == {'id', 'area', 'status', 'message', 'fix', 'command'}
    assert item['status'] == 'warn' and 'HTTP 401' in item['message']
    assert 'person aliases' in item['message'] and item['command'] == 'toucan-bot status --json'
    with sqlite3.connect(world.paths.bot_db) as con:
        con.execute("DELETE FROM meta WHERE key='roster_error:group:chat'")
    item = {i['id']: i for i in doctor(world.config_path, world.fx, world.paths)}['teams.roster']
    assert item['status'] == 'ok'
