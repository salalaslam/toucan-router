"""Ledger and Preflight for projects that turn them on.

The ledger is a timeline of what each project person sent us and what we sent them, across Teams,
WhatsApp and GitHub. It reads the router's own `messages` and `outbox` tables, plus GitHub issues and
comments copied into `ledger_github`. Preflight checks each outgoing send against that timeline and
the project's review workflow before it is queued. In shadow mode it only records what it would have
held; in enforce mode a held send is refused unless the thread passes `--override REASON`.

Preflight fails open: an error while checking is recorded and the send goes ahead.
"""
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .config import Refused, ValidationError
from .effects import local_run

MODES = ('off', 'shadow', 'enforce')
DEFAULTS = dict(mode='shadow', people=[], pel='', github=[], self_github=[], review_hosts=[],
                review_words=['wireframe', 'mock-up', 'mockup', 'mock up'],
                topics={'sprint': r'(?i)\bsprint\s+(\d+(?:\.\d+)+)', 'issue': r'(?<![\w/&])#(\d+)\b'},
                supersede=[r'(?i)\b(?:these|this|it) replaces?\b',
                           r'(?i)\b(?:replaces?|instead of) (?:the|my|our) [^.\n]{0,40}\b(?:I|we) sent\b'],
                burst_per_hour=4, pel_wait_minutes=30, github_minutes=10)
URL = re.compile(r'https?://[^\s<>()\[\]"\']+')
TAG = re.compile(r'@\[([^\[\]]*)\]')
REPO = re.compile(r'^[\w.-]+/[\w.-]+$')
LOGIN = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$')
PLAIN_HOSTS = ('github.com',)  # proof links, not review artifacts
LOOKBACK_DAYS = 14
GITHUB_KINDS = {'issue': 'issues?state=all&', 'comment': 'issues/comments?', 'review_comment': 'pulls/comments?'}
GITHUB_PAGES = 10
GITHUB_BUDGET_SECONDS = 60  # keeps a slow GitHub from stalling the every-minute sync


class Held(Refused):
    """Preflight refused a send in enforce mode. The caller records `checks` after its transaction rolls back."""

    def __init__(self, message, checks):
        super().__init__(message)
        self.checks, self.thread, self.request = checks, None, None


def validate(key, project, people):
    """Fill defaults and check a project's [ledger] table. Called from config.validate."""
    raw = project.get('ledger')
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValidationError(f'project {key}: ledger must be a table')
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValidationError(f'project {key}: unknown ledger key {sorted(unknown)[0]}; check the spelling')
    ledger = project['ledger'] = {k: (v.copy() if isinstance(v, (list, dict)) else v) for k, v in DEFAULTS.items()} | raw
    where = f'project {key}: ledger'
    if ledger['mode'] not in MODES:
        raise ValidationError(f'{where}.mode must be "off", "shadow" or "enforce"')
    for name in ('people', 'github', 'self_github', 'review_hosts', 'review_words', 'supersede'):
        if not isinstance(ledger[name], list) or any(not isinstance(v, str) or not v.strip() for v in ledger[name]):
            raise ValidationError(f'{where}.{name} must be a list of text')
    if any(p not in people for p in ledger['people']):
        raise ValidationError(f'{where}.people must be configured person keys')
    if 'pel' not in raw and project.get('escalate_to') in ledger['people']:
        ledger['pel'] = project['escalate_to']
    if ledger['pel'] and ledger['pel'] not in ledger['people']:
        raise ValidationError(f'{where}.pel must be one of ledger.people')
    if any(not REPO.fullmatch(r) for r in ledger['github']):
        raise ValidationError(f'{where}.github must list repositories as owner/name')
    if any(not LOGIN.fullmatch(login) for login in ledger['self_github']):
        raise ValidationError(f'{where}.self_github must list GitHub logins')
    ledger['self_github'] = [login.lower() for login in ledger['self_github']]
    ledger['review_hosts'] = [h.lower().strip('.') for h in ledger['review_hosts']]
    ledger['review_words'] = [w.lower() for w in ledger['review_words']]
    if not isinstance(ledger['topics'], dict) or any(not isinstance(v, str) for v in ledger['topics'].values()):
        raise ValidationError(f'{where}.topics must map a name to a regular expression')
    for name, pattern in [*ledger['topics'].items(), *(('supersede', p) for p in ledger['supersede'])]:
        try:
            compiled = re.compile(pattern)
        except re.error:
            raise ValidationError(f'{where}: {name} is not a valid regular expression') from None
        if name in ledger['topics'] and compiled.groups > 1:
            raise ValidationError(f'{where}.topics.{name} may have at most one group, the topic key')
    for name in ('burst_per_hour', 'pel_wait_minutes', 'github_minutes'):
        if type(ledger[name]) is not int or ledger[name] <= 0:
            raise ValidationError(f'{where}.{name} must be a positive integer')


