"""Reconcile installed chats through Graph using only the bot's application identity."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlencode, urlsplit

import httpx

from . import media, store

GRAPH = 'https://graph.microsoft.com/v1.0'


def message_base(chat, message):
    return f'{GRAPH}/chats/{quote(chat, safe="")}/messages/{quote(message, safe="")}'


def normalise(message, bot_id):
    sender = message.get('from') or {}
    user, app = sender.get('user') or {}, sender.get('application') or {}
    body = message.get('body') or {}
    raw = body.get('content') or ''
    # Keep cards, file references, quotes and forwards intact in the same JSON shape as push.
    attachments = message.get('attachments') or []
    mentions = message.get('mentions') or []
    # The prior Graph-backed router passed m['id'] directly to Connector reactions.
    # Keep that per-chat ID contract: activity.id and Graph id deduplicate one row.
    return dict(id=message['id'], ts=message.get('createdDateTime'),
                sender_aad_id=user.get('id') if not app else None,
                sender_app_id=app.get('id'), sender_name=app.get('displayName') or user.get('displayName'),
                text=store.plain_text(raw) if body.get('contentType') == 'html' else raw, raw_html=raw,
                mentioned=any((m.get('mentioned', {}).get('application') or {}).get('id') == bot_id for m in mentions),
                reply_to_id=message.get('replyToId'), attachments_json=json.dumps(attachments),
                mentions_json=store.encode_mentions(store.graph_mentions(mentions)),
                edited_at=message.get('lastEditedDateTime'),
                deleted=bool(message.get('deletedDateTime')), deleted_at=message.get('deletedDateTime'),
                modified_at=message.get('lastModifiedDateTime') or message.get('createdDateTime'), source='graph')


async def sync_chat(con, client, token, chat, settings, bot_id):
    state = con.execute('SELECT * FROM catchup WHERE conversation_id=?', (chat,)).fetchone()
    if state and state['retry_at'] and state['retry_at'] > time.time():
        return
    base = f'{GRAPH}/chats/{quote(chat, safe="")}/messages'
    if state and state['next_url']:
        url, upper = state['next_url'], state['upper_bound']
    else:
        upper = datetime.now(timezone.utc).isoformat()
        lower = (datetime.fromisoformat(state['cursor'].replace('Z', '+00:00')) - timedelta(seconds=1)
                 if state and state['cursor'] else datetime.fromisoformat(upper) - timedelta(days=settings['catchup_lookback_days']))
        url = base + '?' + urlencode({'$top': 50, '$orderby': 'lastModifiedDateTime desc',
                                      '$filter': f'lastModifiedDateTime gt {lower.isoformat()} and lastModifiedDateTime lt {upper}'})
        con.execute('INSERT INTO catchup (conversation_id,next_url,upper_bound) VALUES (?,?,?) '
                    'ON CONFLICT(conversation_id) DO UPDATE SET next_url=excluded.next_url,upper_bound=excluded.upper_bound',
                    (chat, url, upper))
        con.commit()
    seen_pages = set()
    while url:
        parts = urlsplit(url)
        # Don't send Graph tokens to a nextLink outside this chat's message endpoint.
        if (parts.scheme != 'https' or parts.netloc != 'graph.microsoft.com' or parts.fragment or
                unquote(parts.path) != unquote(urlsplit(base).path) or url in seen_pages):
            raise ValueError('invalid Graph paging URL')
        seen_pages.add(url)
        for attempt in range(4):
            state = con.execute('SELECT last_request_at,retry_at FROM catchup WHERE conversation_id=?', (chat,)).fetchone()
            due = max((state['last_request_at'] or 0) + 1, state['retry_at'] or 0)
            if due > time.time():
                await asyncio.sleep(due - time.time())  # cancellation propagates, retaining this page
            con.execute('UPDATE catchup SET last_request_at=?,retry_at=NULL WHERE conversation_id=?', (time.time(), chat))
            con.commit()
            try:
                response = await client.get(url, headers={'Authorization': 'Bearer ' + token})
                response.raise_for_status()
                break
            except (httpx.HTTPStatusError, httpx.TransportError) as error:
                response = getattr(error, 'response', None)
                status = response.status_code if response is not None else None
                transient = status in (429, 503) or isinstance(error, httpx.TransportError)
                if transient:
                    delay = media.retry_delay(response, attempt)
                    con.execute('UPDATE catchup SET retry_at=? WHERE conversation_id=?', (time.time() + delay, chat))
                    con.commit()
                # Bound each pass. Long Retry-After values defer this chat without
                # blocking all other chats. The persisted next_url stays unchanged.
                if not transient or attempt == 3 or delay > 30:
                    raise
        page = response.json()
        for message in page['value']:
            values = normalise(message, bot_id)
            if message.get('messageType', 'message') != 'message':
                values['sender_aad_id'] = None
            attachments = json.loads(values['attachments_json'])
            if not values['deleted']:
                attachments += media.hosted_images(values['raw_html'], message_base(chat, message['id']))
            values['attachments_json'] = json.dumps(attachments)
            store.record_message(con, conversation_id=chat, **values)
        url = page.get('@odata.nextLink')
        # Retain the failed page across scheduled passes and process restarts.
        # The final page and cursor are committed together below.
        if url:
            con.execute('UPDATE catchup SET next_url=?,retry_at=NULL WHERE conversation_id=?', (url, chat))
            con.commit()
    with con:
        con.execute('UPDATE catchup SET next_url=NULL,upper_bound=NULL,retry_at=NULL,auth_failures=0 WHERE conversation_id=?', (chat,))
        store.catchup_result(con, chat, cursor=upper)


async def sync(con, client, app, settings):
    chats = [r for r in store.conversations(con) if r['type'] != 'personal']
    if not chats:
        return
    try:
        token = await app.token_provider.get_app_token('https://graph.microsoft.com/.default', settings['tenant_id'])
        if token is None:
            raise ValueError('Graph token unavailable')
    except Exception as error:
        for chat in chats:
            failure(con, chat['id'], error, settings)
        return
    for chat in chats:
        try:
            await sync_chat(con, client, str(token), chat['id'], settings, app.id)
        except Exception as error:
            failure(con, chat['id'], error, settings)


def failure(con, chat, error, settings):
    status = getattr(getattr(error, 'response', None), 'status_code', None)
    reason = ('HTTP 403: no RSC grant for this chat or unsupported chat type; upgrade and consent in this chat'
              if status == 403 else f'HTTP {status}' if status else type(error).__name__)
    store.catchup_result(con, chat, error=reason, status=status)
    if status in (401, 403):
        # Missing consent lasts until someone acts, so back off instead of
        # retrying every pass: double from two intervals up to an hour.
        with con:
            failures = con.execute('SELECT auth_failures FROM catchup WHERE conversation_id=?', (chat,)).fetchone()[0] + 1
            delay = min(3600, settings['catchup_interval_seconds'] * 2 ** failures)
            con.execute('UPDATE catchup SET auth_failures=?,retry_at=? WHERE conversation_id=?',
                        (failures, time.time() + delay, chat))


def retry_now(con, chat):
    """A sign that access may be fixed (an ordinary pushed message or an app install) ends the backoff."""
    with con:
        con.execute('UPDATE catchup SET retry_at=NULL WHERE conversation_id=? AND http_status IN (401,403)', (chat,))


async def roster(con, app, chat):
    generation = store.invalidate_roster(con, chat['id'])
    key = 'roster_generation:' + chat['id']
    try:
        # Chat rosters are not paginated. This is the Bot Connector, not Graph.
        members = await app.api.from_service_url(chat['service_url']).conversations.members(chat['id']).get_all()
        with con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0] != generation:
                return  # a newer fetch or membership change superseded this response
            con.execute('DELETE FROM members WHERE conversation_id=?', (chat['id'],))
            con.executemany('INSERT INTO members VALUES (?,?,?,?)',
                            [(chat['id'], m.id, (m.aad_object_id or '').lower() or None, m.name)
                             for m in members if getattr(m, 'role', None) != 'bot'
                             and not m.id.startswith('28:')])
            con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('roster:' + chat['id'], generation))
            con.execute('DELETE FROM meta WHERE key=?', ('roster_error:' + chat['id'],))
    except Exception as error:
        # Retain the member data but leave aliases unavailable after a failed
        # current fetch. Superseded failures must not overwrite newer health.
        log_status = getattr(getattr(error, 'response', None), 'status_code', None)
        with con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0] != generation:
                return
            con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                        ('roster_error:' + chat['id'], f'HTTP {log_status}' if log_status else type(error).__name__))
        return False
