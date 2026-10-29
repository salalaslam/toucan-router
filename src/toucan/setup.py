"""Local setup checks. Never display credentials or hook URLs."""
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
from importlib.resources import files
from pathlib import Path

from . import router, sources, t3
from .config import PHONE, ValidationError, hook_warning, load, warnings
from .effects import Effects, local_run
from .storage import account_warning, private_dir, readonly, state_path


def config_path(value=None):
    default = Path(os.environ.get('XDG_CONFIG_HOME', '~/.config')).expanduser() / 'toucan/toucan.toml'
    return Path(value or os.environ.get('TOUCAN_CONFIG', default)).expanduser()


def init(path=None, force=False, **options):
    from .agentsetup import initialize
    return initialize(config_path(path), force, **options)


def redact(value, show=False):
    return value if show else '…' + value[-4:]


def state_checks(cfg):
    path = state_path()
    items = []
    existing = [p for p in (path.parent, path) if p.exists()]
    accessible = any(p.stat().st_mode & 0o077 for p in existing)
    items.append(dict(id='state.permissions', area='configuration',
                      status='warn' if accessible else 'ok' if existing else 'skip',
                      message='State directory or database is group/world accessible' if accessible else
                              'State permissions private' if existing else 'State not created yet',
                      fix='Read docs/operations.md', command=(
                          f'chmod 700 {shlex.quote(str(path.parent))}' +
                          (f' && chmod 600 {shlex.quote(str(path))}' if path.exists() else '')
                          if accessible else '')))
    warning = None
    if path.is_file():
        try:
            db = readonly(path)
            try:
                warning = account_warning(db, cfg['sources']['whatsapp']['account'])
            finally:
                db.close()
        except (OSError, sqlite3.Error):
            warning = 'State database unavailable; cannot check sources.whatsapp.account'
    items.append(dict(id='whatsapp.account', area='whatsapp', status='todo' if warning else 'ok',
                      message=warning or 'WhatsApp account matches stored messages',
                      fix='Read docs/configuration.md', command="toucan config set sources.whatsapp.account 'ACCOUNT'" if warning else ''))
    return items


def host_checks(cfg, paths, fx):
    items = []

    def add(id, status, message, command=''):
        items.append(dict(id='host.' + id, area='host', status=status, message=message,
                          fix='Read docs/setup-t3.md', command=command if status in ('todo', 'warn') else ''))

    present = {name: bool(shutil.which(name)) for name in ('claude', 'codex')}
    provider = any(present.values())
    t3_available = t3.probe(paths) or bool(shutil.which('t3code') or shutil.which('t3'))
    if not t3_available:
        try:
            result = local_run(fx, ['ps', '-eo', 'args='])
            t3_available = result.returncode == 0 and any(
                't3code' in line and ('server' in line or 'node ' in line)
                for line in result.stdout.splitlines())
        except (OSError, subprocess.SubprocessError, TimeoutError):
            pass
    add('t3', 'ok' if t3_available else 'todo',
        'T3 host available' if t3_available else 'Install T3 Code; see https://github.com/pingdotgg/t3code',
        'toucan guide setup-t3  # see https://github.com/pingdotgg/t3code')
    for name in ('claude', 'codex'):
        install = 'npm install -g ' + ('@anthropic-ai/claude-code' if name == 'claude' else '@openai/codex')
        if not present[name]:
            add(name, 'skip' if provider else 'todo', f'{name} not on PATH', install)
            continue
        argv = ['claude', 'auth', 'status'] if name == 'claude' else ['codex', 'login', 'status']
        try:
            result = local_run(fx, argv)
            # Older Claude releases have no auth-status command. Presence is enough then.
            unsupported = name == 'claude' and result.returncode != 0 and any(
                marker in (result.stderr + result.stdout).lower()
                for marker in ('unknown command', 'unrecognized', 'unexpected argument', 'invalid command'))
            good = result.returncode == 0 or unsupported
            add(name, 'ok' if good else 'todo', f'{name} available' if good else f'{name} needs local authentication',
                'claude auth login' if name == 'claude' else 'codex login')
        except (OSError, subprocess.SubprocessError, TimeoutError):
            add(name, 'warn', f'{name} authentication status unavailable', shlex.join(argv))
    add('provider', 'ok' if provider else 'todo', 'Agent provider CLI available' if provider else 'Install Claude Code or Codex',
        'npm install -g @anthropic-ai/claude-code')
    fallback = any(thread.get('fallback') for thread in cfg.get('threads', {}).values())
    one = sum(present.values()) == 1
    add('fallback', 'warn' if fallback and one else 'ok' if fallback and provider else 'skip',
        'Fallback threads need another provider CLI' if fallback and one else
        'Provider CLIs available for fallback threads' if fallback and provider else 'No provider fallback to check',
        'npm install -g ' + ('@openai/codex' if present['claude'] else '@anthropic-ai/claude-code'))
    return items


