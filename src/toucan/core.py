"""Request lifecycle and delivery. Every network or process effect is injectable."""
import hashlib
import json
import re
import shlex
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import cards, ledger, router, sources, t3
from .config import GROUP, PHONE, Refused, ValidationError, hook_warning, prefixed, reply_phone, timestamp
from .effects import Effects
from .storage import account_warning, event, insert, private_dir

OPEN = "('new','claimed','done')"
ACTIVE = "('new','held','claimed','done')"  # OPEN plus requests the live router holds: their messages are still tracked
WAITER_FRESH = 30  # seconds; `toucan wait` beats every 5
VOICE_TRIES = 3      # syncs that try one voice note before its request goes ahead without a transcript
VOICE_PER_SYNC = 5
VOICE_BUDGET = 60    # seconds; no new note starts after this, so a sync stays well inside its 5-minute timeout


# Download state the bot or wacli adds to an attachment; it is not an edit of the message.
DOWNLOAD_FIELDS = ('local_path', 'download_status', 'http_status', 'download_error', 'retry_at')


# `@[person]` in a reply; the Teams bot posts a mention, other transports get the name.
TAG = re.compile(r'@\[([^\[\]]*)\]')
PING = re.compile(r'\s*ping[\s.!?]*', re.I)
PING_WINDOW = 600  # a ping older than this when first seen gets no pong
ANSWER_FOLLOWUP_SECONDS = 600  # an unquoted owner message this soon after a card's answer still adds to it


def still_image(path):
    """A PNG, JPEG, GIF or WebP by its magic bytes. The bot rejects animations and unreadable images."""
    with open(path, 'rb') as f:
        head = f.read(12)
    return (head.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a')) or
            head[:4] == b'RIFF' and head[8:12] == b'WEBP')


def attachment_content(value):
    return [{k: v for k, v in a.items() if k not in DOWNLOAD_FIELDS} for a in json.loads(value or '[]')]


def local(ts, zone='UTC'):
    return datetime.fromtimestamp(ts, ZoneInfo(zone)).strftime('%Y-%m-%d %H:%M %Z')


