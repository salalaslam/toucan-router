"""SQLite record of what the bot hears and sends. Toucan reads it read-only."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path

from . import media

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  type TEXT,
  name TEXT,
  service_url TEXT NOT NULL,
  tenant_id TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  conversation_id TEXT NOT NULL,
  id TEXT NOT NULL,
  ts TEXT,
  sender_aad_id TEXT,
  sender_name TEXT,
  text TEXT NOT NULL,
  mentioned INTEGER NOT NULL,
  reply_to_id TEXT,
  received_at TEXT NOT NULL,
  raw_html TEXT,
  attachments_json TEXT NOT NULL DEFAULT '[]',
  quoted_json TEXT NOT NULL DEFAULT '[]',
  edited_at TEXT,
  deleted INTEGER NOT NULL DEFAULT 0,
  deleted_at TEXT,
  restored_at TEXT,
  modified_at TEXT,
  sender_app_id TEXT,
  live_seen_at TEXT,
  graph_seen_at TEXT,
  first_source TEXT NOT NULL DEFAULT 'live',
  change_seq INTEGER NOT NULL DEFAULT 0,
  mentions_json TEXT,
  PRIMARY KEY (conversation_id, id)
);
CREATE TABLE IF NOT EXISTS catchup (
  conversation_id TEXT PRIMARY KEY,
  cursor TEXT,
  last_success TEXT,
  last_error TEXT,
  http_status INTEGER,
  next_url TEXT,
  upper_bound TEXT,
  retry_at REAL,
  last_request_at REAL,
  auth_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_media (
  conversation_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  source TEXT NOT NULL,
  url TEXT NOT NULL,
  attachment_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt REAL NOT NULL DEFAULT 0,
  claim_id TEXT,
  claim_until REAL,
  PRIMARY KEY (conversation_id,message_id,source,url)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members (
  conversation_id TEXT NOT NULL,
  id TEXT NOT NULL,
  aad_id TEXT,
  name TEXT,
  PRIMARY KEY (conversation_id, id)
);
CREATE TABLE IF NOT EXISTS sent (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  activity_id TEXT,
  text TEXT NOT NULL,
  sent_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path, *, timeout=5) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Pre-create privately: SQLite also uses the database mode for WAL/SHM files.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    for suffix in ('-wal', '-shm'):
        sidecar = path.with_name(path.name + suffix)
        try:
            sidecar.chmod(0o600)
        except FileNotFoundError:
            pass
    con = sqlite3.connect(path, timeout=timeout)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    had_members = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='members'").fetchone()
    had_messages = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
    con.executescript(SCHEMA)
    con.execute('BEGIN IMMEDIATE')
    columns = {row['name'] for row in con.execute('PRAGMA table_info(messages)')}
    reset_changes = not had_messages or 'change_seq' not in columns
    had_mentions = 'mentions_json' in columns
    for name, definition in dict(raw_html='TEXT', attachments_json="TEXT NOT NULL DEFAULT '[]'",
                                 quoted_json="TEXT NOT NULL DEFAULT '[]'", edited_at='TEXT',
                                 deleted='INTEGER NOT NULL DEFAULT 0', deleted_at='TEXT',
                                 restored_at='TEXT', modified_at='TEXT', sender_app_id='TEXT',
                                 live_seen_at='TEXT', graph_seen_at='TEXT',
                                 first_source="TEXT NOT NULL DEFAULT 'live'",
                                 change_seq='INTEGER NOT NULL DEFAULT 0', mentions_json='TEXT').items():
        if name not in columns:
            con.execute(f'ALTER TABLE messages ADD COLUMN {name} {definition}')
    if 'raw_html' not in columns:
        for row in con.execute('SELECT conversation_id,id,text FROM messages').fetchall():
            con.execute('UPDATE messages SET raw_html=?,text=? WHERE conversation_id=? AND id=?',
                        (row['text'], plain_text(row['text']), row['conversation_id'], row['id']))
    if next(row for row in con.execute('PRAGMA table_info(messages)') if row['name'] == 'ts')['notnull']:
        reset_changes = True
        # Existing stores required a timestamp, even for an edit received before its original.
        definition = SCHEMA.split('CREATE TABLE IF NOT EXISTS messages (', 1)[1].split(';', 1)[0]
        con.execute('CREATE TABLE messages_new (' + definition)
        names = ','.join(row['name'] for row in con.execute('PRAGMA table_info(messages)'))
        con.execute(f'INSERT INTO messages_new ({names}) SELECT {names} FROM messages')
        con.execute('DROP TABLE messages')
        con.execute('ALTER TABLE messages_new RENAME TO messages')
    con.execute('UPDATE messages SET sender_aad_id=lower(sender_aad_id) WHERE sender_aad_id!=lower(sender_aad_id)')
    con.execute('UPDATE conversations SET tenant_id=lower(tenant_id) WHERE tenant_id!=lower(tenant_id)')
    con.execute('UPDATE members SET aad_id=lower(aad_id) WHERE aad_id!=lower(aad_id)')
    if not had_members or not con.execute('SELECT 1 FROM members LIMIT 1').fetchone():
        # Bot senders have no Entra user ID; only backfill human senders.
        con.execute("""INSERT OR IGNORE INTO members (conversation_id,id,aad_id,name)
                       SELECT conversation_id,lower(sender_aad_id),lower(sender_aad_id),sender_name
                       FROM messages WHERE sender_aad_id IS NOT NULL AND sender_aad_id != ''
                       ORDER BY received_at DESC""")
    if 'live_seen_at' not in columns:
        con.execute('UPDATE messages SET live_seen_at=received_at')
        con.execute('UPDATE messages SET modified_at=COALESCE(deleted_at,edited_at,ts)')
    columns = {row['name'] for row in con.execute('PRAGMA table_info(catchup)')}
    for name, kind in dict(next_url='TEXT', upper_bound='TEXT', retry_at='REAL', last_request_at='REAL',
                           auth_failures='INTEGER NOT NULL DEFAULT 0').items():
        if name not in columns:
            con.execute(f'ALTER TABLE catchup ADD COLUMN {name} {kind}')
    columns = {row['name'] for row in con.execute('PRAGMA table_info(pending_media)')}
    for name, kind in dict(claim_id='TEXT', claim_until='REAL').items():
        if name not in columns:
            con.execute(f'ALTER TABLE pending_media ADD COLUMN {name} {kind}')
    tracking = dict(con.execute("SELECT key,value FROM meta WHERE key IN ('store_generation','change_sequence')"))
    if reset_changes or len(tracking) != 2:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('store_generation',?)", (uuid.uuid4().hex,))
        # A new generation makes every retained message discoverable. Never use
        # SQLite rowids as durable progress, since a rebuild can renumber them.
        rows = con.execute('SELECT conversation_id,id FROM messages ORDER BY conversation_id,id').fetchall()
        for seq, row in enumerate(rows, 1):
            con.execute('UPDATE messages SET change_seq=? WHERE conversation_id=? AND id=?', (seq, *row))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('change_sequence',?)", (str(len(rows)),))
    fields = ('ts', 'sender_aad_id', 'sender_name', 'sender_app_id', 'text', 'raw_html', 'mentioned',
              'reply_to_id', 'attachments_json', 'quoted_json', 'edited_at', 'deleted', 'deleted_at',
              'restored_at', 'modified_at', 'mentions_json')
    changed = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields)
    # A parent's text can supply a reply's displayed quote. Publish that
    # dependency in the same change instead of keeping tracked-row sync reads.
    for operation, condition in [('INSERT', ''), ('UPDATE', 'WHEN ' + changed)]:
        existing = con.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                               ('message_change_' + operation,)).fetchone()
        if existing and condition and any(f'NEW.{field}' not in existing[0] for field in fields):
            con.execute(f'DROP TRIGGER message_change_{operation}')  # a newly tracked field
        con.execute(f'''CREATE TRIGGER IF NOT EXISTS message_change_{operation} AFTER {operation} ON messages {condition}
                        BEGIN
                          UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='change_sequence';
                          UPDATE messages SET change_seq=CAST((SELECT value FROM meta WHERE key='change_sequence') AS INTEGER)
                          WHERE conversation_id=NEW.conversation_id AND (id=NEW.id OR reply_to_id=NEW.id);
                        END''')
    if not had_mentions:
        # Earlier Graph rows kept raw mentions in quoted_json; live rows stay unknown (NULL).
        for row in con.execute("SELECT conversation_id,id,quoted_json FROM messages WHERE quoted_json LIKE '%mentionText%'").fetchall():
            found = graph_mentions([q for q in json.loads(row['quoted_json']) if isinstance(q, dict) and 'mentioned' in q])
            if found:
                con.execute('UPDATE messages SET mentions_json=? WHERE conversation_id=? AND id=?',
                            (encode_mentions(found), row['conversation_id'], row['id']))
    con.execute('CREATE INDEX IF NOT EXISTS message_changes ON messages(conversation_id,change_seq)')
    con.execute('CREATE INDEX IF NOT EXISTS message_replies ON messages(conversation_id,reply_to_id)')
    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '4')")
    con.commit()
    path.chmod(0o600)
    return con


def upsert_conversation(con, *, id, type, name, service_url, tenant_id) -> None:
    t = now()
    con.execute(
        """INSERT INTO conversations (id, type, name, service_url, tenant_id, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             type = COALESCE(excluded.type, type),
             name = COALESCE(excluded.name, name),
             service_url = COALESCE(NULLIF(excluded.service_url, ''), service_url),
             tenant_id = COALESCE(excluded.tenant_id, tenant_id),
             last_seen = excluded.last_seen""",
        (id, type, name, service_url, tenant_id.lower() if tenant_id else None, t, t),
    )
    con.commit()


def heartbeat(con) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES ('heartbeat', ?)", (now(),))
    con.commit()


def invalidate_roster(con, chat):
    """Advance this chat's generation and withdraw person aliases atomically."""
    key = 'roster_generation:' + chat
    with con:
        con.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1',
                    (key, '1'))
        con.execute('DELETE FROM meta WHERE key=?', ('roster:' + chat,))
        return con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]