def validate_github_logins(people):
    seen = set()
    for key, person in people.items():
        logins = person.get('github', [])
        if not isinstance(logins, list) or any(not isinstance(v, str) or not LOGIN.fullmatch(v) for v in logins):
            raise ValidationError(f'person {key}: github must be a list of GitHub logins')
        person['github'] = [v.lower() for v in logins]
        for login in person['github']:
            if login in seen:
                raise ValidationError('identity assigned to multiple people')
            seen.add(login)


def projects(cfg, thread=None):
    """Projects with the ledger on, optionally only those a thread owns."""
    return {k: p for k, p in cfg['projects'].items()
            if p.get('ledger') and p['ledger']['mode'] != 'off' and (thread is None or p['thread'] == thread)}


# ---- reading the timeline -------------------------------------------------------------------------

def host(url):
    try:
        return (urlsplit(url).hostname or '').lower()
    except ValueError:
        return ''


def on(name, hosts):
    return any(name == h or name.endswith('.' + h) for h in hosts)


def topics(ledger, text):
    found = []
    for name, pattern in ledger['topics'].items():
        for match in re.finditer(pattern, text or ''):
            key = f'{name}:{(match.group(1) if match.groups() else match.group(0)).lower()}'
            if key not in found:
                found.append(key)
    return found


def addressed(cfg, chat, text, route_people=()):
    """People a message is for: the 1:1 or WhatsApp chat's person, and anyone tagged. A group message
    that tags nobody is for the route's requesters."""
    owner = cfg['settings']['owner_teams_id']
    found = []
    for key, person in cfg['people'].items():
        teams = person.get('teams_id')
        if (chat in person.get('whatsapp', []) or
                teams and teams != owner and (f'@[{teams}]' in (text or '') or (chat or '').endswith('@unq.gbl.spaces') and teams in chat)):
            found.append(key)
    found += [m.group(1).strip() for m in TAG.finditer(text or '') if m.group(1).strip() in cfg['people']]
    if not found:
        found = list(route_people)
    return list(dict.fromkeys(found))


def chat_people(cfg):
    """chat -> requesters of the route that holds it."""
    return {chat: r['requesters'] for r in cfg['routes'] for chat in r['chats']}