def doctor(path=None, effects=None, paths=None, show_ids=False):
    fx = effects or Effects()
    items = []

    def add(id, area, status, message, doc):
        commands = {
            'config': 'toucan guide configuration',
            'owner_name': "toucan config set settings.owner_name 'NAME'",
            'owner_whatsapp': "toucan config set settings.owner_whatsapp '15550000000@s.whatsapp.net'",
            'owner_teams_id': "toucan config set settings.owner_teams_id '00000000-0000-0000-0000-000000000000'",
            't3.db': "toucan config set t3.db 'PATH'",
            't3.schema': 'toucan guide setup-t3',
            'whatsapp.command': 'toucan guide setup-whatsapp',
            'whatsapp.store': 'wacli auth', 'whatsapp.fresh': 'toucan enable',
            'teams.store': 'toucan enable', 'teams.fresh': 'toucan enable',
            'teams.catchup': 'toucan-bot status --json', 'teams.delivery': 'toucan-bot status --json',
            'teams.background': 'toucan-bot status --json', 'teams.roster': 'toucan-bot status --json',
            'teams.service': 'toucan enable', 'bot.command': 'uv tool install toucan-teams-bot',
            'bot.status': 'toucan-bot status --json',
            'email.command': 'toucan guide setup-email', 'email.fresh': 'notmuch new',
            'transcribe.command': 'toucan guide setup-whatsapp', 'upload.command': 'uv tool install toucan-router-tools',
            'upload.login': 'toucan-upload-loom login', 'sync.timer': 'toucan enable',
            'router.commands': "toucan config set settings.router_commands '[[\"COMMAND\", \"ARG\"]]'",
            'router.health': 'toucan status --json',
        }
        command = commands.get(id, f'toucan guide {doc}')
        if id.startswith('thread:'):
            key = id.split(':', 2)[1]
            command = f'toucan hook set {shlex.quote(key)}  # pipe the T3 webhook URL on stdin'
        items.append(dict(id=id, area=area, status=status, message=message, fix=f'Read docs/{doc}.md',
                          command=command if status in ('todo', 'warn') else ''))

    def check(id, area, good, message, doc, bad='todo'):
        add(id, area, 'ok' if good else bad, message, doc)

    try:
        cfg = load(config_path(path))
    except ValidationError as error:
        add('config', 'configuration', 'todo', str(error), 'configuration')
        items[0]['command'] = "toucan init --owner-name 'NAME' --timezone UTC"
        items.extend(host_checks({}, paths or sources.Paths(), fx))
        return items
    paths = paths or sources.Paths.from_config(cfg)
    config_notes = [w for w in warnings(cfg, paths) if w.startswith(('unknown key ', 'settings.ntfy', 'sources.teams.reader_state'))]
    add('config', 'configuration', 'warn' if config_notes else 'ok', '; '.join(config_notes) or 'Config found and valid', 'configuration')
    if config_notes:
        note = config_notes[0]
        key = note.split(';', 1)[0].removeprefix('unknown key ') if note.startswith('unknown key ') else note.split()[0]
        items[-1]['command'] = 'toucan config unset ' + shlex.quote(key)
    settings = cfg['settings']
    items.extend(state_checks(cfg))
    check('owner_name', 'configuration', bool(settings['owner_name'].strip()) and settings['owner_name'] != 'the owner', 'Set settings.owner_name' if settings['owner_name'] == 'the owner' else 'Owner name set', 'configuration')
    check('owner_whatsapp', 'configuration', bool(PHONE.fullmatch(settings['owner_whatsapp'])), 'Owner WhatsApp set' if PHONE.fullmatch(settings['owner_whatsapp']) else 'Set settings.owner_whatsapp', 'setup-whatsapp')
    add('timezone', 'configuration', 'ok', f'Timezone {settings["timezone"]}', 'configuration')
    for key, thread in cfg['threads'].items():
        warning = hook_warning(thread)
        check(f'thread:{key}:hook', 't3', not warning, f'{key}: {warning or "hook file has mode 600"}', 'setup-t3')
        live = sources.hook_file_thread(paths, thread['hook_file'])
        fallback = None
        if not live and t3.thread_exists(paths, thread.get('t3_thread')):
            fallback = thread['t3_thread']
        add(f'thread:{key}:resolve', 't3', 'ok' if live else 'warn' if fallback else 'todo',
            f'{key}: thread {redact(live, show_ids)}' if live else
            f'{key}: only t3_thread resolves; hook does not resolve' if fallback else f'{key}: no thread resolves', 'setup-t3')
    try:
        db = readonly(paths.t3)
        db.close()
        readable = True
    except (OSError, sqlite3.Error):
        readable = False
    check('t3.db', 't3', readable, 'T3 database readable' if readable else 'T3 database unavailable; optional history features disabled', 'setup-t3', 'warn')
    compatible = t3.probe(paths)
    check('t3.schema', 't3', compatible, 'T3 schema supported' if compatible else 'T3 schema unavailable or unsupported; optional features disabled', 'setup-t3', 'warn')
    items.extend(host_checks(cfg, paths, fx))
    active = {r['source'] for r in cfg['routes']}
    short_fx = Effects(now=fx.now, run=lambda argv: local_run(fx, argv))
    problems = sources.health(paths, short_fx, fx.now(), active - {'email'}, settings['whatsapp_stale_minutes'])
    if settings['owner_whatsapp'] or active:
        check('whatsapp.command', 'whatsapp', bool(shutil.which('wacli')), 'wacli on PATH' if shutil.which('wacli') else 'Install wacli', 'setup-whatsapp')
        check('whatsapp.store', 'whatsapp', os.access(paths.wa / 'wacli.db', os.R_OK), 'WhatsApp store readable' if os.access(paths.wa / 'wacli.db', os.R_OK) else 'WhatsApp store unavailable', 'setup-whatsapp')
    if 'whatsapp' in active:
        check('whatsapp.fresh', 'whatsapp', not any(k.startswith('health:whatsapp:') for k in problems), 'WhatsApp sync fresh' if not any(k.startswith('health:whatsapp:') for k in problems) else 'WhatsApp sync stale or unhealthy', 'setup-whatsapp')
    if 'teams' in active:
        try:
            sources.teams_store(paths)
            present = True
        except (OSError, ValueError, sqlite3.Error):
            present = False
        check('teams.store', 'teams', present, 'Teams bot store readable and supported' if present else 'Teams bot store missing or unsupported', 'setup-teams')
        fresh = not any(k == 'health:teams:stale' for k in problems)
        check('teams.fresh', 'teams', fresh, 'Teams bot heartbeat fresh' if fresh else 'Teams bot heartbeat unavailable or stale', 'setup-teams')
        try:
            coverage = sources.teams_coverage(paths, fx.now())
            # Only routed chats matter; the same check raises `toucan sync` alerts.
            failures = sources.teams_catchup(paths, cfg['routes'], fx.now(), strict=True)
            routed = {chat for route in cfg['routes'] if route['source'] == 'teams' for chat in route['chats']}
            ages = [c['age'] for c in coverage['chats'] if c['id'] in routed and c['age'] is not None]
            message = ('Catch-up failing or stale: ' + '; '.join(failures.values()) if failures else
                       'Catch-up healthy; oldest last success %.0fs ago' % max(ages) if ages else
                       'No routed group chats need catch-up')
            add('teams.catchup', 'teams', 'warn' if failures else 'ok', message, 'setup-teams')
            tasks = coverage['background']
            errors = []
            for name, task in tasks.items():
                age = fx.now() - sources.timestamp(task['last_success']) if task['last_success'] else None
                if task['last_error'] or age is None or age < -60 or age > max(300, task['interval_seconds'] * 3):
                    errors.append(name + ': ' + (task['last_error'] or ('no completed iteration' if age is None else 'last success %.0fs ago' % age)))
            add('teams.background', 'teams', 'warn' if errors else 'ok' if tasks else 'todo',
                '; '.join(errors) if errors else 'Bot background tasks healthy' if tasks else
                'Background task health unavailable; restart the upgraded bot', 'setup-teams')
            roster_errors = coverage['roster_errors']
            add('teams.roster', 'teams', 'warn' if roster_errors else 'ok',
                'Roster refresh failing; person aliases unavailable: ' + '; '.join(
                    redact(chat, show_ids) + ': ' + error for chat, error in roster_errors.items())
                if roster_errors else 'No reported roster refresh failures', 'setup-teams')
            missing = coverage['ordinary_graph_only']
            add('teams.delivery', 'teams', 'warn' if missing else 'ok',
                f'{missing} ordinary messages in 24h first seen by Graph without live push after 5 minutes; '
                'Toucan depends on catch-up latency' if missing else
                'No observed ordinary-message push gaps in 24h; this does not prove live delivery', 'setup-teams')
        except (OSError, ValueError, sqlite3.Error):
            add('teams.catchup', 'teams', 'todo', 'Catch-up health unavailable; restart or upgrade the bot', 'setup-teams')
            add('teams.background', 'teams', 'todo', 'Background task health unavailable; restart or upgrade the bot', 'setup-teams')
            add('teams.roster', 'teams', 'todo', 'Roster health unavailable; restart or upgrade the bot', 'setup-teams')
            add('teams.delivery', 'teams', 'warn', 'Live-delivery coverage unavailable', 'setup-teams')
        if paths.teams_service:
            good = 'health:teams:service' not in problems
            check('teams.service', 'teams', good, 'Teams bot service active' if good else 'Teams bot service unhealthy', 'setup-teams')
        check('owner_teams_id', 'teams', bool(settings['owner_teams_id']), 'Owner Teams ID set' if settings['owner_teams_id'] else 'Set settings.owner_teams_id', 'setup-teams')
        available = bool(shutil.which('toucan-bot'))
        check('bot.command', 'teams', available, 'toucan-bot on PATH' if available else 'Install toucan-teams-bot', 'setup-teams')
        good = False
        if available:
            try:
                result = local_run(fx, ['toucan-bot', 'status', '--json'])
                status = json.loads(result.stdout)
                good = result.returncode == 0 and status.get('ok', False)
            except (OSError, subprocess.SubprocessError, TimeoutError, ValueError, AttributeError):
                pass
        check('bot.status', 'teams', good, 'Bot configured with credentials' if good else 'Configure and register the Teams bot', 'setup-teams')
    if 'email' in active:
        check('email.command', 'email', bool(shutil.which('notmuch')), 'notmuch on PATH' if shutil.which('notmuch') else 'Install notmuch', 'setup-email')
        fresh = False
        if shutil.which('notmuch'):
            try:
                argv = ['notmuch'] + (['--config=' + str(Path(paths.notmuch_config).expanduser())] if paths.notmuch_config else [])
                result = local_run(fx, argv + ['search', '--format=json', '--output=summary', '--sort=newest-first', '--limit=1', '*'])
                rows = json.loads(result.stdout)
                newest = float(rows[0]['timestamp']) if rows else 0
                fresh = result.returncode == 0 and 0 <= fx.now() - newest <= paths.email_stale_minutes * 60
            except (OSError, subprocess.SubprocessError, TimeoutError, ValueError, TypeError, KeyError, IndexError):
                pass
        check('email.fresh', 'email', fresh, 'Email index fresh' if fresh else 'Email index stale or unavailable', 'setup-email')
    for key, doc in [('transcribe', 'setup-whatsapp'), ('upload_command', 'setup-upload')]:
        try:
            command = shlex.split(settings[key])
        except ValueError:
            command = []
        id = 'transcribe.command' if key == 'transcribe' else 'upload.command'
        if key == 'transcribe' and not settings[key]:
            add(id, 'media', 'skip', 'Transcription not configured', doc)
        else:
            available = bool(command and shutil.which(command[0]))
            optional = key == 'upload_command' and command == ['toucan-upload-loom']
            message = ('Uploads not set up; optional (install toucan-router-tools for Loom)' if optional else f'{key} command unavailable')
            check(id, 'media', available, f'{key} available' if available else message, doc, 'skip' if optional else 'todo')
        if key == 'upload_command' and command == ['toucan-upload-loom'] and available:
            logged_in = False
            try:
                result = local_run(fx, [*command, 'status'])
                logged_in = result.returncode == 0 and json.loads(result.stdout).get('logged_in', False)
            except (OSError, subprocess.SubprocessError, TimeoutError, ValueError, AttributeError):
                pass
            check('upload.login', 'media', logged_in, 'Loom logged in' if logged_in else 'Uploads not set up; optional', doc, 'skip')
    if router.routes(cfg):
        commands = settings['router_commands']
        missing = [c[0] for c in commands if not shutil.which(c[0])]
        add('router.commands', 'router', 'warn' if missing or not commands else 'ok',
            'No router_commands: requests the rules cannot place are logged as router unavailable' if not commands else
            'Router commands not on PATH: ' + ', '.join(missing) if missing else f'{len(commands)} router commands available',
            'configuration')
        status, message = 'skip', 'Router has not run yet'
        if state_path().is_file():
            try:
                db = readonly(state_path())
                try:
                    info = router.health(db, cfg, fx.now())
                finally:
                    db.close()
                stuck = info['oldest_pending'] is not None and fx.now() - info['oldest_pending'] > 1800
                bad = info['stage_error'] or info['unavailable_streak'] >= router.UNAVAILABLE_ALERT or stuck
                status = 'warn' if bad else 'ok' if info['last_run'] else 'skip'
                ago = lambda ts: f'{(fx.now() - ts) / 60:.0f} min ago' if ts else 'never'
                message = (f'Router shadow on {", ".join(info["routes"])}: last run {ago(info["last_run"])}, '
                           f'last router command success {ago(info["last_success"])}, {info["pending"]} pending, '
                           f'{info["failures_24h"]} failures in 24h' +
                           (f'; last run failed ({info["stage_error"]["error"]})' if info['stage_error'] else '') +
                           ('; requests have waited over 30 min for a decision' if stuck else ''))
            except (OSError, sqlite3.Error, ValueError, KeyError):
                status, message = 'skip', 'Router state unavailable; it appears after the next sync'
        add('router.health', 'router', status, message, 'operations')
    add('alerts', 'alerts', 'ok' if settings['alert_url'] else 'skip', 'Alert URL configured' if settings['alert_url'] else 'Alerts optional; no URL configured', 'setup-alerts')
    if not shutil.which('systemctl'):
        add('sync.timer', 'automation', 'skip', 'systemctl unavailable', 'operations')
    else:
        try:
            result = local_run(fx, ['systemctl', '--user', 'is-enabled', 'toucan-sync.timer'])
            enabled = result.returncode == 0 and result.stdout.strip() in ('enabled', 'enabled-runtime')
        except (OSError, subprocess.SubprocessError, TimeoutError):
            enabled = False
        check('sync.timer', 'automation', enabled, 'Sync timer enabled' if enabled else 'Enable toucan-sync.timer', 'operations')
    return items


