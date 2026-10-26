"""Resolve chat names from the bot store and optional router route aliases."""

from __future__ import annotations

import os
import sqlite3
import tomllib
from pathlib import Path

from . import config


class NotFound(Exception):
    pass


def load_chats() -> dict[str, dict]:
    path = Path(os.environ.get('TOUCAN_CONFIG', '~/.config/toucan/toucan.toml')).expanduser()
    try:
        with path.open('rb') as f:
            router = tomllib.load(f)
    except (OSError, ValueError):
        router = {}
    owner = config.load()['owner_teams_id'] or router.get('settings', {}).get('owner_teams_id', '')
    owner = owner.lower()
    chats = {}
    try:
        con = sqlite3.connect(config.db_path().resolve().as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            # Keep observations, confirmation and generation in one WAL snapshot.
            con.execute('BEGIN')
            for row in con.execute('SELECT id,type,name FROM conversations'):
                chat = chats[row['id']] = dict(row)
                try:
                    members = con.execute('SELECT aad_id,name FROM members WHERE conversation_id=?',
                                          (row['id'],)).fetchall()
                    markers = dict(con.execute('SELECT key,value FROM meta WHERE key IN (?,?)',
                                               ('roster:' + row['id'], 'roster_generation:' + row['id'])).fetchall())
                    confirmed = markers.get('roster:' + row['id'])
                    generation = markers.get('roster_generation:' + row['id'])
                    complete = bool(confirmed and (generation is None or confirmed == generation))
                except sqlite3.OperationalError:
                    members, complete = [], False
                others = [m['name'] for m in members if (m['aad_id'] or '').lower() != owner and m['name']]
                pair = (complete and owner and len(members) == 2 and all(m['aad_id'] for m in members) and
                        len({(m['aad_id'] or '').lower() for m in members}) == 2 and
                        any((m['aad_id'] or '').lower() == owner for m in members) and row['type'] != 'personal')
                chat['person_aliases'] = others if pair else []
                chat['member_names'] = others
                if pair:
                    chat['type'] = 'oneOnOne'
                    if not chat['name']:
                        chat['name'] = others[0] if others else ''
                chat['aliases'] = [row['name']] if row['name'] else []
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        pass
    for route in router.get('routes', []):
        if route.get('source') != 'teams':
            continue
        for id in route.get('chats', []):
            chat = chats.setdefault(id, dict(name='', type='groupChat', aliases=[]))
            chat['aliases'] += [route[k] for k in ('name', 'label') if isinstance(route.get(k), str) and route[k]]
    return chats


def _norm(s: str) -> str:
    return ' '.join(s.lower().split())


def label(chat: dict) -> str:
    return ' '.join((chat.get('name') or next(iter(chat.get('aliases', [])), '')).split())


def resolve(query: str, bot_ids: list[str], chats: dict[str, dict]) -> str:
    """A person means the owner's human 1:1. Bot personal chats require an explicit ID."""
    if query in bot_ids:
        return query
    q = _norm(query)

    # Resolve people across all known chats first. A topic or route alias must
    # never redirect a person's name to a group, including when their 1:1 is absent.
    people = [(id, c) for id, c in chats.items() if c.get('type') != 'personal' and q and
              any(q in _norm(n) for n in c.get('person_aliases', []))]
    person_name = people or any(q and q in _norm(n) for c in chats.values() for n in c.get('member_names', []))

    def matches(ids):
        if person_name:
            return [(id, c) for id, c in people if id in ids]
        found = [(id, chats[id]) for id in ids if id in chats and chats[id].get('type') != 'personal' and q and
                 any(q in _norm(name) for name in chats[id].get('aliases', [label(chats[id])]))]
        exact = [(id, c) for id, c in found if any(_norm(n) == q for n in c.get('aliases', [label(c)]))]
        return exact or found

    found = matches(bot_ids)
    if len(found) == 1:
        return found[0][0]
    if found:
        options = '\n'.join(f"  {label(c)} ({c.get('type')}): {id}" for id, c in found)
        raise NotFound(f"'{query}' matches several chats; pass one ID with --conversation:\n{options}")
    elsewhere = matches(list(chats))
    if elsewhere:
        labels = ', '.join(sorted({label(c) for _, c in elsewhere}))
        raise NotFound(f'Toucan is not in the chat with {labels}. Add Toucan to that chat in Teams, '
                       'or send the message yourself.')
    raise NotFound(f"no Teams chat matches '{query}' (names come from complete rosters and router routes; bot personal chats require --conversation)")