def timeline(app, key, since, until=None, github=True):
    """Every message to or from the project's people between since and until, oldest first.
    Each item: ts, platform, direction ('in' from them, 'out' from us), people, chat, text, links, topics."""
    cfg, ledger = app.cfg, app.cfg['projects'][key]['ledger']
    members, routes = set(ledger['people']), chat_people(cfg)
    thread = cfg['projects'][key]['thread']
    until = until if until is not None else app.fx.now() + 1
    items = []
    for row in sends(app, thread, since, until):
        people = row['people'] if row['people'] is not None else addressed(cfg, row['to_chat'], row['text'], routes.get(row['to_chat'], ()))
        who = [p for p in people if p in members]
        if who:
            items.append(item(ledger, row['created_at'], 'teams' if row['source'] == 'teams-bot' else row['source'], 'out',
                              who, row['to_chat'], row['text'], ref=f'outbox:{row["id"]}'))
    marks = ','.join('?' for _ in members) or 'NULL'
    owned = [r['name'] for r in cfg['routes'] if r['thread'] == thread]
    route_marks = ','.join('?' for _ in owned) or 'NULL'
    # A message is this project's if one of its requests is with the thread, or it has none and its chat is the thread's.
    for row in app.db.execute(f'SELECT m.id,m.source,m.chat,m.sender_key,m.text,m.ts FROM messages m WHERE m.sender_key IN ({marks}) '
                              'AND COALESCE(m.is_owner,0)=0 AND COALESCE(m.deleted,0)=0 AND m.ts>=? AND m.ts<? '
                              'AND (EXISTS (SELECT 1 FROM requests r WHERE r.message_id=m.id AND r.thread=?) OR '
                              f'(NOT EXISTS (SELECT 1 FROM requests r WHERE r.message_id=m.id) AND m.route IN ({route_marks}))) '
                              'ORDER BY m.ts,m.id', (*sorted(members), since, until, thread, *owned)):
        items.append(item(ledger, row['ts'], row['source'], 'in', [row['sender_key']], row['chat'], row['text'],
                          ref=f'message:{row["id"]}'))
    if github and ledger['github']:
        logins = {login: k for k, p in cfg['people'].items() for login in p.get('github', [])}
        repos = ','.join('?' for _ in ledger['github'])
        for row in app.db.execute(f'SELECT * FROM ledger_github WHERE repo IN ({repos}) AND created_at>=? AND created_at<? '
                                  'ORDER BY created_at,id', (*ledger['github'], since, until)):
            author = (row['author'] or '').lower()
            mentioned = [logins[m.lower()] for m in re.findall(r'(?<![\w/])@([A-Za-z0-9-]+)', row['body'] or '') if m.lower() in logins]
            who = [p for p in dict.fromkeys(([logins[author]] if author in logins else []) + mentioned) if p in members]
            out = author in ledger['self_github']
            if who or out:
                entry = item(ledger, row['created_at'], 'github', 'out' if out else 'in', who, f'{row["repo"]}#{row["number"]}',
                             row['body'], ref=row['url'])
                issue = f'issue:{row["number"]}'
                if issue not in entry['topics']:
                    entry['topics'].append(issue)
                items.append(entry)
    items.sort(key=lambda i: i['ts'])
    return items


def sends(app, thread, since, until):
    """The thread's sends created in [since, until). The same person can work on several projects, so only
    the project's thread counts: a send's thread is its request's, or for a plain `toucan send` the one
    recorded when it was queued."""
    queued = {row[0]: (row[1], row[2]) for row in app.db.execute(
        "SELECT CAST(json_extract(detail_json,'$.outbox') AS INTEGER),thread,json_extract(detail_json,'$.people') FROM events "
        "WHERE kind='outbox_pending' AND ts>=? AND ts<?", (since - 60, until))}
    rows = app.db.execute("SELECT o.id,o.source,o.to_chat,o.text,o.created_at,r.thread FROM outbox o "
                          "LEFT JOIN requests r ON r.id=o.request_id WHERE o.kind='send' AND o.state!='failed' "
                          'AND o.created_at>=? AND o.created_at<? ORDER BY o.created_at,o.id', (since, until))
    found = []
    for row in map(dict, rows):
        recorded_thread, people = queued.get(row['id'], (None, None))
        if (row['thread'] or recorded_thread) == thread:
            # WhatsApp turns tags into names before storing, so who it was for is taken from queue time.
            row['people'] = json.loads(people) if people else None
            found.append(row)
    return found


def item(ledger, ts, platform, direction, people, chat, text, ref):
    text = text or ''
    return dict(ts=ts, platform=platform, direction=direction, people=list(people), chat=chat, text=text,
                links=URL.findall(text), topics=topics(ledger, text), ref=ref)


# ---- preflight --------------------------------------------------------------------------------------

