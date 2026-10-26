"""Metadata-only discovery. Source stores are always opened read-only."""
import json
import sqlite3
import subprocess
from pathlib import Path

from . import sources, t3
from .config import ValidationError
from .effects import Effects
from .storage import readonly


def routed(cfg, source):
    return {chat.lower() if source == 'email' else chat
            for route in cfg['routes'] if route['source'] == source for chat in route['chats']}


def whatsapp(cfg, paths, limit):
    chats = routed(cfg, 'whatsapp')
    try:
        aliases = sources.lid_map(paths)
    except (OSError, sqlite3.Error):
        aliases = {}
    db = readonly(paths.wa / 'wacli.db')
    try:
        rows = db.execute('SELECT chat_jid,MAX(ts) AS ts FROM messages GROUP BY chat_jid '
                          'ORDER BY ts DESC,chat_jid LIMIT ?', (limit,)).fetchall()
        result = []
        for row in rows:
            jid = row['chat_jid']
            name = db.execute('SELECT chat_name FROM messages WHERE chat_jid=? AND chat_name IS NOT NULL '
                              "AND chat_name!='' ORDER BY ts DESC,rowid DESC LIMIT 1", (jid,)).fetchone()
            canonical = aliases.get(jid, jid)
            lids = sorted(lid for lid, phone in aliases.items() if phone == canonical)
            result.append(dict(jid=jid, name=name['chat_name'] if name else '',
                               kind='group' if jid.endswith('@g.us') else 'person',
                               lid_aliases=lids if not jid.endswith('@g.us') else [], last_message_ts=row['ts'],
                               routed=bool(({jid, canonical} | set(lids)) & chats)))
        return result
    finally:
        db.close()


def teams(cfg, paths):
    sources.teams_store(paths)
    chats = routed(cfg, 'teams')
    db = readonly(paths.bot_db)
    try:
        rows = db.execute('SELECT c.id,c.type,c.name,MAX(COALESCE(m.ts,m.received_at)) AS ts '
                          'FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id '
                          'GROUP BY c.id ORDER BY COALESCE(ts,c.last_seen) DESC,c.id').fetchall()
        return [dict(conversation_id=row['id'], type=row['type'], name=row['name'] or '',
                     members=[dict(m) for m in db.execute('SELECT aad_id,name FROM members '
                                                         'WHERE conversation_id=? ORDER BY aad_id,name', (row['id'],))],
                     last_message_ts=row['ts'], routed=row['id'] in chats) for row in rows]
    finally:
        db.close()


def email(paths, effects, limit):
    command = ['notmuch']
    if paths.notmuch_config:
        command.append('--config=' + str(Path(paths.notmuch_config).expanduser()))

    def run(*args):
        try:
            result = effects.run([*command, *args])
            if result.returncode:
                raise ValueError()
            value = json.loads(result.stdout)
            if not isinstance(value, list):
                raise ValueError()
            return value
        except (OSError, subprocess.SubprocessError, TimeoutError, ValueError, AttributeError):
            raise RuntimeError('notmuch discovery failed; check the email index and configuration') from None

    ids = run('search', '--format=json', '--output=messages', '--sort=newest-first', f'--limit={limit}', '*')
    if not ids:
        return []
    if any(not isinstance(mid, str) for mid in ids):
        raise RuntimeError('notmuch returned unsupported discovery metadata')
    query = '(' + ' or '.join('id:' + sources.mail_term(mid.removeprefix('id:')) for mid in ids) + ')'
    result = {}
    for field, count in [('sender', 'sender_count'), ('recipients', 'recipient_count')]:
        for row in run('address', '--format=json', '--output=' + field, '--output=count', '--deduplicate=address', query):
            if not isinstance(row, dict) or not isinstance(row.get('address'), str) or type(row.get('count')) is not int:
                raise RuntimeError('notmuch returned unsupported address metadata')
            address = row['address'].lower()
            entry = result.setdefault(address, dict(address=address, name=row.get('name') or '', sender_count=0, recipient_count=0))
            entry[count] += row['count']
    return sorted(result.values(), key=lambda row: (-(row['sender_count'] + row['recipient_count']), row['address']))


def threads(paths):
    if not t3.probe(paths):
        raise RuntimeError('T3 database unavailable or schema unsupported; thread discovery disabled')
    db = readonly(paths.t3)
    try:
        table = 'orchestration_v2_projection_threads'
        columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}

        def column(*names):
            return next((f't."{name}"' for name in names if name in columns), 'NULL')

        def field(names, json_keys):
            value = column(*names)
            if value == 'NULL' and 'payload_json' in columns:
                return 'COALESCE(' + ','.join(f"json_extract(t.payload_json,'$.{key}')" for key in json_keys) + ',NULL)'
            return value

        title = field(('title', 'name'), ('title', 'name'))
        project = field(('project', 'project_id'), ('project', 'projectId', 'project_id'))
        activity = field(('last_activity', 'updated_at', 'last_activity_at'), ('updatedAt', 'lastActivityAt', 'createdAt'))
        rows = db.execute(f'SELECT t.thread_id,{title} AS title,{project} AS project,'
                          f'COALESCE({activity},(SELECT MAX(created_at) FROM orchestration_v2_projection_messages m '
                          f'WHERE m.thread_id=t.thread_id)) AS last_activity FROM {table} t '
                          'ORDER BY last_activity DESC,t.thread_id').fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def discover(kind, cfg, effects=None, paths=None, limit=50):
    if type(limit) is not int or limit <= 0:
        raise ValidationError('limit must be a positive integer')
    paths, effects = paths or sources.Paths.from_config(cfg), effects or Effects()
    try:
        if kind == 'whatsapp':
            return whatsapp(cfg, paths, limit)
        if kind == 'teams':
            return teams(cfg, paths)
        if kind == 'email':
            return email(paths, effects, limit)
        if kind == 'threads':
            return threads(paths)
    except (OSError, sqlite3.Error, ValueError):
        raise RuntimeError(f'{kind} store unavailable or unsupported; discovery could not read metadata') from None
    raise ValidationError('unknown discovery source')
