"""Read-only adapters for the wacli and Toucan bot stores."""
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from email.utils import getaddresses
from email.parser import BytesHeaderParser

from .config import timestamp
from .storage import readonly
from .t3 import hook_info, hook_thread, hook_file_thread, owner_said, rate_limited, thread_usage


@dataclass
class Paths:
    wa: Path = field(default_factory=lambda: Path.home() / '.local/state/wacli')
    bot_db: Path = field(default_factory=lambda: Path.home() / '.local/state/toucan-bot/bot.db')
    t3: Path = field(default_factory=lambda: Path.home() / '.t3/userdata/statev2.sqlite')
    media: Path = field(default_factory=lambda: Path.home() / '.local/state/toucan/media')
    wa_account: str = 'wa'
    wa_service: str = ''
    teams_service: str = ''
    notmuch_config: str = ''
    email_stale_minutes: float = 60
    teams_stale_minutes: float = 5

    @classmethod
    def from_config(cls, cfg):
        result = cls()
        result.media = Path(os.environ.get('TOUCAN_DB', '~/.local/state/toucan/toucan.db')).expanduser().parent / 'media'
        source = cfg.get('sources', {})
        wa, teams, mail = (source.get(k, {}) for k in ('whatsapp', 'teams', 'email'))
        result.wa = Path(wa.get('store', result.wa)).expanduser()
        result.bot_db = Path(teams.get('bot_db', result.bot_db)).expanduser()
        result.teams_stale_minutes = teams.get('stale_minutes', 5)
        result.t3 = Path(cfg.get('t3', {}).get('db', result.t3)).expanduser()
        result.wa_account = wa.get('account', result.wa_account)
        result.wa_service = wa.get('service_unit', '')
        result.teams_service = teams.get('service_unit', '')
        result.notmuch_config = mail.get('notmuch_config', '')
        result.email_stale_minutes = mail.get('stale_minutes', 60)
        return result


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'attachment', 'systemeventmessage'):
            self.skip += 1
        elif not self.skip:
            if tag in ('br', 'p', 'div', 'li', 'tr'):
                self.parts.append('\n')
            if tag == 'img':
                self.parts.append(dict(attrs).get('alt', '[image]'))
            if tag == 'a' and dict(attrs).get('href'):
                self.parts.append(dict(attrs)['href'] + ' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'attachment', 'systemeventmessage'):
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in ('p', 'div', 'li', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(body):
    if body.get('contentType', '').lower() != 'html':
        return body.get('content') or ''
    parser = Text()
    parser.feed(body.get('content') or '')
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def lid_map(paths):
    db = readonly(paths.wa / 'session.db')
    try:
        return {r['lid'].split('@')[0] + '@lid': r['pn'].split('@')[0] + '@s.whatsapp.net'
                for r in db.execute('SELECT lid,pn FROM whatsmeow_lid_map')}
    finally:
        db.close()


def person_for(cfg, source, raw, mapping):
    if source == 'email':
        return next((key for key, p in cfg['people'].items()
                     if raw and raw.lower() in [i.lower() for i in p.get('email', [])]), None)
    if source == 'teams' and raw:
        raw = raw.lower()
    canonical = mapping.get(raw, raw)
    for key, person in cfg['people'].items():
        ids = person.get('whatsapp', []) if source == 'whatsapp' else [person.get('teams_id')]
        if raw and (raw in ids or canonical in [mapping.get(i, i) for i in ids]):
            return key
    return None


def noise(m):
    """Reactions, and rows wacli could not decode: no text, media or deletion, only its '(message)' placeholder."""
    if m.get('reaction_emoji') or m.get('reaction_to_id'):
        return True
    return (not m.get('text') and m.get('display_text') == '(message)' and not m.get('media_type')
            and not m.get('media_caption') and not m.get('revoked') and not m.get('deleted_for_me'))


def whatsapp(paths, chat, since, tracked):
    db = readonly(paths.wa / 'wacli.db')
    try:
        # Revisit tracked open requests even when their creation is outside lookback.
        marks = ','.join('?' for _ in tracked) or 'NULL'
        rows = db.execute(f'SELECT * FROM messages WHERE chat_jid=? AND (ts>=? OR msg_id IN ({marks}))',
                          (chat, since, *tracked)).fetchall()
    finally:
        db.close()
    result = []
    for row in rows:
        m = dict(row)
        if noise(m):
            continue
        attachments = [{k: m[k] for k in ('media_type', 'filename', 'local_path') if m.get(k)}]
        result.append(dict(source='whatsapp', account=paths.wa_account, chat=chat, msg_id=m['msg_id'],
                           sender_raw=m['sender_jid'], sender_name=m['sender_name'],
                           is_owner=int(bool(m['from_me'])), ts=m['ts'],
                           text=m['text'] or m['display_text'] or m.get('media_caption') or '',
                           attachments_json=json.dumps([a for a in attachments if a]),
                           edited=int(bool(m['edited'])), deleted=int(bool(m['revoked'] or m.get('deleted_for_me')))))
    return result


def owner_chat(paths, chats, since):
    """The owner's 1:1 chat with the assistant number, oldest first, reactions and placeholders excluded."""
    db = readonly(paths.wa / 'wacli.db')
    try:
        marks = ','.join('?' for _ in chats)
        rows = db.execute(f'SELECT * FROM messages WHERE chat_jid IN ({marks}) AND ts>=? ORDER BY ts,rowid',
                          (*chats, since)).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows if not noise(dict(r))]


def voice_notes(paths, chats, since):
    """Voice notes other people sent in these chats since `since`, oldest first."""
    db = readonly(paths.wa / 'wacli.db')
    try:
        marks = ','.join('?' for _ in chats) or 'NULL'
        rows = db.execute(f"SELECT chat_jid,msg_id,sender_jid,local_path FROM messages WHERE chat_jid IN ({marks}) "
                          "AND media_type='audio' AND from_me=0 AND revoked=0 AND ts>=? ORDER BY ts,rowid",
                          (*chats, since)).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]