def is_review(ledger, text, links):
    hosts = [host(u) for u in links]
    words = any(w in (text or '').lower() for w in ledger['review_words'])
    return any(on(h, ledger['review_hosts']) for h in hosts) or (words and any(not on(h, PLAIN_HOSTS) for h in hosts))


def evaluate(app, key, chat, text, now, route_people=(), until=None):
    """Reasons to hold a send to `chat` with `text` at `now`, judged on the timeline up to `now`
    (before `until` when replaying a stored send, so it is not counted against itself).
    Returns None when the send is not for any of the project's people."""
    cfg, ledger = app.cfg, app.cfg['projects'][key]['ledger']
    who = [p for p in addressed(cfg, chat, text, route_people) if p in ledger['people']]
    if not who:
        return None
    names = lambda keys: ', '.join(cfg['people'][k]['name'] for k in keys)
    links, subjects = URL.findall(text or ''), topics(ledger, text)
    reasons = []
    review = is_review(ledger, text, links)
    if review and any(w in (text or '').lower() for w in ledger['review_words']):
        odd = sorted({host(u) for u in links if not on(host(u), ledger['review_hosts'] + list(PLAIN_HOSTS))})
        if odd:
            allowed = ', '.join(ledger['review_hosts']) or 'none configured'
            reasons.append(dict(rule='review_link', text=f'review link on {", ".join(odd)}, which is not a review host ({allowed})'))
    pel = ledger['pel']
    past = timeline(app, key, now - LOOKBACK_DAYS * 86400, now + 1 if until is None else until, github=False)
    if review and pel and pel not in who:
        shared = lambda i: set(i['topics']) & set(subjects) or set(i['links']) & set(links)
        sent = [i for i in past if i['direction'] == 'out' and pel in i['people'] and is_review(ledger, i['text'], i['links']) and shared(i)]
        if not sent:
            reasons.append(dict(rule='pel_first', text=f'review material for {names(who)} has not gone to {names([pel])} '
                                                      f'(the PEL) first for {", ".join(subjects) or "these links"}'))
        else:
            first = sent[-1]['ts']
            answered = any(i['direction'] == 'in' and pel in i['people'] and i['ts'] > first for i in past)
            waited = (now - first) / 60
            if not answered and waited < ledger['pel_wait_minutes']:
                reasons.append(dict(rule='pel_first', text=f'{names([pel])} got it {int(waited)} minutes ago and has not replied; '
                                                          f'wait {ledger["pel_wait_minutes"]} minutes or for a reply'))
    # Revising material with the PEL is the review loop working, so only other people count.
    others = [p for p in who if p != pel]
    if others and any(re.search(p, text or '') for p in ledger['supersede']):
        reasons.append(dict(rule='supersede', text=f'replaces something {names(others)} already has; send it only if they need the new version'))
    for person in who:
        # A conversation is not a flood: only our sends since the person last wrote to us count.
        heard = max([i['ts'] for i in past if i['direction'] == 'in' and person in i['people']], default=0)
        count = sum(1 for i in past if i['direction'] == 'out' and person in i['people'] and i['ts'] >= now - 3600 and i['ts'] > heard)
        if count >= ledger['burst_per_hour']:
            reasons.append(dict(rule='burst', text=f'{names([person])} already got {count} messages from Toucan in the last hour '
                                                   'without writing back; combine this with the next one'))
    return dict(project=key, people=who, topics=subjects, review=review,
                verdict='hold' if reasons else 'pass', reasons=reasons)


def preflight(app, thread, chat, text, override=None):
    """Run before a send is queued. Returns the checks to record; raises Refused in enforce mode."""
    results = []
    routes = chat_people(app.cfg).get(chat, ())
    for key, project in projects(app.cfg, thread).items():
        try:
            result = evaluate(app, key, chat, text, app.fx.now(), routes)
        except Exception as error:  # never block a send because the check broke
            results.append(dict(project=key, verdict='error', error=type(error).__name__, mode=project['ledger']['mode']))
            continue
        if result:
            result['mode'] = project['ledger']['mode']
            results.append(result)
    for result in results:
        result['override'] = (override or '').strip() or None
    held = [r for r in results if r['verdict'] == 'hold' and r['mode'] == 'enforce']
    if held and not (override or '').strip():
        lines = [f'- {reason["text"]}' for r in held for reason in r['reasons']]
        raise Held('Preflight held this send:\n' + '\n'.join(lines) +
                   '\nFix the message, or send it anyway with --override "why it must go now".',
                   [dict(r, refused=True) for r in results])
    return results