class App:
    def __init__(self, cfg, db, effects=None, paths=None, dry=False, shadow=False):
        self.cfg, self.db, self.shadow = cfg, db, shadow
        self.fx, self.paths, self.dry = effects or Effects(), paths or sources.Paths.from_config(cfg), dry
        self.settings = cfg['settings']
        self.local = lambda ts: local(ts, self.settings['timezone'])
        self.queued = []  # owner notices queued inside a sync transaction, sent after it commits
        self.limit_until = {}  # thread key -> when its provider said the rate limit ends, from the last sync

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @contextmanager
    def preflight_log(self):
        """Record a send Preflight refused. Its transaction has rolled back by now, so this uses a new one."""
        try:
            yield
        except ledger.Held as held:
            with self.transaction():
                for check in held.checks:
                    self.record('preflight', held.thread, held.request, outbox=None, **check)
            raise

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def record(self, kind, thread=None, request=None, **detail):
        event(self.db, self.fx.now(), kind, thread, request, **detail)

    def live_thread(self, key):
        thread = self.cfg['threads'][key]
        return sources.hook_file_thread(self.paths, thread['hook_file']) or thread.get('t3_thread')

    def rotation(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', ('rotate:' + key,)).fetchone()
        return json.loads(row['value']) if row else None

    def thread_status(self, key):
        live = self.live_thread(key)
        return dict(t3_thread=live, context=sources.thread_usage(self.paths, live), rotation=self.rotation(key))

    def reconcile_alerts(self, problems):
        for key, text in problems.items():
            if self.db.execute('SELECT 1 FROM alerts WHERE key=?', (key,)).fetchone():
                continue
            insert(self.db, 'alerts', key=key, text=text, since=self.fx.now())
            self.record('alert', key=key)
            if not self.dry and not self.shadow and self.settings['alert_url']:
                try:
                    self.fx.post(self.settings['alert_url'], text.encode(), {'Title': 'Toucan needs attention'})
                except Exception:
                    self.record('notification_failed', key=key)  # never log exception URLs
        for row in self.db.execute('SELECT key FROM alerts').fetchall():
            if row['key'] not in problems:
                self.db.execute('DELETE FROM alerts WHERE key=?', (row['key'],))
                self.record('alert_recovered', key=row['key'])

    def create_request(self, m, route, mapping):
        person = self.cfg['people'][m['sender_key']]
        reply_chat = m['chat']
        if route.get('reply_to'):
            reply_chat = reply_phone(self.cfg, route)
        elif m['source'] == 'whatsapp' and not reply_chat.endswith('@g.us'):
            ids = [mapping.get(i, i) for i in person.get('whatsapp', [])]
            reply_chat = next((i for i in ids if PHONE.fullmatch(i)), mapping.get(m['sender_raw'], m['sender_raw']))
        req = insert(self.db, 'requests', message_id=m['id'], route=route['name'], thread=route['thread'],
                     state='new', reply_source=m['source'], reply_chat=reply_chat,
                     reply_person=m['sender_key'], mode=route['mode'], created_at=self.fx.now())
        self.record('request_created', route['thread'], req)
        if router.is_live(route):
            router.place(self, req, route)
        return req

    def new_request(self, m, route, mapping, report, route_result):
        """A request for m, unless m answers the router's question to its sender (then it is context)."""
        if router.answer(self, m, route) is not None:
            return
        req = self.create_request(m, route, mapping)
        report['created'].append(req)
        route_result['created'].append(dict(request=req, msg_id=m['msg_id'], chat=m['chat'], person=m['sender_key']))

    def owner_chats(self, mapping):
        owner = self.settings['owner_whatsapp']
        return [owner, *(lid for lid, pn in mapping.items() if pn == owner)] if PHONE.fullmatch(owner) else []

    def transcribe(self, now, lookback):
        """Download and transcribe voice notes from requesters and the owner. Runs before the sync
        transaction because each note takes seconds; results are kept per message, so later syncs
        see the same text. Returns how many notes it tried."""
        command = shlex.split(self.settings['transcribe'])
        if not command or self.dry:
            return 0
        try:
            mapping = sources.lid_map(self.paths)
        except (OSError, sqlite3.Error):
            return 0
        senders = {}
        for route in self.cfg['routes']:
            if route['source'] == 'whatsapp':
                ids = {mapping.get(i, i) for p in route['requesters'] for i in self.cfg['people'][p].get('whatsapp', [])}
                for chat in route['chats']:
                    senders.setdefault(chat, set()).update(ids)
        for chat in self.owner_chats(mapping):
            senders.setdefault(chat, set()).add(self.settings['owner_whatsapp'])
        try:
            notes = sources.voice_notes(self.paths, sorted(senders), now - lookback * 3600)
        except (OSError, sqlite3.Error):
            return 0
        tried = 0
        for note in notes:
            if tried >= VOICE_PER_SYNC or self.fx.now() - now > VOICE_BUDGET:
                break
            if mapping.get(note['sender_jid'], note['sender_jid']) not in senders[note['chat_jid']]:
                continue
            key = note['chat_jid'], note['msg_id']
            row = self.db.execute('SELECT * FROM transcripts WHERE chat=? AND msg_id=?', key).fetchone()
            if row and (row['text'] or row['attempts'] >= VOICE_TRIES):
                continue
            tried += 1
            path = next((p for p in (note['local_path'], row and row['path']) if p and Path(p).is_file()), None)
            text = None
            try:
                if not path:
                    private_dir(self.paths.media)
                    # --read-only takes no store lock, so it works while wacli sync runs.
                    result = self.fx.run(['wacli', '--json', '--read-only', 'media', 'download', '--chat', key[0],
                                          '--id', key[1], '--output', str(self.paths.media) + '/',
                                          '--store', str(self.paths.wa)])
                    if result.returncode == 0:
                        path = json.loads(result.stdout)['data']['path']
                if path:
                    result = self.fx.run([*command, path])
                    text = result.stdout.strip() if result.returncode == 0 else None
            except Exception:
                pass  # a slow or broken download or transcriber only costs this attempt
            with self.transaction():
                self.db.execute('INSERT OR REPLACE INTO transcripts VALUES (?,?,?,?,?,?)',
                                (*key, path, text or None, (row['attempts'] if row else 0) + 1, now))
                self.record('voice_transcribed' if text else 'voice_failed', chat=key[0], msg_id=key[1])
        return tried

    def voice(self, m, chat, msg_id):
        """Put a voice note's transcript, or a note that it failed, into m. False while it is still to come."""
        attachments = json.loads(m['attachments_json'] or '[]')
        if not self.settings['transcribe'] or not any(a.get('media_type') == 'audio' for a in attachments):
            return True
        row = self.db.execute('SELECT * FROM transcripts WHERE chat=? AND msg_id=?', (chat, msg_id)).fetchone()
        if not row or not (row['text'] or row['attempts'] >= VOICE_TRIES):
            return False
        if row['path']:
            for a in attachments:
                if a.get('media_type') == 'audio':
                    a['local_path'] = row['path']
            m['attachments_json'] = json.dumps(attachments)
        m['text'] = ('Voice note, transcribed automatically:\n' + row['text'] if row['text'] else
                     'Voice note; automatic transcription failed. Transcribe the attached file before answering '
                     '(if no file is attached, download it with `wacli --read-only media download`).')
        m['own_text'] = m['text']
        return True

    def read_route(self, route, chat, since, tracked):
        if route['source'] == 'email':
            return sources.email(self.paths, chat, since, tracked, route=route, cfg=self.cfg)
        if route['source'] == 'teams':
            return sources.teams(self.paths, chat, since, tracked, self.settings['owner_teams_id'])
        return sources.whatsapp(self.paths, chat, since, tracked)

    def stored_message(self, message, route):
        if message['source'] == 'email':
            return self.db.execute("SELECT * FROM messages WHERE source='email' AND route=? AND msg_id=?",
                                   (route['name'], message['msg_id'])).fetchone()
        return self.db.execute('SELECT * FROM messages WHERE source=? AND account=? AND chat=? AND msg_id=?',
                               tuple(message[k] for k in ('source', 'account', 'chat', 'msg_id'))).fetchone()

    def sync(self, lookback=48):
        warning = account_warning(self.db, self.paths.wa_account)
        if warning:
            raise ValidationError(warning)
        now = self.fx.now()
        report = dict(dry_run=self.dry, ingested=0, created=[], wakes=[], problems={}, routes=[])
        problems = report['problems']
        report['voice_notes'] = self.transcribe(now, lookback)
        with self.transaction():
            problems.update(sources.health(self.paths, self.fx, now,
                                           {r['source'] for r in self.cfg['routes']},
                                           self.settings['whatsapp_stale_minutes']))
            if any(r['source'] == 'teams' for r in self.cfg['routes']):
                try:
                    problems.update(sources.teams_catchup(self.paths, self.cfg['routes'], now))
                except (OSError, ValueError, KeyError, sqlite3.Error):
                    pass  # an unreadable or unsupported store is already a heartbeat problem
            mapping = {}
            if self.settings['owner_whatsapp'] or any(r['source'] == 'whatsapp' for r in self.cfg['routes']):
                try:
                    mapping = sources.lid_map(self.paths)
                except (OSError, sqlite3.Error):
                    mapping = None
                    problems['health:lid_map'] = 'WhatsApp identity map unavailable'
            # Before the routes: when the owner's own chat is a route, an answer must not also become a request.
            if mapping is not None and PHONE.fullmatch(self.settings['owner_whatsapp']):
                try:
                    report['pings'] = self.owner_ping(mapping, now)
                except (OSError, KeyError, sqlite3.Error):
                    problems['health:owner_ping'] = 'Owner WhatsApp chat unavailable; ping not answered'
                try:
                    report['answers'] = self.owner_answers(mapping, now, problems)
                except (OSError, KeyError, sqlite3.Error):
                    problems['health:owner_chat'] = 'Owner WhatsApp chat unavailable; escalation answers not read'
            self.forward_escalations(now)
            for route in self.cfg['routes']:
                if route['source'] == 'whatsapp' and mapping is None:
                    continue  # do not consume unresolved messages while identity data is locked
                seed_key = 'seed:' + route['name']
                seeded = self.db.execute('SELECT value FROM meta WHERE key=?', (seed_key,)).fetchone()
                cutoff = max(float(seeded['value']) if seeded else now, route['active_ts'])
                try:
                    batch = []
                    watermarks = {}
                    chats = [route['chats']] if route['source'] == 'email' else route['chats']
                    for chat in chats:
                        since = now - lookback * 3600
                        if route['source'] == 'teams':
                            key = 'teams-changes:' + route['name'] + ':' + chat
                            previous = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
                            messages, latest = sources.teams_batch(self.paths, chat, since, [],
                                                                  self.settings['owner_teams_id'],
                                                                  after=json.loads(previous['value']) if previous else {})
                            batch.extend(messages)
                            watermarks[key] = latest
                        else:
                            tracked = [r['msg_id'] for r in self.db.execute(
                                f'SELECT DISTINCT m.msg_id FROM messages m LEFT JOIN requests r ON r.message_id=m.id '
                                f'WHERE m.route=? AND (? OR m.chat=?) AND r.state IN {ACTIVE}',
                                (route['name'], route['source'] == 'email', '' if isinstance(chat, list) else chat))]
                            batch.extend(self.read_route(route, chat, since, tracked))
                except (OSError, ValueError, KeyError, sqlite3.Error):
                    problems['health:route:' + route['name']] = f'{route["name"]}: archive unavailable or locked; skipped'
                    continue
                route_result = dict(route=route['name'], messages=len(batch), seeded=not bool(seeded), created=[])
                report['routes'].append(route_result)
                for m in batch:
                    if m['source'] == 'whatsapp' and self.db.execute(
                            'SELECT 1 FROM meta WHERE key=?',
                            ('card-command:' + m['chat'] + ':' + m['msg_id'],)).fetchone():
                        continue  # a pass or losing answer must not become a routed request
                    old = self.stored_message(m, route)
                    if not old and m['deleted']:
                        continue
                    m['sender_key'] = ('toucan' if m['source'] == 'whatsapp' and m['is_owner'] else
                                       sources.person_for(self.cfg, m['source'], m['sender_raw'], mapping))
                    if not m['is_owner'] and not m['sender_key'] and (m['sender_raw'] or '').endswith('@lid'):
                        problems['identity:' + m['sender_raw']] = 'Unresolved WhatsApp sender; stored as context only'
                    asks = seeded and not m['deleted'] and m['ts'] != 0 and m['ts'] >= cutoff and not m['is_owner'] and m['sender_key'] in route['requesters']
                    if m['source'] == 'whatsapp' and not self.voice(m, m['chat'], m['msg_id']) and asks and not old:
                        continue  # a voice note becomes a request once transcribe() has its text
                    if old:
                        if m['deleted'] and not m['text']:
                            m['text'], m['own_text'] = old['text'], old['own_text']
                        # A different reading of who is tagged changes what the thread should do with it;
                        # None means the bot has no mention data and keeps the stored reading.
                        retagged = (m.get('mentions_json') is not None and
                                    json.loads(old['mentions_json'] or '[]') != json.loads(m['mentions_json']))
                        changed = (retagged or any(old[k] != m[k] for k in ('text', 'edited', 'deleted')) or
                                   attachment_content(old['attachments_json']) != attachment_content(m['attachments_json']))
                        ready = (sum(bool(a.get('local_path')) for a in json.loads(m['attachments_json'] or '[]')) >
                                 sum(bool(a.get('local_path')) for a in json.loads(old['attachments_json'] or '[]')))
                        self.db.execute('UPDATE messages SET text=?,attachments_json=?,edited=?,deleted=?,ts=?,last_seen=?,'
                                        'sender_key=?,sender_raw=?,sender_name=?,is_owner=?,mentions_json=COALESCE(?,mentions_json),'
                                        "quoted_ids=COALESCE(NULLIF(?,'[]'),quoted_ids),own_text=? WHERE id=?",
                                        (m['text'], m['attachments_json'], m['edited'], m['deleted'], m['ts'] or old['ts'], now,
                                         m['sender_key'], m['sender_raw'], m['sender_name'], m['is_owner'], m.get('mentions_json'),
                                         m.get('quoted_ids'), m.get('own_text'), old['id']))
                        if old['deferred_cutoff'] is not None and asks and m['ts'] >= old['deferred_cutoff']:
                            if not self.db.execute('SELECT 1 FROM requests WHERE message_id=?', (old['id'],)).fetchone():
                                m['id'] = old['id']
                                self.new_request(m, route, mapping, report, route_result)
                            self.db.execute('UPDATE messages SET deferred_cutoff=NULL WHERE id=?', (old['id'],))
                        if changed:
                            reqs = self.rows(f'SELECT id,thread FROM requests WHERE message_id=? AND state IN {ACTIVE}', (old['id'],))
                            for req in reqs:
                                self.db.execute('UPDATE requests SET source_changed=1,change_pending=1,wakes=0,last_wake_at=NULL WHERE id=?', (req['id'],))
                                self.record('source_changed', req['thread'], req['id'], deleted=bool(m['deleted']))
                        elif ready:
                            # A screenshot finished downloading: tell `toucan wait` and the inbox once,
                            # without calling it an edit or giving the request a fresh wake budget.
                            for req in self.rows(f'SELECT id,thread FROM requests WHERE message_id=? AND state IN {ACTIVE}', (old['id'],)):
                                self.db.execute('UPDATE requests SET change_pending=1 WHERE id=?', (req['id'],))
                                self.record('attachment_ready', req['thread'], req['id'])
                    else:
                        # Only incomplete Teams observations after seeding can be promoted.
                        # Seed context and known non-requesters remain context permanently.
                        deferred = (cutoff if seeded and m['source'] == 'teams' and not m['is_owner'] and
                                    (not m['ts'] or not m['sender_raw']) and
                                    (not m['sender_raw'] or m['sender_key'] in route['requesters']) else None)
                        m['id'] = insert(self.db, 'messages', **m, route=route['name'], first_seen=now, last_seen=now,
                                         deferred_cutoff=deferred)
                        if asks:
                            self.new_request(m, route, mapping, report, route_result)
                    report['ingested'] += 1
                for key, latest in watermarks.items():
                    self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(latest)))
                if not seeded:
                    insert(self.db, 'meta', key=seed_key, value=str(now))
                    self.record('route_seeded', route['thread'], route=route['name'], count=len(batch))
            # On an owner_reply_closes route, the owner answering in the chat before Toucan sends anything stands Toucan down.
            # Toucan has replied to a message once any of its requests (the parts of a split) has a send, and a
            # request holding a promise is never stood down: its acknowledgement may have gone out with another part.
            stand_down = [r['name'] for r in self.cfg['routes'] if r.get('owner_reply_closes')]
            if stand_down:
                for req in self.rows(
                        f"SELECT r.id,r.thread FROM requests r JOIN messages q ON q.id=r.message_id "
                        f"WHERE (r.state IN {ACTIVE} OR r.state='escalated') AND r.reply_source='teams' "
                        f"AND r.route IN ({','.join('?' * len(stand_down))}) AND r.promise IS NULL "
                        "AND NOT EXISTS (SELECT 1 FROM requests s JOIN outbox o ON o.request_id=s.id WHERE s.message_id=r.message_id "
                        "AND o.kind='send' AND o.state IN ('sent','sending','uncertain','pending')) "
                        "AND EXISTS (SELECT 1 FROM messages m WHERE m.source='teams' AND m.chat=r.reply_chat "
                        "AND m.is_owner=1 AND m.deleted=0 AND m.ts>q.ts)", stand_down):
                    self.db.execute("UPDATE requests SET state='closed',closed_at=?,close_reason='owner replied',routing=NULL WHERE id=?", (now, req['id']))
                    self.record('owner_replied', req['thread'], req['id'])
            # Held requests whose time is up, or whose route is no longer live, go to a thread before the wakes.
            router.expire(self, now, problems)
            limited = self.rate_limits(problems, now)
            self.wake(report, problems, now, limited)
            self.rotate_threads(report, problems, now, limited)
            posted = self.rows(f"SELECT id,thread FROM requests WHERE state IN {OPEN} "
                               "AND reply_source='teams' AND EXISTS (SELECT 1 FROM outbox o JOIN messages m "
                               "ON m.source='teams' AND m.chat=requests.reply_chat AND m.is_owner=1 AND m.deleted=0 "
                               "WHERE o.request_id=requests.id AND o.kind='draft' AND o.state='sent' AND m.ts>o.updated_at)")
            for req in posted:
                self.db.execute("UPDATE requests SET state='closed',closed_at=?,close_reason='owner posted' WHERE id=?", (now, req['id']))
                self.record('owner_posted', req['thread'], req['id'])
            # Held requests (bounced back to the router) keep their promise, so it stays watched.
            for req in self.rows(f"SELECT * FROM requests WHERE state IN {OPEN} OR state='held'"):
                if req['promise_due'] is not None:
                    if now > req['promise_due']:
                        problems[f'promise:{req["id"]}'] = (f'{req["thread"]}: request {req["id"]} has an overdue promise '
                                                            f'due {self.local(req["promise_due"])}; still open')
                elif req['state'] == 'claimed' and now - req['claimed_at'] > self.settings['claimed_stale_hours'] * 3600:
                    problems[f'claimed:{req["id"]}'] = f'{req["thread"]}: request {req["id"]} has a stale claim'
                if req['state'] == 'done' and now - req['done_at'] > self.settings['done_unclosed_minutes'] * 60:
                    problems[f'done:{req["id"]}'] = f'{req["thread"]}: work finished on request {req["id"]}, reply still outstanding'
            for out in self.rows("SELECT o.*,r.thread FROM outbox o LEFT JOIN requests r ON r.id=o.request_id "
                                 "WHERE o.state='pending' AND o.updated_at<?", (now - 300,)):
                self.db.execute("UPDATE outbox SET state='failed',error='sender never attempted; pending expired',updated_at=? WHERE id=?",
                                (now, out['id']))
                self.record('delivery_failed', out['thread'], out['request_id'], outbox=out['id'], never_attempted=True)
            for out in self.rows("SELECT o.*,r.thread FROM outbox o LEFT JOIN requests r ON r.id=o.request_id "
                                 "WHERE o.state='sending' AND o.updated_at<?", (now - 120,)):
                self.db.execute("UPDATE outbox SET state='uncertain',updated_at=? WHERE id=?", (now, out['id']))
                self.record('delivery_uncertain', out['thread'], out['request_id'], outbox=out['id'])
            for out in self.rows("SELECT id FROM outbox WHERE state='uncertain'"):
                problems[f'uncertain:{out["id"]}'] = f'Outbox {out["id"]}: delivery uncertain; inspect before sending again'
            for out in self.rows("SELECT o.id,o.error FROM outbox o WHERE o.state='failed' AND NOT EXISTS ("
                                 "SELECT 1 FROM outbox retry WHERE retry.state='sent' AND retry.id>o.id "
                                 "AND retry.request_id IS o.request_id AND retry.kind=o.kind AND retry.source=o.source "
                                 "AND retry.to_chat=o.to_chat AND (o.request_id IS NOT NULL OR (retry.text=o.text AND retry.file IS o.file)))"):
                reason = 'sender never attempted; safe to retry' if out['error'] == 'sender never attempted; pending expired' else 'delivery failed; inspect outbox error and retry'
                problems[f'failed:{out["id"]}'] = f'Outbox {out["id"]}: {reason}'
            problems.update(router.problems(self))
            self.reconcile_alerts(problems)
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync',?)", (str(now),))
            self.record('sync', ingested=report['ingested'], created=len(report['created']), dry=self.dry)
        self.flush_notices(report['problems'])
        # After the transaction: router commands can take seconds, and wakes have already gone out.
        try:
            report['router'] = router.run(self, now)
        except Exception as error:
            report['problems']['router:failed'] = router.failed(self, error)
        # Requests the router stage just placed are woken now, with the usual gap and wake budget.
        threads = set((report.get('router') or {}).get('routed') or ())
        if threads:
            try:
                with self.transaction():
                    self.wake(report, report['problems'], self.fx.now(), limited, only=threads)
            except Exception as error:
                report['problems']['router:wake'] = f'Router: waking routed requests failed ({type(error).__name__}); the next sync wakes them'
        # Last: GitHub reads are slow and optional. A failure is logged for `toucan ledger status`, never a sync problem.
        if not self.shadow:
            try:
                report['ledger'] = ledger.github_sync(self)
            except Exception as error:
                report['ledger'] = dict(error=type(error).__name__)
        return report

    def owner_ping(self, mapping, now):
        """The owner sends "ping" to the assistant number and gets "pong" back, once per message.

        Runs before owner_answers and the routes, and marks the message as a card command, so a
        ping never answers a card or becomes a request. Pings older than PING_WINDOW are ignored."""
        owner = self.settings['owner_whatsapp']
        answered = []
        for row in sources.owner_chat(self.paths, self.owner_chats(mapping), now - PING_WINDOW):
            if row['from_me'] or mapping.get(row['sender_jid'], row['sender_jid']) != owner:
                continue
            if row.get('is_forwarded') or row.get('revoked') or not PING.fullmatch(row['text'] or ''):
                continue
            consumed = 'card-command:' + row['chat_jid'] + ':' + row['msg_id']
            if self.db.execute('SELECT 1 FROM meta WHERE key=?', (consumed,)).fetchone() or self.db.execute(
                    "SELECT 1 FROM messages WHERE source='whatsapp' AND account=? AND chat=? AND msg_id=?",
                    (self.paths.wa_account, row['chat_jid'], row['msg_id'])).fetchone():
                continue  # already ponged, or already taken as a card answer or request
            insert(self.db, 'meta', key=consumed, value=str(now))
            open_requests = self.db.execute(f'SELECT count(*) FROM requests WHERE state IN {OPEN}').fetchone()[0]
            waiting = self.db.execute("SELECT count(*) FROM cards c JOIN requests r ON r.id=c.request_id "
                                      "WHERE c.closed_at IS NULL AND r.state!='closed'").fetchone()[0]
            self.notice_once(row['msg_id'], f'pong {self.local(now)}. {open_requests} open requests, '
                                            f'{waiting} card{"" if waiting == 1 else "s"} waiting on you.')
            answered.append(row['msg_id'])
        return answered

    def owner_answers(self, mapping, now, problems):
        """Hand escalated requests back to their thread when the owner answers on WhatsApp.

        A message that starts with a card ID ("T435 B") answers that card. Otherwise a message
        answers an escalation when it quotes it; an unquoted one answers the escalation sent just
        before it, or else the only escalation still waiting for an answer when it was sent (one
        answered in the last 10 minutes still takes follow-ups). With several open, Toucan asks
        once which card it is for. "more" (quoted, or "more T435") sends the card's full detail.
        A bare letter answers with that option spelled out. Unquoted forwards and caption-less
        documents never answer. When that chat is also a route, only card IDs, quotes and the
        message just after an escalation answer, and anything else becomes a request."""
        sent = self.rows("SELECT o.external_id,o.text AS sent_text,o.created_at AS sent_at,r.id,r.thread,r.route,r.state,r.closed_at,"
                         "c.closed_at AS card_closed,c.delegate_jid FROM outbox o JOIN requests r ON r.id=o.request_id "
                         "LEFT JOIN cards c ON (c.outbox_id=o.id OR c.forward_outbox=o.id) AND c.request_id=r.id "
                         "WHERE o.kind='escalation' AND o.state='sent'")
        # Cards held for the daily list, or sent there, have no escalation message of their own.
        carded = self.rows("SELECT c.request_id AS id,c.created_at AS sent_at,c.delegate_jid,r.thread,r.route,r.state,r.closed_at "
                           "FROM cards c JOIN requests r ON r.id=c.request_id WHERE c.closed_at IS NULL AND r.state!='closed'")
        waiting = [w for w in sent if w['state'] != 'closed' or w['delegate_jid']]
        if not waiting and not carded:
            return []
        owner = self.settings['owner_whatsapp']
        delegates = {w['delegate_jid'] for w in sent + carded if w['delegate_jid']}
        participants = delegates | {owner}
        chats = list(participants | {lid for lid, pn in mapping.items() if pn in participants})
        routed = {c for r in self.cfg['routes'] if r['source'] == 'whatsapp' for c in r['chats']} & set(chats)
        by_id = {w['external_id']: w for w in waiting if w['external_id']}
        # A card replaced by a later one on the same request: quoting it must not answer the new card.
        replaced = {w['external_id'] for w in waiting if w['external_id'] and w['card_closed'] is not None}
        by_text = {w['sent_text']: w for w in waiting}
        by_request = {**{w['id']: w for w in waiting}, **{w['id']: w for w in carded}}
        for row in self.db.execute("SELECT key,value FROM meta WHERE key LIKE 'quote:%'"):
            if int(row['value']) in by_request:
                by_id[row['key'][6:]] = by_request[int(row['value'])]  # a card's "more" detail message
        answered, last_by_chat = [], {}
        for row in sources.owner_chat(self.paths, chats, min(w['sent_at'] for w in waiting + carded) - 60):
            if row['from_me']:
                last_out = row['msg_id']
                last_by_chat[row['chat_jid']] = last_out
                if last_out not in by_id and row['text'] in by_text:
                    by_id[last_out] = by_text[row['text']]  # sender reply carried no message ID
                    if by_text[row['text']]['card_closed'] is not None:
                        replaced.add(last_out)
                continue
            actor = mapping.get(row['sender_jid'], row['sender_jid'])
            if actor not in participants:
                continue
            consumed = 'card-command:' + row['chat_jid'] + ':' + row['msg_id']
            if self.db.execute('SELECT 1 FROM meta WHERE key=?', (consumed,)).fetchone():
                continue
            last_out = last_by_chat.get(row['chat_jid'])
            eligible = {k: w for k, w in by_request.items() if actor == owner or w['delegate_jid'] == actor}

            said = raw = row['text'] or row['display_text'] or row.get('media_caption') or ''
            if row.get('is_forwarded') and not row['quoted_msg_id']:
                continue  # forwarded material is something to look at, not an answer or a command
            old = self.db.execute("SELECT * FROM messages WHERE source='whatsapp' AND account=? AND chat=? AND msg_id=?",
                                  (self.paths.wa_account, row['chat_jid'], row['msg_id'])).fetchone()
            deleted = int(bool(row['revoked'] or row.get('deleted_for_me')))
            if old and ((old['own_text'] if old['own_text'] is not None else old['text']), old['edited'], old['deleted']) == (
                    raw or old['own_text'] or old['text'], int(bool(row['edited'])), deleted):
                continue  # handled on an earlier sync, and unchanged since
            more = cards.MORE.match(said)
            tagged = None if more else cards.CARD_ID.match(said)
            if tagged and cards.MORE.match(tagged[3]):
                more, tagged = cards.MORE.match('more ' + tagged[1]), None  # "T435 more"
            if tagged:
                named = cards.card_id(tagged[1], int(tagged[2] or 1))
                target = eligible.get(int(tagged[1]))
                if not target:
                    self.notice_once(row['msg_id'], f'{named} isn\'t waiting on you.' + self.open_cards_hint(eligible), recipient=actor)
                    continue
                current = self.card_label(target['id'])
                if named.upper() != current.upper():
                    self.notice_once(row['msg_id'], f'{named} isn\'t open any more; {current} replaced it. '
                                                    f'Send your answer again starting with {current}, or "more {current}".', recipient=actor)
                    continue
                said = tagged[3].strip()
                if not said and not row.get('media_type'):
                    continue  # just the ID; nothing to hand back
            elif more and more[1]:
                target = eligible.get(int(more[1]))
                if not target:
                    self.notice_once(row['msg_id'], f'{cards.card_id(more[1])} isn\'t waiting on you.' + self.open_cards_hint(eligible), recipient=actor)
                    continue
            elif row['quoted_msg_id']:
                target = by_id.get(row['quoted_msg_id'])
                if target and row['quoted_msg_id'] in replaced:
                    current = self.card_label(target['id'])
                    self.notice_once(row['msg_id'], f'That card isn\'t open any more; {current} replaced it. '
                                                    f'Send your answer again starting with {current}, or "more {current}".', recipient=actor)
                    continue
            elif row.get('is_forwarded') or (row['media_type'] == 'document' and not (row['text'] or row.get('media_caption'))):
                continue  # forwarded material and bare files are something to look at, not an answer
            elif said.strip().lower() == 'pass' and actor != owner:
                insert(self.db, 'meta', key=consumed, value=str(now))
                open_to_delegate = [w for w in eligible.values() if (c := self.open_card(w['id']))
                                    and c['delegate_jid'] == actor and c['answered_at'] is None]
                target = open_to_delegate[0] if len(open_to_delegate) == 1 else None
                if len(open_to_delegate) > 1:
                    self.notice_once(row['msg_id'], 'Which card? Send its ID followed by pass.', recipient=actor)
            elif actor == owner and row['chat_jid'] in routed:
                target = by_id.get(last_out)
            else:
                live = {w['id'] for w in sent if (actor == owner or w['delegate_jid'] == actor) and w['sent_at'] <= row['ts'] and (w['closed_at'] is None or w['closed_at'] > row['ts'])}
                live |= {w['id'] for w in carded if (actor == owner or w['delegate_jid'] == actor) and w['sent_at'] <= row['ts']}
                live = {i for i in live if self.awaiting_answer(i, row['ts'])}
                target = by_id.get(last_out) or (eligible.get(next(iter(live))) if len(live) == 1 else None)
                if not target and len(live) > 1:
                    ids = ', '.join(self.card_label(i) for i in sorted(live))
                    self.notice_once(row['msg_id'], f'Which card is that for? Send it again starting with the ID: {ids}.', recipient=actor)
            if not target or target['id'] not in eligible:
                continue
            card = self.open_card(target['id']) or self.db.execute('SELECT * FROM cards WHERE request_id=? ORDER BY id DESC LIMIT 1', (target['id'],)).fetchone()
            if card and card['answered_by'] and card['answered_by'] != actor:
                insert(self.db, 'meta', key=consumed, value=str(now))
                name = self.settings['owner_name'] if card['answered_by'] == owner else card['delegate_name']
                self.notice_once(row['msg_id'], f'{self.card_label(target["id"])} already answered by {name}.', recipient=actor)
                continue
            if said.strip().lower() == 'pass' and card and card['delegate_jid'] == actor:
                if card['closed_at'] is None and card['answered_at'] is None:
                    self.forward_card(card, f'{card["delegate_name"]} passed this to you')
                    self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', (consumed, str(now)))
                    self.once('pass:' + row['chat_jid'] + ':' + row['msg_id'],
                              f'{self.card_label(target["id"])} sent to {self.settings["owner_name"]}.',
                              target['id'], recipient=actor)
                continue
            if more:
                self.send_detail(row, target, raw, actor)
                continue
            card = self.open_card(target['id'])
            if card:
                said = cards.expand(said, json.loads(card['options_json'] or '[]'))
            attachments = [{k: row[k] for k in ('media_type', 'filename', 'local_path') if row.get(k)}]
            note = dict(text=said, attachments_json=json.dumps([a for a in attachments if a]))
            if not self.voice(note, row['chat_jid'], row['msg_id']):
                continue  # answered by voice; read it once transcribe() has the text
            text = note['text']
            if old:
                # own_text keeps what the owner typed ("T435 B"); text is what the thread reads.
                self.db.execute('UPDATE messages SET text=?,own_text=?,edited=?,deleted=?,last_seen=? WHERE id=?',
                                (text or old['text'], raw or old['own_text'], int(bool(row['edited'])), deleted, now, old['id']))
            elif deleted:
                continue
            else:
                message = insert(self.db, 'messages', source='whatsapp', account=self.paths.wa_account, chat=row['chat_jid'],
                                 msg_id=row['msg_id'], route=target['route'], sender_key='owner' if actor == owner else next((k for k, p in self.cfg['people'].items() if actor in p.get('whatsapp', [])), 'delegate'),
                                 sender_raw=row['sender_jid'], sender_name=row['sender_name'], is_owner=1, ts=row['ts'],
                                 text=text, own_text=raw, attachments_json=note['attachments_json'],
                                 edited=int(bool(row['edited'])), deleted=0, first_seen=now, last_seen=now)
                insert(self.db, 'owner_answers', message_id=message, request_id=target['id'])
            state = self.db.execute('SELECT state FROM requests WHERE id=?', (target['id'],)).fetchone()['state']
            if state == 'closed':
                continue
            self.db.execute('UPDATE cards SET answered_at=? WHERE request_id=? AND closed_at IS NULL AND answered_at IS NULL',
                            (now, target['id']))
            if card and card['delegate_jid']:
                self.db.execute('UPDATE cards SET answered_by=? WHERE id=?', (actor, card['id']))
            if state == 'escalated':
                self.db.execute("UPDATE requests SET state='new',claim_id=NULL,claimed_at=NULL,done_at=NULL,closed_at=NULL,"
                                "close_reason=NULL,change_pending=0,wakes=0,last_wake_at=NULL WHERE id=?", (target['id'],))
            else:
                self.db.execute('UPDATE requests SET change_pending=1,wakes=0,last_wake_at=NULL WHERE id=?', (target['id'],))
            self.record('owner_answered', target['thread'], target['id'])
            answered.append(target['id'])
            if not old:  # an edited answer reaches the thread too, but gets no second receipt
                self.once('receipt:' + row['msg_id'], cards.receipt(self.card_label(target['id']), target['thread'], text), target['id'], recipient=actor)
        return answered

    def open_card(self, req):
        return self.db.execute(f'SELECT c.*,{cards.SEQ} FROM cards c WHERE c.request_id=? AND c.closed_at IS NULL '
                               'ORDER BY c.id DESC LIMIT 1', (req,)).fetchone()

    def awaiting_answer(self, req, ts):
        """Whether an unquoted owner message sent at ts can still answer this request's card.

        An answered card stays open until the thread closes the request, which can take days. Without
        this, any later unrelated message ("Ping", a number) was handed to that card's thread. A card
        answered in the last 10 minutes still takes follow-ups; older ones need the card ID or a quote."""
        card = self.db.execute('SELECT closed_at,answered_at FROM cards WHERE request_id=? ORDER BY id DESC LIMIT 1',
                               (req,)).fetchone()
        if not card:
            return True  # escalated before cards existed
        if card['closed_at'] is not None and card['closed_at'] <= ts:
            return False
        return card['answered_at'] is None or card['answered_at'] > ts - ANSWER_FOLLOWUP_SECONDS

    def card_label(self, req):
        """The ID of the request's open card: T435, or T435-2 once a second card replaced the first."""
        card = self.open_card(req)
        return cards.label(card) if card else cards.card_id(req)

    def open_cards_hint(self, by_request):
        ids = [self.card_label(r) for r, w in sorted(by_request.items()) if w['state'] == 'escalated']
        return f' Open: {", ".join(ids)}.' if ids else ' Nothing is waiting on you.'

    def notice_once(self, msg_id, text, recipient=None):
        """A one-line reply to the owner about one of their messages, sent once."""
        self.once('notice:' + msg_id, text, recipient=recipient)

    def once(self, key, text, req=None, recipient=None):
        """Send the owner text once per key. With req, a quote on it (or a reply right after it) goes to that request."""
        if self.db.execute('SELECT 1 FROM meta WHERE key=?', (key,)).fetchone():
            return
        insert(self.db, 'meta', key=key, value=str(self.fx.now()))
        self.queue_owner(text, req, recipient)

    def queue_owner(self, text, req=None, recipient=None):
        if self.shadow:
            return None
        out = self._queue_delivery(None, None, 'escalation', 'whatsapp', recipient or self.settings['owner_whatsapp'], text, None)
        self.queued.append((out, req))
        return out

    def send_detail(self, row, target, raw, recipient=None):
        """The owner replied "more": send the card's full context once. The message is stored with no
        route, so no inbox shows it and an owner-chat route doesn't make it a request."""
        now = self.fx.now()
        old = self.db.execute("SELECT id FROM messages WHERE source='whatsapp' AND account=? AND chat=? AND msg_id=?",
                              (self.paths.wa_account, row['chat_jid'], row['msg_id'])).fetchone()
        if old:
            self.db.execute('UPDATE messages SET text=?,own_text=?,edited=?,last_seen=? WHERE id=?',
                            (raw, raw, int(bool(row['edited'])), now, old['id']))
            return  # an edited "more" doesn't send the detail again
        insert(self.db, 'messages', source='whatsapp', account=self.paths.wa_account, chat=row['chat_jid'],
               msg_id=row['msg_id'], route=None, sender_key='owner', sender_raw=row['sender_jid'],
               sender_name=row['sender_name'], is_owner=1, ts=row['ts'], text=raw, own_text=raw, attachments_json='[]',
               edited=int(bool(row['edited'])), deleted=0, first_seen=now, last_seen=now)
        card = self.open_card(target['id'])
        req = self.db.execute('SELECT r.*,m.text AS asked FROM requests r JOIN messages m ON m.id=r.message_id WHERE r.id=?',
                              (target['id'],)).fetchone()
        name = self.cfg['people'].get(req['reply_person'], {}).get('name', 'someone')
        if card:
            text = cards.detail(card, req['thread'], name, req['part'] or req['asked'])
        else:
            last = self.db.execute("SELECT text FROM outbox WHERE request_id=? AND kind='escalation' AND state='sent' "
                                   'ORDER BY id DESC LIMIT 1', (target['id'],)).fetchone()
            text = last['text'] if last else f'No detail stored for {self.card_label(target["id"])}.'
        self.queue_owner(text, target['id'], recipient)
        self.record('card_detail', req['thread'], target['id'])

    def flush_notices(self, problems):
        queued, self.queued = self.queued, []
        if self.dry or self.shadow:
            return
        for out, req in queued:
            try:
                result = self._send_outbox(out, None)
            except Exception:
                problems[f'notice:{out}'] = f'Outbox {out}: a notice to the owner failed; inspect outbox'
                continue
            if req and result.get('external_id'):
                with self.transaction():
                    self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('quote:' + result['external_id'], str(req)))

    def rules_due(self, thread, rules):
        """Whether inbox should print the standing rules in full: they are new to this T3 thread (a rotated
        successor counts as new), they changed, or the thread last saw them over context_hours ago.
        Printing them on every wake would fill a long-running owner thread's context."""
        digest = hashlib.sha256('\n\n'.join(rules).encode()).hexdigest()
        live, now = self.live_thread(thread), self.fx.now()
        row = self.db.execute('SELECT value FROM meta WHERE key=?', ('rules:' + thread,)).fetchone()
        seen = json.loads(row['value']) if row else {}
        if (seen.get('digest') == digest and seen.get('t3_thread') == live
                and now - seen.get('at', 0) < self.settings['context_hours'] * 3600):
            return False
        with self.transaction():
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                            ('rules:' + thread, json.dumps(dict(digest=digest, t3_thread=live, at=now))))
        return True

    def pending(self, thread):
        return self.rows("SELECT r.*,m.ts FROM requests r JOIN messages m ON m.id=r.message_id "
                         f"WHERE r.thread=? AND r.state IN {OPEN} AND (r.state='new' OR r.change_pending=1) AND r.routing IS NULL", (thread,))

    def waiter_running(self, thread):
        beat = self.db.execute('SELECT value FROM meta WHERE key=?', ('waiter:' + thread,)).fetchone()
        return bool(beat) and self.fx.now() - float(beat['value']) < WAITER_FRESH

    def wait(self, thread, timeout, poll=5):
        """Block until the thread has requests to handle; None after timeout.

        T3 queues webhook runs behind a busy turn, so an owner thread keeps this running as a
        background command: its exit reaches the agent mid-turn. The heartbeat lets inbox and
        status show whether one is running."""
        if thread not in self.cfg['threads']:
            raise ValidationError('unknown thread')
        key, deadline = 'waiter:' + thread, self.fx.now() + timeout
        owner, checked = self.live_thread(thread), self.fx.now()
        try:
            while True:
                if self.fx.now() - checked >= 30:
                    live = sources.hook_file_thread(self.paths, self.cfg['threads'][thread]['hook_file'])
                    checked = self.fx.now()
                    if owner and live and live != owner:
                        return dict(thread=thread, replaced=live)
                try:
                    with self.transaction():
                        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(self.fx.now())))
                        pending = self.pending(thread)
                        if pending:
                            self.db.executemany('UPDATE requests SET change_pending=0 WHERE id=?', [(r['id'],) for r in pending])
                except sqlite3.OperationalError:
                    pending = []  # sync holds the lock; leave edits pending and try next poll
                if pending:
                    return dict(thread=thread, requests=[r['id'] for r in pending],
                                latest=self.local(max(r['ts'] for r in pending)))
                if self.fx.now() >= deadline:
                    return None
                self.fx.sleep(poll)
        finally:
            try:
                with self.transaction():
                    self.db.execute('DELETE FROM meta WHERE key=?', (key,))
            except sqlite3.OperationalError:
                pass  # the heartbeat goes stale on its own

    def rate_limits(self, problems, now):
        """Threads whose last turn ended on a model rate limit, and when. Their unclaimed requests
        move to the thread's fallback once it has been stuck for fallback_minutes, at most once each.
        A request handed to a fallback that is itself limited goes back to its owner once the owner can work."""
        limited, until = {}, {}
        for key, thread in self.cfg['threads'].items():
            try:
                limit = sources.rate_limit(self.paths, self.live_thread(key), self.settings['timezone'])
            except (OSError, sqlite3.Error, ValueError):
                problems['health:t3'] = 'T3 history unavailable; rate-limited threads are not detected'
                continue
            if limit:
                limited[key], until[key] = limit
                then = (f'Toucan wakes it after the limit ends at {self.local(until[key])}' if until[key] and until[key] > now
                        else 'Toucan keeps waking it')
                problems['limited:' + key] = f'{key}: last turn stopped at a model rate limit at {self.local(limited[key])}; {then}'
        self.limit_until = until
        for key in limited:
            for req in self.rows("SELECT id FROM requests WHERE thread=? AND state='new' AND routing IS NULL", (key,)):
                moved = self.db.execute("SELECT kind,thread,detail_json FROM events WHERE request_id=? "
                                        "AND kind IN ('handed_over','handed_back','forwarded') ORDER BY id DESC LIMIT 1",
                                        (req['id'],)).fetchone()
                if not moved or moved['kind'] != 'handed_over' or moved['thread'] != key:
                    continue
                owner = json.loads(moved['detail_json'] or '{}').get('from_thread')
                if owner in self.cfg['threads'] and owner not in limited:
                    self.db.execute('UPDATE requests SET thread=?,wakes=0,last_wake_at=NULL WHERE id=?', (owner, req['id']))
                    self.record('handed_back', owner, req['id'], from_thread=key)
        for key, stopped in limited.items():
            backup = self.cfg['threads'][key].get('fallback')
            if not backup or backup in limited:
                continue
            # A request goes to the fallback once. One the fallback handed back waits for this thread.
            for req in self.rows("SELECT id,created_at FROM requests r WHERE thread=? AND state='new' AND routing IS NULL "
                                 "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.request_id=r.id AND e.kind='handed_over')",
                                 (key,)):
                if now - max(req['created_at'], stopped) >= self.settings['fallback_minutes'] * 60:
                    self.db.execute('UPDATE requests SET thread=?,wakes=0,last_wake_at=NULL WHERE id=?', (backup, req['id']))
                    self.record('handed_over', backup, req['id'], from_thread=key)
        return limited

    def wake(self, report, problems, now, limited, only=None):
        rewake = self.settings['rewake_minutes'] * 60
        for key, thread in self.cfg['threads'].items():
            if only is not None and key not in only:
                continue
            pending = self.pending(key)
            stopped = limited.get(key)
            # Wakes into a rate-limited thread fail at once, so they don't use up the wake budget.
            eligible = [r for r in pending if (stopped or r['wakes'] < self.settings['max_wakes']) and
                        (r['last_wake_at'] is None or now - r['last_wake_at'] >= rewake)]
            last = self.db.execute('SELECT value FROM meta WHERE key=?', ('wake:' + key,)).fetchone()
            since_last = now - float(last['value']) if last else None
            fresh = any(r['last_wake_at'] is None for r in eligible)
            gap = self.settings['wake_gap_minutes'] * 60 if fresh else rewake
            if since_last is not None and since_last < gap:
                eligible = []  # batch bursts; a first wake waits at most wake_gap_minutes
            # A turn that ended on a rate limit needs a wake to carry on, with or without requests,
            # but not before the provider said the limit ends: those wakes fail at once.
            ends = self.limit_until.get(key) if stopped else None
            resume = (bool(stopped) and now - stopped >= rewake and (since_last is None or since_last >= rewake)
                      and not (ends and now < ends))
            if stopped and not resume:
                eligible = []
            if eligible or resume:
                summary = f'{key}: no new requests'
                if eligible:
                    latest = self.local(max(r['ts'] for r in eligible)).split(' ', 1)[1]
                    summary = f'{key}: {len(eligible)} new requests (latest {latest})'
                if stopped:
                    summary += f'. Your last turn stopped at a model rate limit at {self.local(stopped)}; carry on where you left off'
                payload = dict(thread=key, summary=summary, count=len(eligible))
                report['wakes'].append(payload)
                ok = self.post_hook(key, payload)
                if ok:
                    for req in eligible:
                        self.db.execute('UPDATE requests SET wakes=wakes+?,last_wake_at=? WHERE id=?',
                                        (0 if stopped else 1, now, req['id']))
                    self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('wake:' + key, str(now)))
                    self.record('wake', key, count=len(eligible), shadow=self.shadow)
                else:
                    problems['hook:' + key] = f'{key}: webhook failed; requests retained, check hook file and task'
                    self.record('wake_failed', key, count=len(eligible))
            for req in [] if stopped else pending:
                if req['state'] == 'new' and req['wakes'] and now - req['created_at'] >= self.settings['ack_alert_minutes'] * 60:
                    name = self.cfg['people'].get(req['reply_person'], {}).get('name', 'someone')
                    problems[f'unacked:{req["id"]}'] = (f'{key}: request {req["id"]} from {name} not picked up after '
                                                         f'{self.settings["ack_alert_minutes"]:g} min; the thread may be busy')
            exhausted = self.db.execute("SELECT count(*) FROM requests WHERE thread=? AND state='new' AND routing IS NULL AND wakes>=?",
                                        (key, self.settings['max_wakes'])).fetchone()[0]
            if exhausted:
                problems['unclaimed:' + key] = f'{key}: {exhausted} requests unclaimed after {self.settings["max_wakes"]} wakes'

    def post_hook(self, key, payload):
        if self.dry or self.shadow:
            return True
        try:
            thread = self.cfg['threads'][key]
            if hook_warning(thread):
                return False
            url = Path(thread['hook_file']).expanduser().read_text().strip()
            if not url.startswith(('http://', 'https://')):
                return False
            return self.fx.post(url, json.dumps(payload).encode(), {'Content-Type': 'application/json'}) in (200, 202)
        except Exception:
            return False

    def request_rotation(self, key, old, tokens):
        rotation = dict(old=old, time=self.fx.now(), tokens=tokens)
        insert(self.db, 'meta', key='rotate:' + key, value=json.dumps(rotation))
        self.record('rotation_requested', key, **rotation)
        return rotation

    def rotate_threads(self, report, problems, now, limited):
        if not t3.probe(self.paths):
            return
        for key, thread in self.cfg['threads'].items():
            if key in limited:
                continue
            live, rotation = self.live_thread(key), self.rotation(key)
            if rotation:
                resolved = sources.hook_file_thread(self.paths, thread['hook_file'])
                if resolved and resolved != rotation['old']:
                    self.record('rotated', key, old=rotation['old'], new=resolved, tokens=rotation['tokens'])
                    self.db.execute('DELETE FROM meta WHERE key=?', ('rotate:' + key,))
                    if self.settings['alert_url'] and not self.dry and not self.shadow:
                        size = f'{rotation["tokens"] / 1000:g}k tokens' if rotation['tokens'] is not None else 'unknown context size'
                        text = f'{key} moved to a fresh thread; the old one had {size}'
                        try:
                            self.fx.post(self.settings['alert_url'], text.encode(), {'Title': 'Toucan rotated a thread'})
                        except Exception:
                            self.record('notification_failed', key, reason='rotated')
                elif resolved == rotation['old'] and now - rotation['time'] >= self.settings['rotate_stall_minutes'] * 60:
                    at = self.local(rotation['time']).split(' ', 1)[1]
                    problems['rotate:' + key] = (f"{key}: rotation requested at {at} hasn't finished; "
                                                'the hook still points at the old thread')
                continue
            usage = sources.thread_usage(self.paths, live)
            threshold = thread.get('rotate_tokens', self.settings['rotate_tokens'])
            if (not live or not usage or usage['busy'] is not False or usage['used_tokens'] < threshold
                    or self.pending(key) or self.db.execute(
                        "SELECT 1 FROM outbox o JOIN requests r ON r.id=o.request_id "
                        "WHERE r.thread=? AND o.state IN ('pending','sending')", (key,)).fetchone()):
                continue
            tokens = usage['used_tokens']
            summary = (f'{key}: your context is {tokens / 1000:g}k tokens, over the {threshold / 1000:g}k limit. '
                       f'Hand over to a fresh thread now: run `toucan rotate --thread {key}` and follow its steps exactly.')
            payload = dict(thread=key, summary=summary, count=0, rotate=True)
            report['wakes'].append(payload)
            if self.post_hook(key, payload):
                self.request_rotation(key, live, tokens)
            else:
                problems['hook:' + key] = f'{key}: webhook failed; requests retained, check hook file and task'
                self.record('wake_failed', key, count=0, rotate=True)

    def rotate(self, key, check=False, reset=False):
        if key not in self.cfg['threads']:
            raise ValidationError('unknown thread')
        with self.transaction():
            live, rotation = self.live_thread(key), self.rotation(key)
            hook_file = Path(self.cfg['threads'][key]['hook_file']).expanduser()
            if reset:
                self.db.execute('DELETE FROM meta WHERE key=?', ('rotate:' + key,))
                self.record('rotation_reset', key)
                return dict(thread=key, reset=True)
            if not t3.probe(self.paths):
                raise Refused('rotation disabled: T3 database schema is unavailable or unsupported')
            if check:
                warning = hook_warning(self.cfg['threads'][key])
                resolved = sources.hook_file_thread(self.paths, self.cfg['threads'][key]['hook_file'])
                live = resolved or live
                done = bool(live and not warning and (not rotation or resolved and resolved != rotation['old']))
                failure = warning or ('the hook still points at the old thread' if resolved else 'the hook does not resolve to a thread')
                message = (f'{key}: rotation is done; live thread {live}' if rotation and done else
                           f'{key}: rotation is not done; {failure}' if rotation else
                           f'{key}: live thread {live or "unknown"}')
                if warning and not rotation:
                    message += '; ' + warning
                return dict(thread=key, done=done, t3_thread=live, context=sources.thread_usage(self.paths, live), message=message)
            try:
                info = sources.hook_info(hook_file.read_text().strip())
            except (OSError, UnicodeError):
                info = None
            if not info or not live or hook_warning(self.cfg['threads'][key]):
                raise Refused('rotation needs a mode-600 hook file with a T3 webhook task and a live thread')
            if not rotation:
                usage = sources.thread_usage(self.paths, live)
                rotation = self.request_rotation(key, live, usage['used_tokens'] if usage else None)
            requests = self.rows("SELECT id,claim_id,state,promise,promise_due FROM requests "
                                 "WHERE thread=? AND state IN ('claimed','done','escalated') ORDER BY id", (key,))
            old, task, origin, t3db = rotation['old'], info['task_id'], info['origin'], str(self.paths.t3)
            setup = (f'You replace thread {old} as the Toucan owner thread for {key}. Read README.md and the config.\n'
                     f'1. Create a webhook task bound to this thread with schedule_task, title "Toucan requests ({key})", '
                     'schedule {type:"webhook"}, bindToCurrentThread true, and a prompt identical to task '
                     f'{task} (read its prompt column read-only from {t3db}, table scheduled_tasks, open with ?mode=ro).\n'
                     '2. Read the new task\'s webhook_token read-only from the same table and write '
                     f'{origin}/api/hooks/<encodeURIComponent(task_id)>/<webhook_token> to {hook_file} '
                     'with umask 077 (mode 600), never printing the token or URL.\n'
                     f'3. Run `toucan rotate --thread {key} --check`.\n'
                     f'4. Delete the old task {task} with delete_scheduled_task.\n'
                     f'5. Start `toucan wait --thread {key}` as a background command.\n'
                     f'6. Run `toucan inbox --thread {key}` and carry on the open work in the brief, reusing the claim ids listed.')
            instructions = (f'Thread key: {key}\nOld thread: {old}\nHook file: {hook_file}\nWebhook task: {task}\n'
                            f'T3 database: {t3db}\nHook origin: {origin}\n\nOpen work:\n')
            instructions += '\n'.join(f'Request {r["id"]}: claim {r["claim_id"] or "none"}, state {r["state"]}, '
                                      f'promise {json.dumps(r["promise"])}, due {self.local(r["promise_due"]) if r["promise_due"] is not None else "none"}'
                                      for r in requests) or 'None'
            instructions += ('\n\na. Write a handoff brief for your successor inline (no files), under about 900 words, '
                             'with no secrets, tokens or hook URLs. Include people and chats and reply tone; environments, '
                             f"deploys and what needs {self.settings['owner_name']}'s OK; repo conventions; each open request with its id, claim id, "
                             'promise, real state and exact next step; unfinished work with no request; each result you owe '
                             'another T3 thread or are waiting on from one, with its thread link, what was asked, and that '
                             'the reply goes to that thread with t3_thread_send; recent history that '
                             f'matters; things {self.settings["owner_name"]} corrected or asked for.\n'
                             'b. Read your model selection and modes with t3_thread_configuration, then launch the successor '
                             'with t3_thread_launch: same project, workspaceStrategy {type:"root"}, the same modelSelection '
                             'including options, the same runtimeMode, your title, and as message the successor setup block '
                             'below followed by your brief.\n'
                             f'c. Wait for it with t3_thread_wait, then run `toucan rotate --thread {key} --check`. '
                             'When it says the rotation is done, stop your background `toucan wait`, do not start it again, '
                             'do not claim or reply to anything else, and end your turn. If the check fails, escalate nothing; '
                             'just report what failed in your final message.\n\nSuccessor setup block:\n' + setup)
            return dict(thread=key, old=old, hook_file=str(hook_file), task_id=task, t3_db=t3db, origin=origin,
                        requests=requests, successor_setup=setup, instructions=instructions)

    def forward(self, req, target, thread, reason='', claim=None):
        """Move a request to another owner thread, like a rate-limit handover. A claimed request
        needs its current claim and goes over unclaimed; the target is woken as for a new request."""
        if thread not in self.cfg['threads'] or target not in self.cfg['threads']:
            raise ValidationError('unknown thread')
        if target == thread:
            raise Refused('the request is already in that thread')
        with self.transaction():
            row = self.movable(req, thread, claim, 'forwarded')
            self.db.execute("UPDATE requests SET thread=?,state='new',claim_id=NULL,claimed_at=NULL,change_pending=0,"
                            "wakes=0,last_wake_at=NULL WHERE id=?", (target, req))
            self.record('forwarded', target, req, from_thread=thread, to_thread=target, reason=reason,
                        was_claimed=row['state'] == 'claimed')
        return dict(request=req, from_thread=thread, thread=target, state='new')

    def movable(self, req, thread, claim, verb):
        """The request, if `thread` may hand it on: new, or claimed with its current claim."""
        row = self.get(req, thread=thread)
        if row['routing']:
            raise Refused('request is still being routed')
        if row['state'] == 'claimed':
            if not claim or row['claim_id'] != claim:
                raise Refused('request is claimed; pass its current --claim, or release it first')
        elif row['state'] != 'new':
            raise Refused(f'only a new or claimed request can be {verb}, not one that is {row["state"]}')
        if self.db.execute("SELECT 1 FROM outbox WHERE request_id=? AND state IN ('pending','sending')", (req,)).fetchone():
            raise Refused('delivery in progress')
        return row

    def bounce(self, req, thread, reason, claim=None):
        """A thread says a request isn't its. On a live route it goes back to the router, which won't
        pick a thread that bounced it; after max_bounces it is escalated to the owner instead."""
        if thread not in self.cfg['threads']:
            raise ValidationError('unknown thread')
        with self.transaction():
            row = self.movable(req, thread, claim, 'bounced')
            route = router.route_of(self.cfg, row['route'])
            if not router.is_live(route):
                raise Refused(f"request {req}'s route does not use the live router, so there is nothing to bounce it back to; "
                              f'hand it to the right thread with `toucan forward {req} --to THREAD --thread {thread}`')
            count = self.db.execute("SELECT count(*) FROM events WHERE kind='bounced' AND request_id=?", (req,)).fetchone()[0] + 1
            self.record('bounced', thread, req, reason=reason, count=count, was_claimed=row['state'] == 'claimed')
            reset = "claim_id=NULL,claimed_at=NULL,change_pending=0,wakes=0,last_wake_at=NULL,route_reason=NULL"
            if count < self.settings['max_bounces']:
                self.db.execute(f"UPDATE requests SET thread=?,{reset},state='held',routing='pending',held_at=?,"
                                "route_gen=route_gen+1 WHERE id=?", (route['thread'], self.fx.now(), req))
                return dict(request=req, bounced=count, state='routing')
            # The route's thread holds the escalation, and gets the owner's answer back.
            claim_id = str(uuid.uuid4())
            self.db.execute(f"UPDATE requests SET thread=?,state='new',{reset} WHERE id=?", (route['thread'], req))
            self.db.execute("UPDATE requests SET state='claimed',claim_id=?,claimed_at=? WHERE id=?", (claim_id, self.fx.now(), req))
            threads = router.bounced(self.db, req)
        note = (f'No thread has taken this request: {", ".join(threads)} said it is not theirs (last reason: {reason}). '
                'Which thread should handle it?')
        try:
            self.deliver(req, claim_id, text=note, escalation=True)
        except Exception as error:
            with self.transaction():
                self.db.execute("UPDATE requests SET state='new',claim_id=NULL,claimed_at=NULL WHERE id=? AND claim_id=?", (req, claim_id))
                self.record('bounce_escalation_failed', route['thread'], req, error=type(error).__name__)
            raise RuntimeError(f'request {req} bounced {count} times; escalating it to the owner failed, '
                               f'so it is now a new request in {route["thread"]}') from None
        return dict(request=req, bounced=count, state='escalated', thread=route['thread'])

    def claim(self, ids, thread):
        with self.transaction():
            requests = [self.get(req) for req in ids]
            if not requests or any(r['thread'] != thread or r['state'] != 'new' or r['routing'] for r in requests):
                raise Refused('all requests must be new and belong to this thread')
            claim_id = str(uuid.uuid4())
            for req in requests:
                self.db.execute("UPDATE requests SET state='claimed',claim_id=?,claimed_at=? WHERE id=?", (claim_id, self.fx.now(), req['id']))
                self.record('claimed', thread, req['id'], claim_id=claim_id)
            return claim_id

    def resume(self, req, thread):
        """Hand an escalated request back when the owner answered in the owning T3 thread instead of on WhatsApp."""
        if thread not in self.cfg['threads']:
            raise ValidationError('unknown thread')
        with self.transaction():
            row = self.get(req, thread=thread)
            if row['state'] != 'escalated':
                raise Refused('only an escalated request can be resumed')
            asked = self.db.execute("SELECT max(t) FROM (SELECT max(created_at) AS t FROM outbox WHERE request_id=? "
                                    "AND kind='escalation' AND state='sent' UNION ALL SELECT max(ts) FROM events "
                                    "WHERE request_id=? AND kind IN ('card_updated','card_held'))", (req, req)).fetchone()[0]
            try:
                said = sources.owner_said(self.paths, self.live_thread(thread), asked)
                if said is None:
                    raise OSError()
            except (OSError, sqlite3.Error):
                raise Refused(f'T3 history unavailable; ask {self.settings["owner_name"]} to answer the escalation on WhatsApp') from None
            if not said:
                raise Refused(f'{self.settings["owner_name"]} has not written in this thread since the escalation; ask here or on WhatsApp')
            claim_id = str(uuid.uuid4())
            self.db.execute("UPDATE requests SET state='claimed',claim_id=?,claimed_at=?,done_at=NULL,change_pending=0,"
                            "wakes=0,last_wake_at=NULL WHERE id=?", (claim_id, self.fx.now(), req))
            self.db.execute('UPDATE cards SET answered_at=? WHERE request_id=? AND closed_at IS NULL AND answered_at IS NULL',
                            (self.fx.now(), req))
            self.record('owner_resumed', thread, req, claim_id=claim_id, t3_message=said[-1]['id'])
            return dict(claim_id=claim_id, owner_said=[m['text'] for m in said])

    def get(self, req, claim=None, thread=None):
        row = self.db.execute('SELECT * FROM requests WHERE id=?', (req,)).fetchone()
        if not row or (thread and row['thread'] != thread):
            raise Refused('request not found in this thread')
        if claim is not None and (row['claim_id'] != claim or row['state'] not in ('claimed', 'done')):
            raise Refused('current claim required on a claimed or done request')
        return dict(row)

    def transition(self, req, claim, action, note='', thread=None):
        if not claim:
            raise Refused('current claim required')
        with self.transaction():
            row = self.get(req, claim, thread)
            now = self.fx.now()
            if self.db.execute("SELECT 1 FROM outbox WHERE request_id=? AND state IN ('pending','sending')", (req,)).fetchone():
                raise Refused('delivery in progress')
            if action == 'release':
                self.db.execute("UPDATE requests SET state='new',claim_id=NULL,claimed_at=NULL,done_at=NULL,wakes=0,last_wake_at=NULL WHERE id=?", (req,))
            elif action == 'done':
                if row['state'] != 'claimed':
                    raise Refused('done requires a claimed request')
                self.db.execute("UPDATE requests SET state='done',done_at=?,done_note=? WHERE id=?", (now, note, req))
            elif action == 'close':
                self.db.execute("UPDATE requests SET state='closed',closed_at=?,close_reason=? WHERE id=?", (now, note, req))
            self.record(action, row['thread'], req)

    def react(self, req, claim, emoji, message=None, remove=False, thread=None):
        if not claim:
            raise Refused('current claim required')
        if not emoji.strip() or (message is not None and not message.strip()):
            raise ValidationError('message ID and emoji cannot be empty')
        with self.transaction():
            row = self.get(req, claim, thread)
            route = next((r for r in self.cfg['routes'] if r['name'] == row['route']), {})
            if not (row['reply_source'] == 'teams' and row['mode'] == 'act' and route.get('post_as_bot')):
                raise Refused('react requires a Teams act route with post_as_bot enabled')
            own = self.db.execute('SELECT msg_id FROM messages WHERE id=?', (row['message_id'],)).fetchone()['msg_id']
            message = own if message is None else message
            detail = dict(conversation_id=row['reply_chat'], message_id=message, emoji=emoji, removed=remove)
            # Parts of a split message share one reaction: the first adds it, the last to finish removes it.
            if remove and message == own and self.db.execute(
                    "SELECT 1 FROM requests WHERE message_id=? AND id!=? AND state IN ('new','held','claimed','done','escalated')",
                    (row['message_id'], req)).fetchone():
                self.record('reaction_kept', row['thread'], req, **detail)
                return dict(request=req, **detail, skipped='another part of this message is still open')
            if not remove and self.reacted(row['message_id'], detail):
                self.record('reaction_kept', row['thread'], req, **detail)
                return dict(request=req, **detail, skipped='already reacted')
            self.record('reaction_pending', row['thread'], req, **detail)
        argv = ['toucan-bot', '--json', 'react', '--conversation', row['reply_chat'], '--message', message, '--emoji', emoji]
        if remove:
            argv.append('--remove')
        try:
            result = self.fx.run(argv)
        except Exception:
            with self.transaction():
                self.record('reaction_uncertain', row['thread'], req, **detail)
            raise RuntimeError('toucan-bot interrupted; reaction uncertain') from None
        with self.transaction():
            self.record('reaction_failed' if result.returncode else 'reaction_removed' if remove else 'reaction_added',
                        row['thread'], req, **detail, **({'error': result.stderr[:2000]} if result.returncode else {}))
        if result.returncode:
            raise RuntimeError('toucan-bot react failed; inspect request log')
        return dict(request=req, **detail)

    def reacted(self, message_id, detail):
        """Whether the bot's reaction is already on that message, added for any request on the same message."""
        for e in self.db.execute("SELECT kind,detail_json FROM events WHERE kind IN ('reaction_added','reaction_removed') "
                                 'AND request_id IN (SELECT id FROM requests WHERE message_id=?) ORDER BY id DESC', (message_id,)):
            seen = json.loads(e['detail_json'])
            if all(seen.get(k) == detail[k] for k in ('conversation_id', 'message_id', 'emoji')):
                return e['kind'] == 'reaction_added'
        return False

    def send(self, route_name, thread, text='', file=None, caption='', chat=None, override=None):
        route = next((r for r in self.cfg['routes'] if r['name'] == route_name), None)
        if not route:
            raise ValidationError('unknown route')
        if thread != route['thread']:
            raise Refused('send requires the route owner thread')
        if route.get('reply_to'):
            chat = reply_phone(self.cfg, route)
        elif chat is None:
            if len(route['chats']) != 1:
                raise ValidationError('route has several chats; select one with --chat ID')
            chat = route['chats'][0]
        elif chat not in route['chats']:
            raise ValidationError('chat is not configured in this route')
        as_bot = route['source'] == 'teams' and route['mode'] == 'act' and route.get('post_as_bot')
        phone = (next((i for i in self.cfg['people'][route['requesters'][0]].get('whatsapp', []) if PHONE.fullmatch(i)), None)
                 if len(route['requesters']) == 1 else None)
        teams_direct = route['source'] == 'teams' and route['mode'] == 'act' and route.get('reply_via_whatsapp') and phone and not as_bot
        draft = route['mode'] == 'draft' or (route['source'] == 'teams' and not as_bot and not teams_direct)
        body = prefixed(route, caption if file else text)
        if draft:
            body = f'Draft for {route.get("label") or chat}:\n\n{body}'
        with self.preflight_log(), self.transaction():
            out = self._queue_delivery(None, thread, 'draft' if draft else 'send',
                                       'teams-bot' if as_bot else 'whatsapp',
                                       self.drafts_chat() if draft else phone if teams_direct else chat, body, file,
                                       override=override, route=route_name, chat=chat)
        return self._send_outbox(out, thread)

    def deliver(self, req, claim, text='', file=None, caption='', keep_open=False, escalation=False, thread=None,
                promise=None, due=None, card=None, override=None):
        if not claim:
            raise Refused('current claim required')
        with self.preflight_log(), self.transaction():
            row = self.get(req, claim, thread)
            message = dict(self.db.execute('SELECT * FROM messages WHERE id=?', (row['message_id'],)).fetchone())
            route = next((r for r in self.cfg['routes'] if r['name'] == row['route']), {})
            # On a post_as_bot route an act-mode Teams reply is posted in the chat as the Toucan bot.
            # WhatsApp delivery requires an explicit route opt-in.
            as_bot = row['reply_source'] == 'teams' and row['mode'] == 'act' and route.get('post_as_bot') and not escalation
            phone = next((i for i in self.cfg['people'][row['reply_person']].get('whatsapp', [])
                          if PHONE.fullmatch(i)), None)
            teams_direct = row['reply_source'] == 'teams' and row['mode'] == 'act' and route.get('reply_via_whatsapp', False) and phone and not as_bot
            draft = row['mode'] == 'draft' or (row['reply_source'] == 'teams' and not teams_direct and not as_bot)
            kind = 'escalation' if escalation else 'draft' if draft else 'send'
            recipient = (self.settings['owner_whatsapp'] if kind == 'escalation' else self.drafts_chat() if kind == 'draft'
                         else row['reply_chat'] if as_bot else phone if teams_direct else row['reply_chat'])
            # Ambiguous attempts must be resolved by a person before another send.
            if self.db.execute("SELECT 1 FROM outbox WHERE request_id=? AND state IN ('pending','sending','uncertain')", (req,)).fetchone():
                raise Refused('request already has an outstanding or uncertain delivery')
            name = self.cfg['people'][row['reply_person']]['name']
            if row['part_label'] and not escalation:
                # A request split from a bigger message says which project the reply is about.
                said = (caption if file else text) or ''
                if not said.lower().startswith(row['part_label'].lower() + ':'):
                    said = f'{row["part_label"]}: {said}' if said else row['part_label']
                caption, text = (said, text) if file else (caption, said)
            if kind == 'send' and promise and self.db.execute(
                    "SELECT 1 FROM requests r JOIN outbox o ON o.request_id=r.id WHERE r.message_id=? AND r.id!=? "
                    "AND o.kind='send' AND o.state IN ('pending','sending','sent','uncertain')", (row['message_id'], req)).fetchone():
                # One acknowledgement per message: another part of it already had a reply.
                self.db.execute('UPDATE requests SET promise=?,promise_due=? WHERE id=?', (promise, due, req))
                self.record('promise_not_sent', row['thread'], req, promise=promise, due=due)
                return dict(outbox=None, state='not_sent', promise=promise,
                            reason='another part of this message already had a reply, so this promise was recorded but not sent; '
                                   'send the result with --final when it is ready')
            body = prefixed(route, caption if file else text)
            card_row = None
            if escalation:
                body, card_row, outcome = self._card(row, name, row['part'] or message['text'] or '', text, card)
                if body is None:
                    return dict(outbox=None, state='escalated', card=self.card_label(req), sent=False, outcome=outcome,
                                reason=('same question as the open card, so the note was added to that card without a new '
                                        'message; the owner sees it in the daily list' if outcome == 'updated' else
                                        'held for the daily list, which shows the owner every card waiting on them'))
            elif draft:
                body = f'Draft for {route.get("label") or message["chat"]} → reply to {name}:\n\n{body}'
            if escalation and card is not None and card_row:
                project = self.escalation_project(row)
                if project and project.get('escalate_to') and self.fx.now() >= timestamp(project.get('escalate_from', '1970-01-01T00:00:00+00:00')):
                    person = self.cfg['people'][project['escalate_to']]
                    recipient = next(jid for jid in person['whatsapp'] if PHONE.fullmatch(jid))
                    wait = project.get('escalate_wait_minutes', 10)
                    body += (f'\nGoes to {self.settings["owner_name"]} if not answered in {wait} minutes; '
                             f'reply pass to send it to {self.settings["owner_name"]} now.')
                    self.db.execute('UPDATE cards SET delegate_jid=?,delegate_name=?,wait_minutes=? WHERE id=?',
                                    (recipient, person['name'], wait, card_row))
            out = self._queue_delivery(req, row['thread'], kind, 'teams-bot' if as_bot else 'whatsapp', recipient, body, file,
                                       override=override)
            if card_row:
                self.db.execute('UPDATE cards SET outbox_id=? WHERE id=?', (out, card_row))
        result = self._send_outbox(out, row['thread'], req, keep_open, promise, due)
        return dict(result, card=self.card_label(req)) if escalation else result

    def escalation_project(self, row):
        """Use a recorded live decision first, then thread metadata and fallback ancestry.
        Ambiguous multi-project decisions deliberately keep owner delivery.
        """
        projects = self.cfg['projects']
        decisions = self.rows("SELECT d.* FROM router_decisions d JOIN requests r ON r.id=d.request_id "
                              "WHERE r.message_id=? AND d.mode='live' ORDER BY d.id DESC", (row['message_id'],))
        for decision in decisions:
            parts = json.loads(decision['parts_json'] or '[]')
            keys = {p['project'] for p in parts if row['part'] and p['text'] in row['part']}
            if not keys and decision['request_id'] == row['id']:
                keys = set(json.loads(decision['projects_json'] or '[]'))
            if keys:
                return projects.get(next(iter(keys))) if len(keys) == 1 else None
        threads = self.cfg['threads']
        key = row['thread']
        project = threads.get(key, {}).get('project')
        if project in projects:
            return projects[project]
        candidates = set()
        for origin, data in threads.items():
            seen, current = set(), origin
            while current in threads and current not in seen:
                seen.add(current)
                if current == key:
                    candidate = data.get('project')
                    if candidate in projects:
                        candidates.add(candidate)
                    candidates.update(k for k, p in projects.items() if p['thread'] == origin)
                    break
                current = threads[current].get('fallback')
        if len(candidates) == 1:
            return projects[next(iter(candidates))]
        # A shared standby can still identify the original route's project.
        route = next((r for r in self.cfg['routes'] if r['name'] == row['route']), {})
        original = route.get('thread')
        candidate = threads.get(original, {}).get('project')
        if candidate in candidates:
            return projects[candidate]
        return None

    def forward_escalations(self, now):
        for card in self.rows("SELECT c.*,o.text FROM cards c JOIN outbox o ON o.id=c.outbox_id "
                              "JOIN requests r ON r.id=c.request_id WHERE c.delegate_jid IS NOT NULL "
                              "AND c.closed_at IS NULL AND c.answered_at IS NULL AND r.state!='closed' "
                              "AND c.forward_outbox IS NULL AND o.state='sent' "
                              "AND c.created_at+c.wait_minutes*60<=?", (now,)):
            self.forward_card(card, f'{card["delegate_name"]} did not answer within {card["wait_minutes"]} minutes.')

    def forward_card(self, card, reason):
        if card['forward_outbox'] is not None:
            return
        original = self.db.execute("SELECT text FROM outbox WHERE id=? AND state='sent'", (card['outbox_id'],)).fetchone()
        if original:
            out = self.queue_owner(original['text'] + '\n' + reason, card['request_id'])
            if out is not None:
                # Associate the owner copy durably so quotes work even if the process stops
                # after sending but before flush_notices records its convenience quote alias.
                self.db.execute('UPDATE outbox SET request_id=? WHERE id=?', (card['request_id'], out))
                self.db.execute('UPDATE cards SET forward_outbox=? WHERE id=?', (out, card['id']))

    def drafts_chat(self):
        chat = self.settings['drafts_whatsapp']
        return chat if PHONE.fullmatch(chat) or GROUP.fullmatch(chat) else self.settings['owner_whatsapp']

    def escalate(self, req, claim, category, want, question, options, pick, note='', thread=None):
        """The only way a thread escalates: a card that passes the gate in cards.validate."""
        want, question, options, index = cards.validate(category, want, question, options, pick)
        return self.deliver(req, claim, text=note, escalation=True, thread=thread,
                            card=dict(category=category, want=want, question=question, options=options, pick=index))

    def _card(self, row, name, asked, note, card):
        """Inside deliver's transaction: store the card and return (body, card id, outcome). Body is None
        when nothing is sent: the same question as the open card only adds the note to it, and a
        request handed back for re-triage keeps its new card for the daily list."""
        now = self.fx.now()
        if card is None:  # Toucan's own escalations, such as a request no thread would take
            want = cards.one_line(asked)
            card = dict(category=cards.ROUTING, want=want if len(want) <= 100 else want[:99] + '…',
                        question=cards.one_line(note), options=[], pick=None)
        last = self.db.execute("SELECT c.*,o.state AS out_state FROM cards c LEFT JOIN outbox o ON o.id=c.outbox_id "
                               'WHERE c.request_id=? AND c.closed_at IS NULL ORDER BY c.id DESC LIMIT 1', (row['id'],)).fetchone()
        live = bool(last and (last['held'] or last['out_state'] == 'sent'))
        if live and cards.same_question(last['question'], card['question']):
            updates = json.loads(last['updates_json'] or '[]') + [dict(at=self.local(now), note=note)]
            self.db.execute('UPDATE cards SET updates_json=?,answered_at=NULL,answered_by=NULL WHERE id=?', (json.dumps(updates), last['id']))
            self.db.execute("UPDATE requests SET state='escalated',claim_id=NULL WHERE id=?", (row['id'],))
            self.record('card_updated', row['thread'], row['id'], card=last['id'])
            return None, last['id'], 'updated'
        self.db.execute('UPDATE cards SET closed_at=? WHERE request_id=? AND closed_at IS NULL', (now, row['id']))
        retriage = self.db.execute('SELECT value FROM meta WHERE key=?', (f'retriage:{row["id"]}',)).fetchone()
        held = bool(retriage) and now - float(retriage['value']) < 3 * 86400
        self.db.execute('DELETE FROM meta WHERE key=?', (f'retriage:{row["id"]}',))
        card_row = insert(self.db, 'cards', request_id=row['id'], category=card['category'], want=card['want'],
                          question=card['question'], options_json=json.dumps(card['options']), pick=card['pick'],
                          detail=note, held=int(held), created_at=now)
        if held:
            self.db.execute("UPDATE requests SET state='escalated',claim_id=NULL WHERE id=?", (row['id'],))
            self.record('card_held', row['thread'], row['id'], card=card_row)
            return None, card_row, 'held'
        seq = self.db.execute('SELECT count(*) FROM cards WHERE request_id=? AND id<=?', (row['id'], card_row)).fetchone()[0]
        body = cards.render(row['id'], row['thread'], name, card['want'], card['question'], card['options'], card['pick'],
                            replaces=live and last['answered_at'] is None, seq=seq)
        return body, card_row, 'sent'

    def tags(self, body, source):
        """Swap `@[person]` for an Entra ID tag the bot turns into a Teams mention, or a plain name elsewhere."""
        def swap(match):
            key = match.group(1).strip()
            person = self.cfg['people'].get(key)
            if person is None and key == 'owner':
                person = dict(name=self.settings['owner_name'], teams_id=self.settings['owner_teams_id'])
            if person is None:
                raise ValidationError(f'unknown person in tag @[{key}]; use a person key from the config, or owner')
            if source != 'teams-bot':
                return person['name']
            if not person.get('teams_id'):
                raise ValidationError(f'@[{key}] has no teams_id, so Teams cannot tag them')
            return f'@[{person["teams_id"]}]'
        return TAG.sub(swap, body) if body else body

    def _queue_delivery(self, req, thread, kind, source, recipient, body, file, override=None, **detail):
        if file and not Path(file).is_file():
            raise ValidationError('attachment file does not exist')
        raw = body
        body = self.tags(body, source)
        if source == 'teams-bot' and file and not still_image(file):
            raise ValidationError('the Toucan bot posts text and still images (PNG, JPEG, GIF, WebP) only; send other files another way')
        if not recipient or (kind != 'send' and not PHONE.fullmatch(recipient) and not (kind == 'draft' and GROUP.fullmatch(recipient))):
            raise ValidationError('a valid owner_whatsapp phone JID is required')
        count = self.db.execute("SELECT count(*) FROM outbox WHERE to_chat=? AND state IN ('pending','sent','sending','uncertain') AND created_at>=?",
                                (recipient, self.fx.now() - 3600)).fetchone()[0]
        # Drafts and escalations go to the owner and are never held back.
        if kind == 'send' and count >= self.settings['rate_per_hour']:
            raise Refused('recipient hourly rate limit reached')
        # Judged on the text as written (person keys in tags), against the chat it goes to. The router's own
        # "which project?" question is exempt.
        checks, people = [], None
        if kind == 'send' and 'asks_request' not in detail and ledger.projects(self.cfg, thread):
            try:
                checks = ledger.preflight(self, thread, recipient, f'{raw or ""}\n{body or ""}', override)
            except ledger.Held as held:
                held.thread, held.request = thread, req
                raise
            try:
                people = ledger.addressed(self.cfg, recipient, raw or '', ledger.chat_people(self.cfg).get(recipient, ()))
            except Exception:
                people = None  # the timeline falls back to reading the stored text
        out = insert(self.db, 'outbox', attempt_id=str(uuid.uuid4()), request_id=req, kind=kind,
                     source=source, to_chat=recipient, text=body, file=str(file) if file else None,
                     state='pending', created_at=self.fx.now(), updated_at=self.fx.now())
        self.record('outbox_pending', thread, req, outbox=out, delivery_kind=kind,
                    **(dict(people=people) if people is not None else {}), **detail)
        for check in checks:
            self.record('preflight', thread, req, outbox=out, **check)
        return out

    def _send_outbox(self, out, thread, req=None, keep_open=False, promise=None, due=None):
        # Pending has committed; commit sending before the external effect too.
        with self.transaction():
            changed = self.db.execute("UPDATE outbox SET state='sending',updated_at=? WHERE id=? AND state='pending'", (self.fx.now(), out))
            if not changed.rowcount:
                raise Refused('delivery is no longer pending; inspect outbox before retrying')
            row = self.db.execute('SELECT * FROM outbox WHERE id=?', (out,)).fetchone()
        recipient, body, file, kind = (row[k] for k in ('to_chat', 'text', 'file', 'kind'))
        if row['source'] == 'teams-bot':
            argv = ['toucan-bot', '--json', 'send', '--conversation', recipient, '--text', body or '']
            argv += ['--image', str(file)] if file else []
        else:
            argv = ['wacli', '--json', 'send', 'file' if file else 'text', '--to', recipient]
            argv += ['--file', str(file)] + (['--caption', body] if body else []) if file else ['--message', body]
            argv += ['--store', str(self.paths.wa)]
        try:
            result = self.fx.run(argv)
        except Exception:
            # A timeout can happen after the recipient received the message.
            with self.transaction():
                self.db.execute("UPDATE outbox SET state='uncertain',error='sender interrupted',updated_at=? WHERE id=?", (self.fx.now(), out))
                self.record('delivery_uncertain', thread, req, outbox=out)
            raise RuntimeError('sender interrupted; delivery uncertain') from None
        with self.transaction():
            external = None
            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout)
                    data = data.get('data', data)
                    external = data.get('id') or data.get('msg_id') or data.get('MsgID') or data.get('activity_id')
                except (ValueError, AttributeError):
                    pass
            uncertain = row['source'] == 'teams-bot' and result.returncode == 4
            self.db.execute('UPDATE outbox SET state=?,error=?,external_id=?,updated_at=? WHERE id=?',
                            ('uncertain' if uncertain else 'failed' if result.returncode else 'sent', result.stderr[:2000] if result.returncode else None,
                             external, self.fx.now(), out))
            if result.returncode == 0 and req is not None:
                if promise and kind != 'escalation':
                    self.db.execute('UPDATE requests SET promise=?,promise_due=? WHERE id=?', (promise, due, req))
                if kind == 'escalation':
                    # Stays open: the owner's WhatsApp answer hands it back to the thread (owner_answers).
                    self.db.execute("UPDATE requests SET state='escalated',claim_id=NULL WHERE id=?", (req,))
                elif kind == 'draft':
                    self.db.execute("UPDATE requests SET state='done',done_at=? WHERE id=?", (self.fx.now(), req))
                elif not keep_open and not promise:
                    self.db.execute("UPDATE requests SET state='closed',closed_at=?,close_reason='replied' WHERE id=?", (self.fx.now(), req))
            self.record('delivery_uncertain' if uncertain else 'delivery_failed' if result.returncode else 'delivered', thread, req, outbox=out, delivery_kind=kind,
                        keep_open=keep_open, promise=promise)
        if result.returncode:
            raise RuntimeError('delivery uncertain; inspect outbox before sending again' if uncertain
                               else f'{argv[0]} failed; inspect outbox error')
        return dict(outbox=out, state='sent', external_id=external)

    def digest(self):
        """The once-a-day list for the owner: every card still waiting on them, and how many requests
        threads settled without them yesterday. Sends nothing when nothing is waiting."""
        now = self.fx.now()
        zone = ZoneInfo(self.settings['timezone'])
        today = datetime.fromtimestamp(now, zone).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = (today - timedelta(days=1)).timestamp(), today.timestamp()
        waiting = self.rows(f"SELECT c.*,{cards.SEQ},r.thread,r.reply_person FROM cards c JOIN requests r ON r.id=c.request_id "
                            "WHERE c.closed_at IS NULL AND c.answered_at IS NULL AND r.state='escalated' AND (c.held=1 OR "
                            "EXISTS (SELECT 1 FROM outbox o WHERE o.id=c.outbox_id AND o.state='sent')) ORDER BY c.created_at,c.id")
        legacy = self.rows("SELECT r.id,r.thread,r.reply_person FROM requests r WHERE r.state='escalated' "
                           'AND NOT EXISTS (SELECT 1 FROM cards c WHERE c.request_id=r.id AND c.closed_at IS NULL) ORDER BY r.id')
        handled = self.db.execute(
            "SELECT count(*) FROM requests r WHERE r.state='closed' AND r.closed_at>=? AND r.closed_at<? "
            "AND coalesce(r.close_reason,'') NOT IN ('owner replied','owner posted') "
            'AND NOT EXISTS (SELECT 1 FROM cards c WHERE c.request_id=r.id) '
            "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.request_id=r.id AND o.kind='escalation')", (start, end)).fetchone()[0]
        name = lambda key: self.cfg['people'].get(key, {}).get('name', 'someone')
        since = lambda ts: datetime.fromtimestamp(ts, zone).strftime('%-d %b %H:%M')
        report = dict(waiting=[cards.label(c) for c in waiting] + [cards.card_id(r['id']) for r in legacy],
                      handled_yesterday=handled, sent=False, text=None)
        if not report['waiting']:
            return report
        blocks = [cards.compact(c, c['thread'], name(c['reply_person']), since(c['created_at'])) for c in waiting]
        blocks += [f'*{cards.card_id(r["id"])}* {r["thread"]} · {name(r["reply_person"])}: older escalation; reply "more {cards.card_id(r["id"])}" to see it'
                   for r in legacy]
        first = report['waiting'][0]
        text = (f'*Waiting on you: {len(report["waiting"])}*\n\n' + '\n\n'.join(blocks) +
                f'\n\n{handled} handled without you yesterday.\nReply "{first} A" or "more {first}".')
        report['text'] = text
        if self.dry:
            return report
        with self.transaction():
            out = self._queue_delivery(None, None, 'escalation', 'whatsapp', self.settings['owner_whatsapp'], text, None)
        self._send_outbox(out, None)
        with self.transaction():
            # A held card now counts as delivered through this list, so later lists keep showing it.
            self.db.execute('UPDATE cards SET held=0,outbox_id=? WHERE held=1 AND closed_at IS NULL', (out,))
            self.record('digest', count=len(report['waiting']), handled=handled)
        report['sent'] = True
        return report

    def retriage(self):
        """Hand every escalation from before decision cards back to its thread. The thread settles
        it, or escalates it again as a card, which waits for the next daily list."""
        with self.transaction():
            rows = self.rows("SELECT r.id,r.thread FROM requests r WHERE r.state='escalated' "
                             'AND NOT EXISTS (SELECT 1 FROM cards c WHERE c.request_id=r.id) ORDER BY r.id')
            for r in rows:
                self.db.execute("UPDATE requests SET state='new',claim_id=NULL,claimed_at=NULL,done_at=NULL,change_pending=0,"
                                'wakes=0,last_wake_at=NULL WHERE id=?', (r['id'],))
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (f'retriage:{r["id"]}', str(self.fx.now())))
                self.record('retriaged', r['thread'], r['id'])
        return dict(requests=[r['id'] for r in rows], threads=sorted({r['thread'] for r in rows}))

    def inbox(self, thread):
        if thread not in self.cfg['threads']:
            raise ValidationError('unknown thread')
        # Chats this thread owns, plus those of requests handed to it while their owner was rate-limited.
        routes = ({r['name'] for r in self.cfg['routes'] if r['thread'] == thread} |
                  {r['route'] for r in self.rows("SELECT DISTINCT route FROM requests WHERE thread=? AND state!='closed'", (thread,))})
        marks = ','.join('?' for _ in routes) or 'NULL'
        with self.transaction():
            # A request another thread holds (forwarded or handed over) is context here, not this thread's work.
            # One row per message, or per request of this thread when it holds several parts of one message.
            # A request held by the router is nobody's yet, so its message is plain context.
            other = 'SELECT {} FROM requests o WHERE o.message_id=m.id AND o.thread!=? AND o.routing IS NULL ORDER BY o.id LIMIT 1'
            messages = self.rows(f'SELECT m.*,r.id AS request_id,r.state,r.promise,r.promise_due,r.part,r.part_label,r.route_reason,'
                                 f'({other.format("o.id")}) AS other_request,({other.format("o.thread")}) AS other_thread,'
                                 f'a.request_id AS answers_request FROM messages m '
                                 f'LEFT JOIN requests r ON r.message_id=m.id AND r.thread=? AND r.routing IS NULL '
                                 f'LEFT JOIN owner_answers a ON a.message_id=m.id LEFT JOIN requests ar ON ar.id=a.request_id '
                                 f'WHERE m.route IN ({marks}) AND (m.ts>=? OR (m.ts=0 AND m.first_seen>=?) OR ar.state IN {OPEN} '
                                 f'OR EXISTS (SELECT 1 FROM requests o WHERE o.message_id=m.id AND o.state IN {ACTIVE})) '
                                 f'ORDER BY m.chat,m.ts,m.id,r.id',
                                 (thread, thread, thread, *sorted(routes), self.fx.now() - self.settings['context_hours'] * 3600,
                                  self.fx.now() - self.settings['context_hours'] * 3600))
            # The thread has now seen any edit or owner answer, so `toucan wait` stops reporting it.
            self.db.execute(f'UPDATE requests SET change_pending=0 WHERE thread=? AND change_pending=1 AND state IN {OPEN} '
                            'AND routing IS NULL', (thread,))
            self.record('inbox', thread, count=len(messages))
        return messages

    def import_pending(self, route_name, since):
        route = next((r for r in self.cfg['routes'] if r['name'] == route_name), None)
        if not route:
            raise ValidationError('unknown route')
        # Import reads the archives directly, including before active_from and lookback.
        mapping = sources.lid_map(self.paths) if route['source'] == 'whatsapp' else {}
        chats = [route['chats']] if route['source'] == 'email' else route['chats']
        batch = [m for chat in chats for m in self.read_route(route, chat, since, [])]
        created = []
        with self.transaction():
            for m in batch:
                person = sources.person_for(self.cfg, m['source'], m['sender_raw'], mapping)
                if not m['ts'] or m['is_owner'] or m['deleted'] or person not in route['requesters']:
                    continue
                if any(reply['is_owner'] and not reply['deleted'] and reply['chat'] == m['chat'] and reply['ts'] > m['ts'] for reply in batch):
                    continue
                old = self.stored_message(m, route)
                m['sender_key'] = person
                m['id'] = old['id'] if old else insert(self.db, 'messages', **m, route=route_name, first_seen=self.fx.now(), last_seen=self.fx.now())
                if not self.db.execute('SELECT 1 FROM requests WHERE message_id=?', (m['id'],)).fetchone():
                    created.append(self.create_request(m, route, mapping))
        return created