def teams_store(paths):
    """Probe the read contract without importing or depending on the bot package."""
    db = readonly(paths.bot_db)
    try:
        row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not row or row['value'] != '4':
            raise ValueError('Teams bot store schema unsupported; restart or upgrade the bot')
        db.execute('SELECT id,type,name FROM conversations LIMIT 0')
        db.execute('SELECT conversation_id,id,ts,sender_aad_id,sender_name,text,attachments_json,edited_at,deleted,sender_app_id,live_seen_at,graph_seen_at,first_source,change_seq,mentions_json FROM messages LIMIT 0')
        progress = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('store_generation','change_sequence')"))
        if not progress.get('store_generation') or int(progress.get('change_sequence', '-1')) < 0:
            raise ValueError('Teams bot change tracking unavailable; restart or upgrade the bot')
        row = db.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone()
        return row['value'] if row else ''
    finally:
        db.close()


def teams_coverage(paths, at):
    """Read catch-up health without loading bot code or making any Microsoft requests."""
    teams_store(paths)
    db = readonly(paths.bot_db)
    try:
        interval = db.execute("SELECT value FROM meta WHERE key='catchup_interval_seconds'").fetchone()
        interval = int(interval['value']) if interval else 120
        rows = db.execute("""SELECT c.id,s.last_success,s.last_error,s.http_status
                             FROM conversations c LEFT JOIN catchup s ON c.id=s.conversation_id
                             WHERE c.type IS NULL OR c.type!='personal'""").fetchall()
        chats = [dict(r) | {'age': at - timestamp(r['last_success']) if r['last_success'] else None} for r in rows]
        missing = db.execute("""SELECT count(*) FROM messages WHERE first_source='graph' AND live_seen_at IS NULL
                                AND sender_app_id IS NULL AND mentioned=0
                                AND julianday(ts)>=julianday(?, 'unixepoch','-1 day')
                                AND julianday(graph_seen_at)<=julianday(?, 'unixepoch','-5 minutes')""", (at, at)).fetchone()[0]
        tasks = {r['key'].removeprefix('task:'): json.loads(r['value'])
                 for r in db.execute("SELECT key,value FROM meta WHERE key LIKE 'task:%'")}
        roster_errors = {r['key'].removeprefix('roster_error:'): r['value']
                         for r in db.execute("SELECT key,value FROM meta WHERE key LIKE 'roster_error:%'")}
        return dict(chats=chats, interval_seconds=interval, ordinary_graph_only=missing,
                    background=tasks, roster_errors=roster_errors)
    finally:
        db.close()


def mentions(items, owner):
    """Who a Teams message tags: a bot (Toucan), the owner, the whole chat, or another person.
    A user the bot could not identify, or any user while the owner's ID is unset, stays 'unknown'."""
    owner = (owner or '').lower()
    return [dict(name=item.get('text'), who='bot' if item.get('kind') == 'application' else
                 'everyone' if item.get('kind') in ('conversation', 'tag') else
                 'owner' if owner and item.get('id') == owner else
                 'person' if owner and item.get('id') else 'unknown') for item in items]