# ---- GitHub collector -------------------------------------------------------------------------------

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def github_sync(app, force=False):
    """Copy new and edited GitHub issues and comments for ledger projects. Each page and its cursor
    commit together, so an interrupted run repeats a page at worst; rows are keyed, so repeats are harmless."""
    repos = sorted({r for p in projects(app.cfg).values() for r in p['ledger']['github']})
    if not repos or app.dry:
        return dict(skipped='no repositories' if not repos else 'dry run')
    now = app.fx.now()
    every = min(p['ledger']['github_minutes'] for p in projects(app.cfg).values() if p['ledger']['github']) * 60
    last = app.db.execute("SELECT value FROM meta WHERE key='ledger-github:last'").fetchone()
    if not force and last and now - float(last[0]) < every:
        return dict(skipped='ran recently')
    # Mark the run before reading, so a run killed mid-way still waits its turn instead of retrying every sync.
    with app.transaction():
        app.db.execute("INSERT OR REPLACE INTO meta VALUES ('ledger-github:last',?)", (str(now),))
    report = dict(stored=0, errors=[])
    for repo in repos:
        for kind, path in GITHUB_KINDS.items():
            if app.fx.now() - now > GITHUB_BUDGET_SECONDS:
                report['errors'].append(dict(repo=repo, kind=kind, error='time budget spent; continues next run'))
                continue
            try:
                report['stored'] += github_kind(app, repo, kind, path, f'ledger-github:{repo}:{kind}', now)
            except Exception as error:
                report['errors'].append(dict(repo=repo, kind=kind, error=type(error).__name__ if not isinstance(error, RuntimeError) else str(error)))
    with app.transaction():
        if report['errors']:
            app.record('ledger_github_error', errors=report['errors'])
        else:
            app.db.execute("INSERT OR REPLACE INTO meta VALUES ('ledger-github:ok',?)", (str(now),))
    return report


def github_kind(app, repo, kind, path, cursor_key, started):
    """Read items updated since the cursor, oldest first, asking again from the new cursor after each full
    page. Page numbers would skip items that move to the end when they are edited between two requests."""
    stored = 0
    for _ in range(GITHUB_PAGES):
        row = app.db.execute('SELECT value FROM meta WHERE key=?', (cursor_key,)).fetchone()
        since = row[0] if row else iso(started - LOOKBACK_DAYS * 86400)
        argv = ['gh', 'api', '-X', 'GET', f'repos/{repo}/{path}sort=updated&direction=asc&per_page=100&since={since}']
        result = local_run(app.fx, argv, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f'gh api failed for {repo} {kind} (exit {result.returncode})')
        entries = json.loads(result.stdout or '[]')
        if not isinstance(entries, list):
            raise RuntimeError(f'gh api returned no list for {repo} {kind}')
        newest = None
        with app.transaction():
            for entry in entries:
                if kind == 'issue':
                    number = entry['number']
                    body = f'{entry.get("title") or ""}\n\n{entry.get("body") or ""}'.strip()
                else:
                    url = entry.get('issue_url') or entry.get('pull_request_url') or ''
                    number = int(url.rstrip('/').rsplit('/', 1)[-1])
                    body = entry.get('body') or ''
                app.db.execute('INSERT INTO ledger_github (repo,kind,gh_id,number,author,created_at,updated_at,body,url) '
                               'VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(repo,kind,gh_id) DO UPDATE SET '
                               'author=excluded.author,updated_at=excluded.updated_at,body=excluded.body,url=excluded.url',
                               (repo, kind, str(entry['id']), number, (entry.get('user') or {}).get('login', ''),
                                parse_time(entry['created_at']), parse_time(entry['updated_at']), body[:20000], entry.get('html_url', '')))
                stored += 1
                newest = max(newest or entry['updated_at'], entry['updated_at'])
            if newest:
                app.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (cursor_key, newest))
        # A short page is the end. A full page that didn't move the cursor (100 items edited in the same second)
        # can't be read further this way; stop rather than loop.
        if len(entries) < 100 or newest is None or newest <= since:
            if len(entries) >= 100:
                raise RuntimeError(f'more than 100 {kind} items in {repo} share one update time; some were not read')
            break
    return stored