def doctor_text(items):
    labels = [('ok', 'Configured'), ('todo', 'Left to do'), ('warn', 'Warnings'), ('skip', 'Optional / skipped')]
    return '\n\n'.join(label + '\n' + '\n'.join(f'  {i["id"]}: {i["message"]}. {i["fix"]}' + (f'\n    {i["command"]}' if i['command'] else '') for i in items if i['status'] == status)
                        for status, label in labels if any(i['status'] == status for i in items))


def upload(settings, file, title=None):
    path = Path(file).expanduser()
    if not path.is_file():
        raise RuntimeError('upload file does not exist')
    try:
        command = shlex.split(settings['upload_command'])
    except ValueError:
        raise RuntimeError('upload_command has invalid quoting') from None
    if not command:
        raise RuntimeError('upload_command is empty')
    try:
        result = subprocess.run([*command, 'upload', str(path), '--title', title or path.stem],
                                capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise RuntimeError('upload command failed or timed out; check docs/setup-upload.md') from None
    if result.returncode:
        # Provider stderr may contain signed URLs, tokens or account data.
        detail = 'authentication failed' if any(s in result.stderr.lower() for s in ('auth', 'login', 'unauthorized')) else 'uploader reported a failure'
        raise RuntimeError(f'upload failed (exit {result.returncode}): {detail}')
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in ('url', 'id')) or not data['url'].startswith(('https://', 'http://')):
            raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeError('upload command returned invalid JSON; expected url and id') from None
    return dict(url=data['url'], id=data['id'])