def teams_catchup(paths, routes, at, strict=False):
    """Catch-up problems per routed Teams chat, read from the bot store; unrouted chats never count.

    A fresh heartbeat does not prove ordinary messages arrive: without a working catch-up and
    consent in the chat, only @mentions of the bot do. Authorization failures report at once,
    other failures once a chat has gone three intervals without a success. Unless strict, a chat
    that has never synced waits until a catch-up pass ends after the bot first saw it, or until
    three intervals after the later of bot start and first sight, whichever comes first; a
    catch-up task that keeps failing before any chat result is then reported too."""
    teams_store(paths)
    db = readonly(paths.bot_db)
    try:
        interval = db.execute("SELECT value FROM meta WHERE key='catchup_interval_seconds'").fetchone()
        limit = max(300, int(interval['value']) * 3 if interval else 360)
        task = db.execute("SELECT value FROM meta WHERE key='task:catchup'").fetchone()
        task = json.loads(task['value']) if task else {}
        passed = timestamp(task['last_success']) if task.get('last_success') else 0
        started = db.execute("SELECT value FROM meta WHERE key='started_at'").fetchone()
        try:
            started = timestamp(started['value']) if started else 0
        except ValueError:
            started = 0
        problems = {}
        for route in routes:
            if route['source'] != 'teams':
                continue
            found = []
            for chat in route['chats']:
                row = db.execute('SELECT c.type,c.first_seen,s.last_success,s.last_error,s.http_status FROM conversations c '
                                 'LEFT JOIN catchup s ON s.conversation_id=c.id WHERE c.id=?', (chat,)).fetchone()
                if not row:
                    found.append('the Toucan bot has not seen this Teams chat; add the Toucan app to it')
                    continue
                if row['type'] == 'personal':
                    continue  # the bot's own 1:1 chats get every message by push
                error, age = row['last_error'], at - timestamp(row['last_success']) if row['last_success'] else None
                try:
                    seen = timestamp(row['first_seen'])
                except ValueError:
                    seen = 0
                reached = passed > seen if seen else bool(passed)
                # Grace for a first pass after start, bounded so a failing catch-up still shows.
                overdue = bool(started or seen) and at - max(started, seen) > limit
                if not error and task.get('last_error'):
                    error = 'catch-up task failing: ' + task['last_error']
                if row['http_status'] == 403:
                    found.append('Teams catch-up is not allowed in this chat (HTTP 403), so Toucan only sees '
                                 'messages that @mention it. Upgrade the Toucan Teams app to 1.1.0 or later and '
                                 'accept its permissions in this chat')
                elif row['http_status'] == 401:
                    found.append('Teams catch-up was refused (HTTP 401); check the bot credentials and run '
                                 '`toucan-bot status`')
                elif age is not None and (age > limit or age < -60):
                    found.append(f'Teams catch-up last succeeded {age / 60:.0f} min ago'
                                 f'{" (" + error + ")" if error else ""}; messages that do not @mention Toucan '
                                 'may be missing. Run `toucan-bot status`')
                elif age is None and (strict or reached or overdue):
                    found.append(f'Teams catch-up has never succeeded in this chat{" (" + error + ")" if error else ""}; '
                                 'messages that do not @mention Toucan may be missing. Run `toucan-bot status`')
            if found:
                where = f' ({len(found)} of {len(route["chats"])} chats)' if len(route['chats']) > 1 else ''
                problems['teams:catchup:' + route['name']] = (route.get('label') or route['name']) + where + ': ' + found[0]
        return problems
    finally:
        db.close()


def teams(paths, chat, since, tracked, owner_teams_id=''):
    return teams_batch(paths, chat, since, tracked, owner_teams_id)[0]