# ---- views ------------------------------------------------------------------------------------------

def show(app, key, since, person=None, topic=None):
    if key not in projects(app.cfg):
        raise ValidationError('unknown project, or its ledger is off')
    items = timeline(app, key, since)
    if person:
        items = [i for i in items if person in i['people']]
    if topic:
        topic = topic.lower()
        items = [i for i in items if any(t == topic or t.split(':', 1)[1] == topic for t in i['topics'])]
    return [dict(i, at=app.local(i['ts'])) for i in items]


def replay(app, key, since):
    """Run Preflight on each past send as it stood just before it went out."""
    if key not in projects(app.cfg):
        raise ValidationError('unknown project, or its ledger is off')
    routes = chat_people(app.cfg)
    held, checked = [], 0
    for row in sends(app, app.cfg['projects'][key]['thread'], since, app.fx.now() + 1):
        # Judge who it was for as Preflight did: from the people recorded at queue time when there are any.
        tags = ' '.join(f'@[{p}]' for p in row['people'] or ())
        result = evaluate(app, key, row['to_chat'], f'{row["text"] or ""}\n{tags}', row['created_at'],
                          routes.get(row['to_chat'], ()), until=row['created_at'])
        if not result:
            continue
        checked += 1
        if result['verdict'] == 'hold':
            held.append(dict(outbox=row['id'], at=app.local(row['created_at']), people=result['people'], topics=result['topics'],
                             reasons=[r['text'] for r in result['reasons']], rules=[r['rule'] for r in result['reasons']],
                             text=(row['text'] or '')[:200]))
    return dict(project=key, checked=checked, held=len(held), sends=held)


def status(app):
    meta = dict(app.db.execute("SELECT key,value FROM meta WHERE key IN ('ledger-github:last','ledger-github:ok')").fetchall())
    counts = app.rows("SELECT json_extract(detail_json,'$.project') AS project,json_extract(detail_json,'$.verdict') AS verdict,"
                      "count(*) AS count FROM events WHERE kind='preflight' AND ts>=? GROUP BY 1,2", (app.fx.now() - 7 * 86400,))
    return dict(projects={k: p['ledger']['mode'] for k, p in projects(app.cfg).items()},
                github_last_run=app.local(float(meta['ledger-github:last'])) if 'ledger-github:last' in meta else None,
                github_last_ok=app.local(float(meta['ledger-github:ok'])) if 'ledger-github:ok' in meta else None,
                github_items=app.db.execute('SELECT count(*) FROM ledger_github').fetchone()[0],
                preflight_7d=counts)


def recent(app, thread, hours=2):
    """One line per person the thread's ledger projects messaged in the last few hours, for the inbox."""
    lines = []
    for key in projects(app.cfg, thread):
        items = [i for i in timeline(app, key, app.fx.now() - hours * 3600, github=False) if i['direction'] == 'out']
        people = {}
        for i in items:
            for p in i['people']:
                entry = people.setdefault(p, dict(count=0, topics=[]))
                entry['count'] += 1
                entry['topics'] += [t for t in i['topics'] if t not in entry['topics']]
        for p, entry in people.items():
            about = f' about {", ".join(entry["topics"])}' if entry['topics'] else ''
            lines.append(f'{app.cfg["people"][p]["name"]}: {entry["count"]} message(s) from Toucan in the last {hours} hours{about}')
    return lines
