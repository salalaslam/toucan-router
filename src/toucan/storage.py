"""Owned state only; dry runs copy the existing database read-only into memory."""
import json
import os
import sqlite3
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, source TEXT, account TEXT, chat TEXT, msg_id TEXT, route TEXT,
 sender_key TEXT, sender_raw TEXT, sender_name TEXT, is_owner INTEGER, ts REAL, text TEXT,
 attachments_json TEXT, edited INTEGER, deleted INTEGER, first_seen REAL, last_seen REAL,
 deferred_cutoff REAL, mentions_json TEXT,
 UNIQUE(source,account,chat,msg_id));
CREATE TABLE IF NOT EXISTS requests (
 id INTEGER PRIMARY KEY, message_id INTEGER UNIQUE REFERENCES messages(id), route TEXT, thread TEXT,
 state TEXT CHECK(state IN ('new','claimed','done','closed','escalated')), claim_id TEXT,
 claimed_at REAL, done_at REAL, done_note TEXT, closed_at REAL, close_reason TEXT,
 reply_source TEXT, reply_chat TEXT, reply_person TEXT, mode TEXT, source_changed INTEGER DEFAULT 0,
 change_pending INTEGER DEFAULT 0, wakes INTEGER DEFAULT 0, last_wake_at REAL, created_at REAL,
 promise TEXT, promise_due REAL);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, attempt_id TEXT UNIQUE, request_id INTEGER REFERENCES requests(id),
 kind TEXT CHECK(kind IN ('send','draft','escalation')), source TEXT, to_chat TEXT, text TEXT, file TEXT,
 state TEXT CHECK(state IN ('pending','sending','sent','failed','uncertain')),
 error TEXT, created_at REAL, updated_at REAL, external_id TEXT);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, ts REAL, kind TEXT, thread TEXT, request_id INTEGER, detail_json TEXT);
CREATE TABLE IF NOT EXISTS owner_answers (
 message_id INTEGER PRIMARY KEY REFERENCES messages(id), request_id INTEGER REFERENCES requests(id));
CREATE TABLE IF NOT EXISTS transcripts (
 chat TEXT, msg_id TEXT, path TEXT, text TEXT, attempts INTEGER DEFAULT 0, updated_at REAL,
 PRIMARY KEY(chat,msg_id));
CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, text TEXT, since REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS request_thread ON requests(thread,state);
CREATE INDEX IF NOT EXISTS outbox_recipient ON outbox(to_chat,created_at);
'''


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=200')
    db.execute('BEGIN')  # one source snapshot for metadata, messages and quoted context
    return db


def state_path():
    return Path(os.environ.get('TOUCAN_DB', '~/.local/state/toucan/toucan.db')).expanduser()


def private_dir(path):
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            continue
        parent.chmod(0o700)


def account_warning(db, account):
    labels = [r['account'] for r in db.execute("SELECT DISTINCT account FROM messages WHERE source='whatsapp'")]
    if labels and account not in labels:
        return ('WhatsApp account differs from stored messages; set sources.whatsapp.account to the old label: '
                + ', '.join(json.dumps(label) for label in labels))
    return None


def connect(path=None, dry=False):
    path = Path(path).expanduser() if path else state_path()
    if dry:
        db = sqlite3.connect(':memory:', isolation_level=None)
        if path.exists():
            source = readonly(path)
            try:
                source.backup(db)
            finally:
                source.close()
    else:
        private_dir(path.parent)
        media = path.parent / 'media'
        private_dir(media)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
        db.execute('PRAGMA journal_mode=WAL')
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    migrate(db)
    return db


def migrate(db):
    db.execute("DELETE FROM meta WHERE key LIKE 'teams-ingestion:%'")
    for table, fields in (('messages', (('deferred_cutoff', 'REAL'), ('mentions_json', 'TEXT'))),
                          ('requests', (('promise', 'TEXT'), ('promise_due', 'REAL')))):
        have = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        for column, kind in fields:
            if column not in have:
                try:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
                except sqlite3.OperationalError as error:
                    if 'duplicate column' not in str(error):
                        raise  # another process added it first otherwise


def insert(db, table, **values):
    cols = ','.join(values)
    marks = ','.join('?' for _ in values)
    return db.execute(f'INSERT INTO {table} ({cols}) VALUES ({marks})', tuple(values.values())).lastrowid


def event(db, now, kind, thread=None, request=None, **detail):
    insert(db, 'events', ts=now, kind=kind, thread=thread, request_id=request,
           detail_json=json.dumps(detail))