def teams_batch(paths, chat, since, tracked, owner_teams_id='', after=None):
    """Return messages and durable change progress from the same read-only snapshot."""
    teams_store(paths)
    db = readonly(paths.bot_db)
    try:
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('store_generation','change_sequence')"))
        latest = dict(generation=meta['store_generation'], sequence=int(meta['change_sequence']))
        if after is not None:
            sequence = (after['sequence'] if after.get('generation') == latest['generation'] and
                        after['sequence'] <= latest['sequence'] else 0)
            rows = db.execute('SELECT * FROM messages WHERE conversation_id=? AND change_seq>?', (chat, sequence)).fetchall()
        else:
            # Explicit history imports still use the operator's requested time range.
            marks = ','.join('?' for _ in tracked) or 'NULL'
            rows = db.execute(f"SELECT * FROM messages WHERE conversation_id=? AND "
                              f"(julianday(ts)>=julianday(?, 'unixepoch') OR id IN ({marks}))",
                              (chat, since, *tracked)).fetchall()
        result = []
        for row in rows:
            m = dict(row)
            quotes = []
            for entity in json.loads(m.get('quoted_json') or '[]'):
                if entity.get('type') == 'quotedReply':
                    quote = entity.get('quotedReply', {})
                    if quote.get('preview'):
                        quotes.append(html_text(dict(contentType='html', content=quote['preview'])))
            for attachment in json.loads(m['attachments_json']):
                if attachment.get('contentType') in ('messageReference', 'forwardedMessageReference'):
                    reference = attachment.get('content') or {}
                    if isinstance(reference, str):
                        try:
                            reference = json.loads(reference)
                        except ValueError:
                            continue
                    if isinstance(reference, dict):
                        preview = reference.get('messagePreview') or reference.get('originalMessageContent')
                        if preview:
                            quotes.append(html_text(dict(contentType='html', content=preview)))
            if not quotes and m.get('reply_to_id'):
                quoted = db.execute('SELECT text FROM messages WHERE conversation_id=? AND id=?',
                                    (chat, m['reply_to_id'])).fetchone()
                if quoted and quoted['text']:
                    quotes.append(quoted['text'])
            text = m['text']
            for quote in quotes:
                text += '\n\nQuote:\n' + '\n'.join('> ' + line for line in quote.splitlines())
            sender = (m['sender_aad_id'] or '').lower() or None
            if m.get('sender_app_id'):
                sender = None
                text = '[Application context: ' + (m['sender_name'] or m['sender_app_id']) + ']\n' + text
            attachments = json.loads(m['attachments_json'])
            for attachment in attachments:
                kind = str(attachment.get('contentType', ''))
                if attachment.get('contentUrl') and not kind.startswith(('image/', 'text/html', 'application/vnd.microsoft.card.')):
                    attachment['access_note'] = "The bot can't download files. The owner's own Microsoft login is needed."
            result.append(dict(source='teams', account='teams', chat=chat, msg_id=m['id'],
                               sender_raw=sender, sender_name=m['sender_name'],
                               is_owner=int(bool(owner_teams_id) and sender == owner_teams_id.lower()),
                               # Zero marks unknown creation; sync/import must keep it context-only.
                               ts=timestamp(m['ts']) if m['ts'] else 0, text=text,
                               attachments_json=json.dumps(attachments),
                               # None: the bot has no mention data for this row; keep what was stored.
                               mentions_json=None if m['mentions_json'] is None else
                               json.dumps(mentions(json.loads(m['mentions_json']), owner_teams_id)),
                               edited=int(bool(m['edited_at'])), deleted=int(bool(m['deleted']))))
        return result, latest
    finally:
        db.close()


def health(paths, effects, now, sources, whatsapp_stale_minutes=120):
    problems = {}
    for source in sources:
        if source == 'email':
            try:
                newest = email_newest(paths) if shutil.which('notmuch') else None
            except (OSError, ValueError, subprocess.SubprocessError):
                newest = None
            if newest is None or now - newest > paths.email_stale_minutes * 60:
                problems['health:email:stale'] = 'Email index unavailable or stale; check notmuch'
            continue
        unit = paths.wa_service if source == 'whatsapp' else paths.teams_service
        if unit:
            try:
                result = effects.run(['systemctl', '--user', 'is-active', unit])
                if result.returncode:
                    problems[f'health:{source}:service'] = f'{source} service is unhealthy'
            except (OSError, subprocess.SubprocessError, TimeoutError):
                problems[f'health:{source}:service'] = f'{source} service cannot be checked'
        try:
            newest = (paths.wa / 'HEARTBEAT').stat().st_mtime if source == 'whatsapp' else timestamp(teams_store(paths))
        except (OSError, ValueError, sqlite3.Error):
            newest = 0
        limit = whatsapp_stale_minutes if source == 'whatsapp' else paths.teams_stale_minutes
        if not newest or now - newest > limit * 60 or newest > now + 60:
            problems[f'health:{source}:stale'] = f'{source} heartbeat unavailable or stale'
    return problems


