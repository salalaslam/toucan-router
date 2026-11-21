"""Anonymous usage events: at most one `router.active` event a day, so the project can count active installs.

Nothing is sent until the operator says yes (`toucan telemetry on`). Until they answer, `toucan doctor`
and the daily list ask them. docs/telemetry.md lists every field. Only counts and fixed names are sent;
message text, chat names, identities, thread ids and config values never are. A failed send never fails
a command."""
import hashlib
import json
import os
import platform
import secrets
import sys
from importlib import metadata

from .storage import state_path

# A PostHog project key can only write events, so it is safe to publish.
POSTHOG_KEY = 'phc_rzCqfDL7K8VJ7G5x7azZRnyvxnwjZMhXcVCzaFAm8ynC'  # gitleaks:allow (public, write-only project key)
POSTHOG_URL = 'https://us.i.posthog.com/i/v0/e/'
EVENT = 'router.active'
DAY = 86400
RETRY = 3600
TIMEOUT = 3
OFF = {'0', 'false', 'no', 'off'}
NOTICE = ('toucan: sending one anonymous usage event a day, as agreed (version and counts, never message content). '
          '`toucan telemetry off` stops it. See docs/telemetry.md.')
# Agents put this to the operator word for word (`toucan doctor` item `telemetry`, field `ask`). They used to
# read "telemetry" as something to switch off on the operator's behalf, so the operator never saw the choice.
QUESTION = ("Toucan Router is new, and its maintainers can't tell whether anyone uses it. May it send one anonymous "
            'event a day so they can count active installs? The event holds the Toucan, Python and OS versions and '
            'counts of routes, threads, people and requests. It never holds messages, names, phone numbers, IDs or '
            'config values. The count decides whether the maintainers keep working on Toucan Router and what '
            'they build next.')
ASK = dict(header='Usage data', question=QUESTION, details='docs/telemetry.md (or `toucan guide telemetry`)',
           options=[dict(label='Yes, share counts', command='toucan telemetry on',
                         description='One anonymous event a day. `toucan telemetry off` stops it later.'),
                    dict(label='No', command='toucan telemetry off', description='Nothing is sent.')])
DIGEST_LINE = ("Toucan Router is new and its maintainers can't tell whether anyone uses it. May it send one anonymous "
               'usage event a day (versions and counts, never messages or names)? Run `toucan telemetry on` or '
               '`toucan telemetry off`. Either answer removes this line.')


def _env_off(environ):
    environ = os.environ if environ is None else environ
    return (environ.get('TOUCAN_TELEMETRY', '').strip().lower() in OFF or
            environ.get('DO_NOT_TRACK', '').strip().lower() not in ('', *OFF))


def enabled(settings, environ=None):
    """On only after an explicit yes: `settings.telemetry = true`, which `toucan telemetry on` writes."""
    return settings.get('telemetry') is True and not _env_off(environ) and not POSTHOG_KEY.endswith('REPLACE_ME')


def pending(settings, environ=None):
    """True while the operator has not answered. A `telemetry = false` written before Toucan asked (often by
    an installing agent) does not count as an answer, so upgraded installs are asked once too."""
    return (settings.get('telemetry') is not True and settings.get('telemetry_asked') is not True
            and not _env_off(environ) and not POSTHOG_KEY.endswith('REPLACE_ME'))


def state_file():
    return state_path().parent / 'telemetry.json'


def version():
    try:
        return metadata.version('toucan-router')
    except metadata.PackageNotFoundError:
        return 'unknown'


def properties(cfg, db, now):
    routes = cfg['routes']
    modes = {mode: sum(1 for r in routes if (r.get('router') or 'off') == mode) for mode in ('off', 'shadow', 'live')}
    sources = sorted({r['source'] for r in routes if r['source'] in ('whatsapp', 'teams', 'email')})
    requests = db.execute('SELECT count(*) FROM requests WHERE created_at>=?', (now - DAY,)).fetchone()[0]
    return dict(version=version(), python=platform.python_version(), os=platform.system().lower(),
                routes=len(routes), threads=len(cfg['threads']), people=len(cfg['people']),
                router_off=modes['off'], router_shadow=modes['shadow'], router_live=modes['live'],
                sources=sources, requests_24h=requests)


def _read(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) and isinstance(data.get('install_id'), str) else None
    except (OSError, ValueError):
        return None


def _write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def report(cfg, db, effects, path=None, environ=None, stderr=None):
    """Send today's event if it is due. Returns 'off', 'skip', 'sent' or 'failed'; never raises."""
    try:
        if not enabled(cfg['settings'], environ):
            return 'off'
        path = path or state_file()
        now = effects.now()
        state = _read(path)
        if state is None:
            state = dict(install_id=secrets.token_hex(16), next_at=0)
            print(NOTICE, file=stderr or sys.stderr)
        if now < state.get('next_at', 0):
            return 'skip'
        # Claim the slot before sending so overlapping runs and a hung endpoint cannot repeat the event.
        state['next_at'] = now + RETRY
        _write(path, state)
        body = dict(api_key=POSTHOG_KEY, event=EVENT,
                    distinct_id=hashlib.sha256(state['install_id'].encode()).hexdigest(),
                    properties=properties(cfg, db, now) | {'$process_person_profile': False, '$geoip_disable': True,
                                                            '$lib': 'toucan-router'})
        effects.post(POSTHOG_URL, json.dumps(body).encode(), {'Content-Type': 'application/json'}, timeout=TIMEOUT)
        state['next_at'] = now + DAY
        _write(path, state)
        return 'sent'
    except Exception:
        return 'failed'
