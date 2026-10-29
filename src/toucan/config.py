"""Configuration and identity validation. Display names never establish identity."""
import json
import os
import re
import stat
import tomllib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULTS = dict(rewake_minutes=15, wake_gap_minutes=2, max_wakes=3, claimed_stale_hours=4,
                done_unclosed_minutes=30, whatsapp_stale_minutes=120, rate_per_hour=20,
                context_hours=24, ack_alert_minutes=5, fallback_minutes=5, rotate_tokens=200000,
                rotate_stall_minutes=60, owner_whatsapp='', owner_teams_id='', owner_name='the owner', timezone='UTC', alert_url='',
                alert_url_file='', upload_command='toucan-upload-loom', transcribe='', followup_minutes=120,
                router_general_thread='', router_commands=(), router_timeout_seconds=60, router_history=20,
                router_budget_seconds=90)
ROUTER_MODES = ('off', 'shadow')
ROUTER_BUDGET_MAX = 180  # seconds; keeps a sync well inside the timer unit's 5-minute timeout
PHONE = re.compile(r'^\d+@s\.whatsapp\.net$')
JID = re.compile(r'^\d+@(s\.whatsapp\.net|lid|g\.us)$')
# Keys toucan reads (active_ts is added by load). Anything else is likely a typo that would do nothing.
KEYS = dict(settings=set(DEFAULTS) | {'ntfy'}, people={'name', 'role', 'teams_id', 'whatsapp', 'email'},
            threads={'t3_thread', 'project', 'hook_file', 'fallback', 'rotate_tokens'},
            projects={'name', 'aliases', 'description', 'thread'},
            routes={'name', 'label', 'source', 'chats', 'thread', 'mode', 'requesters', 'active_from', 'router',
                    'post_as_bot', 'owner_reply_closes', 'reply_via_whatsapp', 'reply_to', 'reply_prefix', 'active_ts'},
            sources={'whatsapp': {'store', 'account', 'service_unit'},
                     'teams': {'bot_db', 'stale_minutes', 'service_unit'},
                     'email': {'notmuch_config', 'stale_minutes'}}, t3={'db'})
SOURCE_DEFAULTS = dict(whatsapp=dict(store='~/.local/state/wacli', account='wa', service_unit=''),
                       teams=dict(bot_db='~/.local/state/toucan-bot/bot.db', stale_minutes=5, service_unit=''),
                       email=dict(notmuch_config='', stale_minutes=60))
TEXT_SETTINGS = {'owner_whatsapp', 'owner_teams_id', 'owner_name', 'timezone', 'alert_url', 'alert_url_file', 'upload_command', 'transcribe', 'ntfy',
                 'router_general_thread'}
LIST_SETTINGS = {'router_commands'}


class ValidationError(ValueError):
    pass


class Refused(ValueError):
    pass


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('timezone required')
        return dt.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('invalid timestamp; use ISO 8601 with a timezone') from None


def hook_warning(thread):
    path = Path(thread['hook_file']).expanduser()
    try:
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            return 'hook_file must exist and have mode 600'
        from .t3 import hook_info
        if not hook_info(path.read_text().strip()):
            return 'hook_file must contain a valid HTTP URL with an /api/hooks/ task path'
    except (OSError, UnicodeError):
        return 'hook_file cannot be inspected'
    return None


def http_url(value):
    try:
        url = urlsplit(value)
        url.port
        return url.scheme in ('http', 'https') and bool(url.hostname) and not any(c.isspace() for c in value)
    except ValueError:
        return False


def load(path=None):
    try:
        default = Path(os.environ.get('XDG_CONFIG_HOME', '~/.config')).expanduser() / 'toucan/toucan.toml'
        with Path(path or os.environ.get('TOUCAN_CONFIG', default)).expanduser().open('rb') as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        raise ValidationError('cannot load config; check its path and TOML syntax') from None
    return validate(cfg)