def notmuch(paths, *args):
    env = os.environ.copy()
    if paths.notmuch_config:
        env['NOTMUCH_CONFIG'] = str(Path(paths.notmuch_config).expanduser())
    try:
        result = subprocess.run(['notmuch', *args], capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('notmuch unavailable; check the email index and configuration') from None
    if result.returncode:
        raise ValueError('notmuch failed; check the email index and configuration')
    return json.loads(result.stdout)


def email_newest(paths):
    rows = notmuch(paths, 'search', '--format=json', '--output=summary', '--sort=newest-first', '--limit=1', '*')
    return float(rows[0]['timestamp']) if rows else None


def mail_term(value):
    return '"' + value.replace('"', '""') + '"'


def mail_messages(value):
    if isinstance(value, list):
        for entry in value:
            yield from mail_messages(entry)
    elif isinstance(value, dict) and 'headers' in value and 'id' in value:
        yield value


def mail_body(parts):
    plain, html = [], []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind, content = part.get('content-type', '').lower(), part.get('content')
        if isinstance(content, list):
            text, markup = mail_body(content)
            plain.extend(text)
            html.extend(markup)
        elif isinstance(content, str) and part.get('content-disposition', '').split(';')[0].strip().lower() != 'attachment':
            if kind == 'text/plain':
                plain.append(content)
            elif kind == 'text/html':
                html.append(html_text(dict(contentType='html', content=content)))
    return plain, html


def email(paths, chat, since, tracked, route=None, cfg=None):
    allowed = {address.lower() for key in (route or {}).get('requesters', [])
               for address in (cfg or {}).get('people', {}).get(key, {}).get('email', [])}
    if not allowed:
        return []
    recipients_allowed = {address.lower() for address in ([chat] if isinstance(chat, str) else chat)}
    # The bare address also covers indexed delivery headers; headers are checked again below.
    to = '(' + ' or '.join('to:' + mail_term(address) for address in sorted(recipients_allowed)) + ')'
    addresses = '(' + to + ' or ' + ' or '.join(mail_term(address) for address in sorted(recipients_allowed)) + ')'
    senders = '(' + ' or '.join('from:' + mail_term(address) for address in sorted(allowed)) + ')'
    dates = f'date:@{max(0, int(since))}..'
    if tracked:
        dates = '(' + dates + ' or ' + ' or '.join('id:' + mail_term(mid) for mid in tracked) + ')'
    ids = notmuch(paths, 'search', '--format=json', '--output=messages', f'{dates} and {addresses} and {senders}')
    # Delivered-To is not indexed by default, so inspect other mail from approved senders too.
    ids += notmuch(paths, 'search', '--format=json', '--output=messages',
                   f'{dates} and {senders} and not {to}')
    result, seen = [], set()
    for mid in dict.fromkeys(ids):
        mid = mid.removeprefix('id:')
        shown = notmuch(paths, 'show', '--format=json', '--entire-thread=false', '--body=true', 'id:' + mail_term(mid))
        for message in mail_messages(shown):
            if not message.get('match', True) or message['id'] in seen:
                continue
            headers = {key.lower(): str(value) for key, value in message['headers'].items()}
            sender = getaddresses([headers.get('from', '')])
            if len(sender) != 1 or sender[0][1].lower() not in allowed:
                continue
            recipients = {address.lower() for _, address in getaddresses(
                [headers[key] for key in ('to', 'cc', 'delivered-to') if headers.get(key)])}
            if not recipients_allowed & recipients:
                filenames = message.get('filename', [])
                for filename in [filenames] if isinstance(filenames, str) else filenames:
                    try:
                        with Path(filename).open('rb') as archive:
                            raw = BytesHeaderParser().parsebytes(archive.read(131072))
                        recipients.update(address.lower() for _, address in getaddresses(
                            [str(value) for key in ('To', 'Cc', 'Delivered-To') for value in raw.get_all(key, [])]))
                    except OSError:
                        continue
                if not recipients_allowed & recipients:
                    continue
            ts = float(message['timestamp'])
            if ts < since and message['id'] not in tracked:
                continue
            plain, html = mail_body(message.get('body', []))
            text = 'Subject: ' + headers.get('subject', '') + '\n\n' + '\n\n'.join(plain or html)
            if len(text) > 32000:
                text = text[:31980] + '\n[truncated]'
            seen.add(message['id'])
            result.append(dict(source='email', account='email', chat=min(recipients_allowed & recipients), msg_id=message['id'],
                               sender_raw=sender[0][1].lower(), sender_name=sender[0][0], is_owner=0,
                               ts=ts, text=text, attachments_json='[]', edited=0, deleted=0))
    return result