def remember_member(con, conversation_id, member) -> None:
    con.execute("""INSERT INTO members VALUES (?, ?, ?, ?) ON CONFLICT(conversation_id,id)
                   DO UPDATE SET aad_id=COALESCE(excluded.aad_id,aad_id), name=COALESCE(excluded.name,name)""",
                (conversation_id, member.id, (getattr(member, 'aad_object_id', None) or '').lower() or None, member.name))
    con.commit()


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'attachment'):
            self.skip += 1
        elif not self.skip:
            if tag in ('br', 'p', 'div', 'li', 'tr', 'blockquote'):
                self.parts.append('\n')
            if tag == 'img':
                self.parts.append(dict(attrs).get('alt', '[image]'))
            if tag == 'a' and dict(attrs).get('href'):
                self.parts.append(dict(attrs)['href'] + ' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'attachment'):
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in ('p', 'div', 'li', 'tr', 'blockquote'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_text(markup):
    parser = Text()
    parser.feed(markup)
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else float('-inf')


def merge_attachments(old, new):
    """Keep Graph file enrichment and downloaded live images across duplicate deliveries."""
    result = json.loads(old)
    for item in json.loads(new):
        match = next((a for a in result if a == item or
                      (item.get('id') and a.get('id') == item['id']) or
                      (item.get('contentUrl') and a.get('contentUrl') == item['contentUrl']) or
                      (not item.get('id') and not item.get('contentUrl') and
                       not a.get('id') and not a.get('contentUrl') and
                       a.get('contentType') == item.get('contentType') and
                       a.get('content') == item.get('content') and a.get('name') == item.get('name'))), None)
        if match is None:
            result.append(item)
        else:
            local = {k: match[k] for k in ('local_path', 'download_status', 'http_status') if k in match}
            match.update(item)
            if local.get('local_path'):
                match.update(local)
    return json.dumps(result)


def graph_mentions(items):
    """Normalize Graph chatMessage mentions to the stored shape: text, kind and ID."""
    result = []
    for item in items or []:
        to = item.get('mentioned') or {}
        kind = next((k for k in ('application', 'conversation', 'tag', 'user') if to.get(k)), 'user')
        ident = (to.get(kind) or {}).get('id')
        result.append(dict(text=item.get('mentionText') or (to.get(kind) or {}).get('displayName'), kind=kind,
                           id=(ident or '').lower() or None if kind == 'user' else ident))
    return result


def encode_mentions(items):
    """Teams may split one name into several adjacent mentions of the same account; join them."""
    result = []
    for item in items:
        if result and item['id'] and (result[-1]['kind'], result[-1]['id']) == (item['kind'], item['id']):
            result[-1]['text'] = ' '.join(t for t in (result[-1]['text'], item['text']) if t)
        else:
            result.append(dict(item))
    return json.dumps(result)


def merge_mentions(old, new, same_content):
    """None means the delivery carried no mention data. A duplicate keeps the better-resolved list."""
    if new is None or (same_content and old is not None and
                       sum(bool(m['id']) for m in json.loads(new)) <= sum(bool(m['id']) for m in json.loads(old))):
        return old
    return new


def record_message(con, *, conversation_id, id, ts, sender_aad_id, sender_name, text, mentioned,
                   reply_to_id, raw_html=None, attachments_json='[]', quoted_json='[]', edited_at=None,
                   deleted=False, deleted_at=None, restored=False, modified_at=None,
                   sender_app_id=None, source='live', content_present=True, mentions_json=None) -> bool:
    """Merge the same (chat, message ID) from push and Graph without replaying old state.

    Only an explicit undelete or a newer Graph non-deleted snapshot clears a tombstone.
    A live edit alone never proves restoration.
    """
    with con:
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT * FROM messages WHERE conversation_id=? AND id=?', (conversation_id, id)).fetchone()
        seen = now()
        revision = modified_at or deleted_at or edited_at or ts
        if old:
            values = dict(old)
            values[source + '_seen_at'] = values[source + '_seen_at'] or seen
            # Graph creation is authoritative even when its content snapshot is older.
            values['ts'] = ts if source == 'graph' and ts else values['ts'] or ts
            # A tombstone/update may arrive without the original sender.
            values['sender_aad_id'] = values['sender_aad_id'] or (sender_aad_id or '').lower() or None
            values['sender_app_id'] = values['sender_app_id'] or sender_app_id
            values['sender_name'] = values['sender_name'] or sender_name
            same_content = instant(edited_at or ts) == instant(old['edited_at'] or old['ts'])
            if not deleted and not old['deleted'] and same_content:
                values['attachments_json'] = merge_attachments(old['attachments_json'], attachments_json)
            newer = instant(revision) >= instant(old['modified_at'])
            if deleted and instant(revision) > instant(old['restored_at']) and newer:
                values.update(deleted=1, deleted_at=deleted_at or revision, modified_at=revision)
            elif not deleted and newer:
                clear = (restored or source == 'graph') and instant(revision) > instant(old['deleted_at'])
                if old['deleted'] and clear:
                    values.update(deleted=0, restored_at=revision)
                if not values['deleted'] and content_present:
                    # A new content revision replaces removed attachments; duplicates enrich them.
                    attachments = (merge_attachments(old['attachments_json'], attachments_json)
                                   if same_content else attachments_json)
                    values.update(text=text, raw_html=raw_html, attachments_json=attachments,
                                  quoted_json=quoted_json if quoted_json != '[]' or not same_content else old['quoted_json'],
                                  reply_to_id=reply_to_id or old['reply_to_id'],
                                  edited_at=edited_at or old['edited_at'], modified_at=revision,
                                  mentioned=int(bool(mentioned or old['mentioned'])),
                                  mentions_json=merge_mentions(old['mentions_json'], mentions_json, same_content))
                if restored and not values['deleted']:
                    values.update(restored_at=revision, modified_at=revision)
            assignments = ','.join(k + '=?' for k in values if k not in ('conversation_id', 'id'))
            con.execute(f'UPDATE messages SET {assignments} WHERE conversation_id=? AND id=?',
                        (*[v for k, v in values.items() if k not in ('conversation_id', 'id')], conversation_id, id))
        else:
            values = dict(conversation_id=conversation_id, id=id, ts=ts,
                          sender_aad_id=sender_aad_id.lower() if sender_aad_id else None, sender_name=sender_name,
                          text=text, mentioned=int(mentioned), reply_to_id=reply_to_id, received_at=seen,
                          raw_html=raw_html, attachments_json=attachments_json, quoted_json=quoted_json,
                          mentions_json=mentions_json, edited_at=edited_at, deleted=int(deleted), deleted_at=deleted_at,
                          restored_at=revision if restored else None, modified_at=revision, sender_app_id=sender_app_id,
                          live_seen_at=seen if source == 'live' else None,
                          graph_seen_at=seen if source == 'graph' else None, first_source=source)
            con.execute(f"INSERT INTO messages ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
        # A committed message always has durable work for every incomplete image,
        # including Connector attachments retained when Graph enriches the row.
        for transport in ('live', 'graph'):
            media.queue(con, conversation_id, id, transport)
        return old is None


def catchup_result(con, chat, *, cursor=None, error=None, status=None):
    con.execute("""INSERT INTO catchup (conversation_id,cursor,last_success,last_error,http_status) VALUES (?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
                   cursor=COALESCE(excluded.cursor,cursor),
                   last_success=COALESCE(excluded.last_success,last_success),
                   last_error=excluded.last_error,http_status=excluded.http_status""",
                (chat, cursor, now() if error is None else None, error, status))
    con.commit()


def coverage(con, at=None):
    at = at or datetime.now(timezone.utc).timestamp()
    rows = con.execute("""SELECT c.id,s.cursor,s.last_success,s.last_error,s.http_status,s.retry_at
                          FROM conversations c LEFT JOIN catchup s ON c.id=s.conversation_id
                          WHERE c.type IS NULL OR c.type!='personal'""").fetchall()
    chats = [dict(r) | {'last_success_age_seconds': at - instant(r['last_success'])
                       if r['last_success'] else None} for r in rows]
    missing = con.execute("""SELECT count(*),sum(CASE WHEN sender_app_id IS NULL AND mentioned=0 THEN 1 ELSE 0 END)
                            FROM messages WHERE first_source='graph' AND live_seen_at IS NULL
                            AND julianday(ts)>=julianday(?, 'unixepoch','-1 day')
                            AND julianday(graph_seen_at)<=julianday(?, 'unixepoch','-5 minutes')""", (at, at)).fetchone()
    interval = con.execute("SELECT value FROM meta WHERE key='catchup_interval_seconds'").fetchone()
    seconds = int(interval[0]) if interval else 120
    for chat in chats:
        age = chat['last_success_age_seconds']
        chat['stale'] = age is None or age < -60 or age > max(300, seconds * 3)
    failed = sum(bool(c['last_error'] or c['stale']) for c in chats)
    roster_errors = {r['key'].removeprefix('roster_error:'): r['value']
                     for r in con.execute("SELECT key,value FROM meta WHERE key LIKE 'roster_error:%'")}
    return dict(catchup=dict(interval_seconds=seconds, chats=chats, failing_chats=failed, ok=not failed),
                delivery=dict(window_hours=24, grace_seconds=300, graph_only=missing[0],
                              ordinary_graph_only=missing[1] or 0), background=background(con, at),
                roster=dict(errors=roster_errors, ok=not roster_errors))


def background(con, at):
    tasks = {r['key'].removeprefix('task:'): json.loads(r['value'])
             for r in con.execute("SELECT key,value FROM meta WHERE key LIKE 'task:%'")}
    for task in tasks.values():
        age = at - instant(task['last_success']) if task['last_success'] else None
        task['last_success_age_seconds'] = age
        task['stale'] = age is None or age < -60 or age > max(300, task['interval_seconds'] * 3)
    return dict(tasks=tasks, ok=bool(tasks) and all(not t['last_error'] and not t['stale'] for t in tasks.values()))


def record_sent(con, *, conversation_id, activity_id, text) -> None:
    con.execute(
        "INSERT INTO sent (conversation_id, activity_id, text, sent_at) VALUES (?, ?, ?, ?)",
        (conversation_id, activity_id, text, now()),
    )
    con.commit()


def get_conversation(con, conversation_id: str):
    return con.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()


def conversations(con):
    return con.execute("SELECT * FROM conversations ORDER BY last_seen DESC").fetchall()


def recent_messages(con, limit: int = 20):
    return con.execute("SELECT * FROM messages ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