def validate(cfg):
    if any(not isinstance(cfg.get(k, {}), dict) for k in ('settings', 'people', 'threads', 'projects', 'sources', 't3')):
        raise ValidationError('settings, people, threads and projects must be TOML tables')
    settings = cfg['settings'] = DEFAULTS | cfg.get('settings', {})
    for key in DEFAULTS:
        if key not in TEXT_SETTINGS | LIST_SETTINGS:
            value = settings[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < float('inf'):
                raise ValidationError(f'settings.{key} must be positive')
    for key in ('max_wakes', 'rate_per_hour', 'rotate_tokens', 'router_history'):
        if not isinstance(settings[key], int):
            raise ValidationError(f'settings.{key} must be an integer')
    for key in TEXT_SETTINGS & settings.keys():
        if not isinstance(settings[key], str):
            raise ValidationError(f'settings.{key} must be text')
    commands = settings['router_commands']
    if (not isinstance(commands, (list, tuple)) or
            any(not isinstance(c, list) or not c or any(not isinstance(a, str) or '\0' in a for a in c) or not c[0] for c in commands)):
        raise ValidationError('settings.router_commands must be an array of commands, each an array of text such as ["claude", "-p"]')
    if settings['router_budget_seconds'] > ROUTER_BUDGET_MAX:
        raise ValidationError(f'settings.router_budget_seconds must be at most {ROUTER_BUDGET_MAX}')
    if settings['router_timeout_seconds'] > settings['router_budget_seconds']:
        raise ValidationError('settings.router_timeout_seconds must not exceed router_budget_seconds')
    settings['owner_teams_id'] = settings['owner_teams_id'].lower()
    if 'ntfy' in settings and not settings['alert_url']:
        settings['alert_url'] = settings['ntfy']
    if settings['alert_url_file']:
        path = Path(settings['alert_url_file']).expanduser()
        try:
            if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise OSError()
            settings['alert_url'] = path.read_text().strip()
        except (OSError, UnicodeError):
            raise ValidationError('settings.alert_url_file must be a readable mode-600 file') from None
        if not http_url(settings['alert_url']):
            raise ValidationError('settings.alert_url_file must contain a valid HTTP URL')
    if settings['alert_url'] and not http_url(settings['alert_url']):
        raise ValidationError('settings.alert_url must be an HTTP URL')
    try:
        ZoneInfo(settings['timezone'])
    except (ValueError, ZoneInfoNotFoundError):
        raise ValidationError('settings.timezone must be an IANA timezone') from None
    for source, defaults in SOURCE_DEFAULTS.items():
        entry = cfg.setdefault('sources', {}).get(source, {})
        if not isinstance(entry, dict):
            raise ValidationError(f'sources.{source} must be a table')
        entry = cfg['sources'][source] = defaults | entry
        for key, value in entry.items():
            if key == 'stale_minutes':
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < float('inf'):
                    raise ValidationError(f'sources.{source}.stale_minutes must be positive')
            elif key in defaults and not isinstance(value, str):
                raise ValidationError(f'sources.{source}.{key} must be text')
    cfg.setdefault('t3', {}).setdefault('db', '~/.t3/userdata/statev2.sqlite')
    if not isinstance(cfg['t3']['db'], str) or not cfg['t3']['db']:
        raise ValidationError('t3.db must be a path')
    people, threads = (cfg.setdefault(k, {}) for k in ('people', 'threads'))
    routes = cfg.setdefault('routes', [])
    if not isinstance(routes, list):
        raise ValidationError('define at least one [[routes]] entry')
    identities = set()
    for key, person in people.items():
        if not isinstance(person, dict) or not isinstance(person.get('name'), str) or not person['name'] or not isinstance(person.get('whatsapp', []), list) or not isinstance(person.get('email', []), list):
            raise ValidationError(f'person {key}: name and a WhatsApp list required')
        for source, ids in [('teams', [person.get('teams_id')]), ('whatsapp', person.get('whatsapp', [])), ('email', person.get('email', []))]:
            for identity in filter(None, ids):
                if not isinstance(identity, str) or (source == 'whatsapp' and not JID.fullmatch(identity)):
                    raise ValidationError(f'person {key}: invalid identity')
                if source == 'teams':
                    identity = person['teams_id'] = identity.lower()
                if source == 'email':
                    identity = identity.lower()
                    if not re.fullmatch(r'[^\s@]+@[^\s@]+', identity):
                        raise ValidationError(f'person {key}: invalid email')
                if (source, identity) in identities:
                    raise ValidationError('identity assigned to multiple people')
                identities.add((source, identity))
    for key, thread in threads.items():
        if not isinstance(thread, dict) or not isinstance(thread.get('hook_file'), str) or not thread['hook_file']:
            raise ValidationError(f'thread {key}: hook_file required')
        if 't3_thread' in thread and (not isinstance(thread['t3_thread'], str) or not thread['t3_thread'].strip()):
            raise ValidationError(f'thread {key}: t3_thread must be non-empty text')
        if 'rotate_tokens' in thread and (type(thread['rotate_tokens']) is not int or thread['rotate_tokens'] <= 0):
            raise ValidationError(f'thread {key}: rotate_tokens must be a positive integer')
        if 'fallback' in thread and (thread['fallback'] not in threads or thread['fallback'] == key):
            raise ValidationError(f'thread {key}: fallback must be another configured thread')
    terms = {}
    for key, project in cfg.setdefault('projects', {}).items():
        if not isinstance(project, dict) or not isinstance(project.get('name'), str) or not project['name'].strip():
            raise ValidationError(f'project {key}: name required')
        if not isinstance(project.get('thread'), str) or project['thread'] not in threads:
            raise ValidationError(f'project {key}: thread must be a configured thread')
        aliases = project.get('aliases', [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not re.search(r'\w', a) for a in aliases):
            raise ValidationError(f'project {key}: aliases must be a list of words or names')
        description = project.get('description', '')
        if not isinstance(description, str) or '\n' in description or '\r' in description:
            raise ValidationError(f'project {key}: description must be one line of text')
        for term in {' '.join(t.lower().split()) for t in (key, project['name'], *aliases)}:
            if terms.setdefault(term, key) != key:
                raise ValidationError(f'projects {terms[term]} and {key} both use the name or alias {json.dumps(term)}')
    general = settings['router_general_thread']
    if general and general not in threads:
        raise ValidationError('settings.router_general_thread must be a configured thread')
    seen, names = set(), set()
    for route in routes:
        if not isinstance(route, dict):
            raise ValidationError('each route must be a TOML table')
        name = route.get('name')
        if not isinstance(name, str) or not name or name in names:
            raise ValidationError('route names must be present and unique')
        names.add(name)
        if route.get('source') not in ('whatsapp', 'teams', 'email') or route.get('mode') not in ('act', 'draft'):
            raise ValidationError(f'route {name}: source and mode must be explicitly supported')
        if route['source'] == 'email':
            if route['mode'] != 'draft':
                raise ValidationError(f'route {name}: email is read-only, Toucan never sends email')
            if any(k in route for k in ('post_as_bot', 'owner_reply_closes', 'reply_to', 'reply_via_whatsapp')):
                raise ValidationError(f'route {name}: reply delivery options are invalid on email routes')
        if 'reply_via_whatsapp' in route and (type(route['reply_via_whatsapp']) is not bool or route['source'] != 'teams' or route['mode'] != 'act'):
            raise ValidationError(f'route {name}: reply_via_whatsapp is only valid on Teams act routes and must be a boolean')
        if not isinstance(route.get('thread'), str) or route['thread'] not in threads:
            raise ValidationError(f'route {name}: unknown thread')
        router = route.setdefault('router', 'off')
        if router == 'live':
            raise ValidationError(f'route {name}: router = "live" is not available yet; use "shadow" to log decisions')
        if router not in ROUTER_MODES:
            raise ValidationError(f'route {name}: router must be "off" or "shadow"')
        if router != 'off' and not general:
            raise ValidationError(f'route {name}: router needs settings.router_general_thread, the thread for off-topic requests')
        if not isinstance(route.get('requesters'), list) or not route['requesters']:
            raise ValidationError(f'route {name}: requesters required')
        if any(not isinstance(p, str) or p not in people for p in route['requesters']):
            raise ValidationError(f'route {name}: unknown person')
        if not isinstance(route.get('chats'), list) or not route['chats']:
            raise ValidationError(f'route {name}: chats required')
        for chat in route['chats']:
            if not isinstance(chat, str) or not chat or (route['source'] == 'whatsapp' and not JID.fullmatch(chat)):
                raise ValidationError(f'route {name}: invalid chat')
            if route['source'] == 'email' and not re.fullmatch(r'[^\s@]+@[^\s@]+', chat):
                raise ValidationError(f'route {name}: invalid mailbox address')
            pair = route['source'], chat.lower() if route['source'] == 'email' else chat
            if pair in seen:
                raise ValidationError(f'route {name}: chat assigned to two routes')
            seen.add(pair)
        if not isinstance(route.get('label', ''), str):
            raise ValidationError(f'route {name}: label must be text')
        if not isinstance(route.get('post_as_bot', False), bool) or (route.get('post_as_bot') and route['source'] != 'teams'):
            raise ValidationError(f'route {name}: post_as_bot must be true or false, and only on Teams routes')
        if not isinstance(route.get('owner_reply_closes', False), bool) or (route.get('owner_reply_closes') and route['source'] != 'teams'):
            raise ValidationError(f'route {name}: owner_reply_closes must be true or false, and only on Teams routes')
        if 'reply_to' in route and (route['source'] != 'whatsapp' or route['mode'] != 'act' or not reply_phone(cfg, route)):
            raise ValidationError(f'route {name}: reply_to must name a person with a WhatsApp phone JID, '
                                  'and only on WhatsApp act routes')
        if not isinstance(route.get('reply_prefix', ''), str):
            raise ValidationError(f'route {name}: reply_prefix must be text')
        route['active_ts'] = timestamp(route.get('active_from'))
    return cfg


def reply_phone(cfg, route):
    """Phone JID of the route's reply_to person, who gets every reply instead of the chat."""
    person = cfg['people'].get(route.get('reply_to')) if isinstance(route.get('reply_to'), str) else None
    return next((i for i in (person or {}).get('whatsapp', []) if PHONE.fullmatch(i)), None)


def prefixed(route, body):
    return f'{route["reply_prefix"]}\n\n{body}' if route.get('reply_prefix') and body else body


def warnings(cfg, paths=None):
    from . import sources

    result = [f'{key}: {w}' for key, t in cfg['threads'].items() if (w := hook_warning(t))]
    for key, thread in cfg['threads'].items():
        live = sources.hook_file_thread(paths or sources.Paths.from_config(cfg), thread['hook_file'])
        if not live and not thread.get('t3_thread'):
            result.append(f'{key}: no T3 thread resolves from the hook or t3_thread')
        elif live and thread.get('t3_thread') and live != thread['t3_thread']:
            result.append(f"{key}: t3_thread is stale; Toucan uses the hook's thread")
    if not PHONE.fullmatch(cfg['settings']['owner_whatsapp']):
        result.append('owner_whatsapp must be an explicit phone JID before drafts or escalations can be sent')
    if any(r['source'] == 'teams' for r in cfg['routes']) and not cfg['settings']['owner_teams_id']:
        result.append('Set settings.owner_teams_id to identify owner messages in Teams')
    if 'reader_state' in cfg['sources']['teams']:
        result.append("sources.teams.reader_state is gone; Teams now reads the Toucan bot's store")
    if 'ntfy' in cfg['settings']:
        result.append('settings.ntfy is renamed to alert_url')
    unknown = [k for k in cfg if k not in KEYS] + [f'settings.{k}' for k in cfg['settings'] if k not in KEYS['settings']]
    for table in ('people', 'threads', 'projects'):
        unknown += [f'{table}.{key}.{k}' for key, entry in cfg[table].items() for k in entry if k not in KEYS[table]]
    unknown += [f'routes.{r["name"]}.{k}' for r in cfg['routes'] for k in r if k not in KEYS['routes']]
    unknown += [f'sources.{k}' for k in cfg['sources'] if k not in SOURCE_DEFAULTS]
    unknown += [f'sources.{source}.{k}' for source in SOURCE_DEFAULTS for k in cfg['sources'][source] if k not in KEYS['sources'][source]]
    unknown += [f't3.{k}' for k in cfg['t3'] if k not in KEYS['t3']]
    result += [f'unknown key {k}; check the spelling' for k in unknown]
    return result
