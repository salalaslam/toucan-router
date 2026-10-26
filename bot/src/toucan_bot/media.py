"""Private, bounded inline-image cache. File references are never downloaded."""

from __future__ import annotations

import hashlib
import asyncio
import os
import json
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from itertools import islice
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit, urljoin
from email.utils import parsedate_to_datetime

import httpx

from . import config


class Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == 'img' and (src := dict(attrs).get('src')):
            self.urls.append(src)


def identifier(value):
    """Decode before validation, including nested encodings that clients may normalize."""
    for _ in range(8):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    if (not value or '%' in value or value in ('.', '..') or
            any(c in value for c in '/\\?#') or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise ValueError('invalid image identifier')
    return value


def download_url(url, source, key):
    """Build an endpoint from IDs, never pass an untrusted URL to authenticated HTTP."""
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.username or parts.password or parts.query or parts.fragment or
            parts.port not in (None, 443)):
        raise ValueError('unsupported image URL')
    chat, message = (identifier(v) for v in key.split('/'))
    segments = parts.path.split('/')
    if parts.hostname == 'graph.microsoft.com':
        if (len(segments) != 9 or segments[:3] != ['', 'v1.0', 'chats'] or
                segments[4] != 'messages' or segments[6] != 'hostedContents' or segments[8] != '$value' or
                identifier(segments[3]) != chat or identifier(segments[5]) != message):
            raise ValueError('image is not bound to this message')
        hosted = identifier(segments[7])
        if source != 'graph':
            raise ValueError('Graph images require Graph authentication')
        return ('https://graph.microsoft.com/v1.0/chats/' + quote(chat, safe='') + '/messages/' +
                quote(message, safe='') + '/hostedContents/' + quote(hosted, safe='') + '/$value')
    # Connector attachment IDs are opaque. Bind the canonical ID/view to the
    # authenticated activity's chat/message key when queuing and caching it.
    host = parts.hostname or ''
    if source != 'live' or not (host == 'smba.trafficmanager.net' or host.endswith('.asm.skype.com')):
        raise ValueError('unsupported image host')
    # Connector service URLs may carry a regional routing segment. Preserve it,
    # validating it before HTTPX can normalize any literal or encoded traversal.
    route = segments[1:]
    prefix = []
    if len(route) == 6:
        region = identifier(route.pop(0))
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', region):
            raise ValueError('unsupported image service prefix')
        prefix = [region]
    if (len(route) == 5 and route[:2] in (['v3', 'attachments'], ['v1', 'objects']) and route[3] == 'views'
            and (not prefix or route[:2] == ['v3', 'attachments'])):
        attachment, view = identifier(route[2]), identifier(route[4])
        if view not in ('original', 'imgo'):
            raise ValueError('unsupported image view')
        return 'https://' + host + '/' + '/'.join(prefix + [route[0], route[1], quote(attachment, safe=''), 'views', view])
    raise ValueError('unsupported image path')


def hosted_images(markup, base):
    parser = Images()
    parser.feed(markup)
    result = []
    segments = urlsplit(base).path.split('/')
    key = identifier(segments[3]) + '/' + identifier(segments[5])
    for url in dict.fromkeys(parser.urls):
        # Validate before URL joining: urljoin would discard literal ../ segments.
        try:
            for part in urlsplit(url).path.split('/'):
                if part:
                    identifier(part)
            endpoint = download_url(urljoin(base + '/', url), 'graph', key)
        except ValueError:
            continue
        result.append(dict(contentType='image/*', contentUrl=endpoint, inline=True))
    return result


def retry_delay(response=None, attempts=0):
    delay = min(3600, 2 ** min(attempts + 1, 11))
    value = response.headers.get('Retry-After', '') if response is not None else ''
    try:
        after = float(value)
    except ValueError:
        try:
            after = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            after = 0
    return max(delay, after)


async def download(client, attachment, token, settings, *, source, key, defer_publish=False):
    item = dict(attachment)
    # A PNG uploaded as a SharePoint file is still a file, not an inline image.
    if not str(item.get('contentType', '')).lower().startswith('image/'):
        return item
    url = item.get('contentUrl', '')
    item.pop('local_path', None)
    item.update(download_status='unavailable')
    try:
        url = download_url(url, source, key)
    except (ValueError, TypeError):
        item['download_error'] = 'unsupported or unbound image URL'
        return item
    for field in ('download_error', 'http_status', 'retry_at'):
        item.pop(field, None)
    temp = None
    try:
        directory = config.state_dir() / 'media'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        filename = hashlib.sha256((key + '\n' + url).encode()).hexdigest() + '.img'
        path = directory / filename
        if path.is_file() and not path.is_symlink():
            path.chmod(0o600)
            item.update(local_path=str(path), download_status='available')
            return item
        async with client.stream('GET', url, headers={'Authorization': 'Bearer ' + token}, follow_redirects=False) as response:
            item['http_status'] = response.status_code
            if response.status_code != 200:
                if response.status_code in (401, 408, 429) or response.status_code >= 500:
                    item.update(download_status='pending', retry_at=time.time() + retry_delay(response))
                return item
            if not response.headers.get('content-type', '').lower().startswith('image/'):
                item['download_error'] = 'response is not an image'
                return item
            cap = settings['media_max_bytes']
            if int(response.headers.get('content-length', '0')) > cap:
                item['download_error'] = 'image exceeds size cap'
                return item
            fd, temp = tempfile.mkstemp(prefix=filename + '.', suffix='.part', dir=directory)
            size = 0
            with os.fdopen(fd, 'wb') as output:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    size += len(chunk)
                    if size > cap:
                        item['download_error'] = 'image exceeds size cap'
                        return item
                    output.write(chunk)
        # Claims fence publication as well as metadata. A stale worker must not
        # overwrite another worker's bytes or recreate an expired screenshot.
        if defer_publish:
            item['_staged_path'] = temp
        else:
            os.replace(temp, path)
        item.update(local_path=str(path), download_status='available')
    except (httpx.HTTPError, OSError, ValueError) as error:
        # URLs and HTTP exception strings can contain credentials. Persist only the class.
        item['download_error'] = type(error).__name__
        if isinstance(error, (httpx.TransportError, OSError)):
            item.update(download_status='pending', retry_at=time.time() + retry_delay())
    finally:
        if temp and item.get('_staged_path') != temp and os.path.exists(temp):
            os.unlink(temp)
    return item


def queue(con, chat, message, source):
    # Join the message writer's transaction rather than committing it here.
    with nullcontext() if con.in_transaction else con:
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT deleted,attachments_json FROM messages WHERE conversation_id=? AND id=?', (chat, message)).fetchone()
        if not row:
            return
        items = json.loads(row['attachments_json']) if not row['deleted'] else []
        urls = set()
        for item in items:
            if not str(item.get('contentType', '')).startswith('image/'):
                continue
            url = item.get('contentUrl', '')
            # Duplicate deliveries may enrich a message with images from the other
            # transport. Keep their authentication and pending work separate.
            try:
                graph = urlsplit(url).hostname == 'graph.microsoft.com'
            except ValueError:
                graph = False  # downloader records unsupported URLs as unavailable
            if graph != (source == 'graph'):
                continue
            if item.get('download_status') in ('available', 'expired', 'unavailable'):
                continue
            urls.add(url)
            con.execute('INSERT OR IGNORE INTO pending_media (conversation_id,message_id,source,url,attachment_json) VALUES (?,?,?,?,?)',
                        (chat, message, source, url, json.dumps(item)))
        for job in con.execute('SELECT url FROM pending_media WHERE conversation_id=? AND message_id=? AND source=?', (chat, message, source)).fetchall():
            if job['url'] not in urls:
                con.execute('DELETE FROM pending_media WHERE conversation_id=? AND message_id=? AND source=? AND url=?', (chat, message, source, job['url']))


async def renew_token(client, settings, source):
    """Bypass the SDK's cached rejected token using the bot's stored credentials."""
    creds = config.load_credentials()
    if creds is None or config.credential_warning(creds, settings):
        raise ValueError('bot credentials unavailable for image token renewal')
    tenant = settings['tenant_id'] if source == 'graph' else config.app_tenant(creds) or 'botframework.com'
    scope = 'https://graph.microsoft.com/.default' if source == 'graph' else 'https://api.botframework.com/.default'
    response = await client.post('https://login.microsoftonline.com/' + quote(tenant, safe='') + '/oauth2/v2.0/token',
                                 data=dict(grant_type='client_credentials', client_id=creds.client_id,
                                           client_secret=creds.client_secret, scope=scope), follow_redirects=False)
    response.raise_for_status()
    token = response.json().get('access_token')
    if not isinstance(token, str) or not token:
        raise ValueError('image token renewal returned no token')
    return token


async def retry_pending(con, client, token, settings, *, source, chat=None):
    jobs = con.execute('SELECT conversation_id,message_id,source,url FROM pending_media '
                       'WHERE source=? AND next_attempt<=? AND (? IS NULL OR conversation_id=?)',
                       (source, time.time(), chat, chat)).fetchall()
    renewed_token, renewal_error = None, None
    for candidate in jobs:
        where = tuple(candidate[k] for k in ('conversation_id', 'message_id', 'source', 'url'))
        claim = uuid.uuid4().hex
        with con:
            if not con.in_transaction:
                con.execute('BEGIN IMMEDIATE')
            claimed = con.execute('UPDATE pending_media SET claim_id=?,claim_until=? '
                                  'WHERE conversation_id=? AND message_id=? AND source=? AND url=? '
                                  'AND next_attempt<=? AND (claim_id IS NULL OR claim_until<=?)',
                                  (claim, time.time() + 300, *where, time.time(), time.time())).rowcount
            if not claimed:
                continue
            job = con.execute('SELECT * FROM pending_media WHERE conversation_id=? AND message_id=? AND source=? AND url=?', where).fetchone()
            row = con.execute('SELECT deleted,attachments_json FROM messages WHERE conversation_id=? AND id=?', where[:2]).fetchone()
            items = json.loads(row['attachments_json']) if row and not row['deleted'] else []
            eligible = any(a.get('contentUrl', '') == job['url'] and str(a.get('contentType', '')).startswith('image/') and
                           a.get('download_status') not in ('available', 'expired', 'unavailable') for a in items)
            if not eligible:
                con.execute('DELETE FROM pending_media WHERE claim_id=?', (claim,))
                continue
        # No transaction spans authentication or image HTTP. The five-minute
        # lease recovers crashed workers; claim identity fences a late completion.
        staged = None
        try:
            attachment = json.loads(job['attachment_json'])
            if attachment.get('http_status') == 401 and renewed_token is None and renewal_error is None:
                try:
                    renewed_token = await renew_token(client, settings, source)
                except (httpx.HTTPError, OSError, ValueError) as error:
                    renewal_error = error
            if attachment.get('http_status') == 401 and renewal_error is not None:
                result = attachment | dict(download_status='pending', download_error=type(renewal_error).__name__,
                                           retry_at=time.time() + retry_delay(getattr(renewal_error, 'response', None), job['attempts']))
            else:
                result = await download(client, attachment, renewed_token or token, settings, source=source,
                                        key=job['conversation_id'] + '/' + job['message_id'], defer_publish=True)
            staged = result.pop('_staged_path', None)
            with con:
                if not con.in_transaction:
                    con.execute('BEGIN IMMEDIATE')
                active = con.execute('SELECT 1 FROM pending_media WHERE conversation_id=? AND message_id=? AND source=? AND url=? AND claim_id=?',
                                     (*where, claim)).fetchone()
                if not active:
                    continue  # another worker or a queue replacement owns the current work
                row = con.execute('SELECT deleted,attachments_json FROM messages WHERE conversation_id=? AND id=?', where[:2]).fetchone()
                items = json.loads(row['attachments_json']) if row and not row['deleted'] else []
                indexes = [i for i, a in enumerate(items) if a.get('contentUrl', '') == job['url'] and
                           str(a.get('contentType', '')).startswith('image/') and
                           a.get('download_status') not in ('available', 'expired', 'unavailable')]
                if indexes:
                    if result['download_status'] == 'available' and not staged and not os.path.isfile(result['local_path']):
                        # Maintenance may have expired an orphaned cache entry
                        # between the downloader's lookup and this transaction.
                        result.pop('local_path')
                        result.update(download_status='pending', download_error='cached image expired',
                                      retry_at=time.time() + retry_delay(attempts=job['attempts']))
                    if staged:
                        try:
                            os.replace(staged, result['local_path'])
                            staged = None
                        except OSError as error:
                            result.pop('local_path', None)
                            result.update(download_status='pending', download_error=type(error).__name__,
                                          retry_at=time.time() + retry_delay(attempts=job['attempts']))
                    for i in indexes:
                        # An edit can change the name/card metadata without changing
                        # the image URL. Replace download fields only.
                        for field in ('local_path', 'download_status', 'http_status', 'download_error', 'retry_at'):
                            items[i].pop(field, None)
                            if field in result:
                                items[i][field] = result[field]
                    con.execute('UPDATE messages SET attachments_json=? WHERE conversation_id=? AND id=?',
                                (json.dumps(items), *where[:2]))
                if indexes and result['download_status'] == 'pending':
                    after = max(result.get('retry_at', 0), time.time() + retry_delay(attempts=job['attempts']))
                    con.execute('UPDATE pending_media SET attempts=attempts+1,next_attempt=?,attachment_json=?,claim_id=NULL,claim_until=NULL '
                                'WHERE claim_id=?', (after, json.dumps(result), claim))
                else:
                    con.execute('DELETE FROM pending_media WHERE claim_id=?', (claim,))
        finally:
            if staged and os.path.exists(staged):
                os.unlink(staged)
            # Cancellation releases only this worker's claim. Never delete or
            # unclaim replacement work created while HTTP was in flight.
            with con:
                con.execute('UPDATE pending_media SET claim_id=NULL,claim_until=NULL WHERE claim_id=?', (claim,))


async def expire(settings):
    """Keep filesystem work off the HTTP loop and join it on cancellation."""
    stop = threading.Event()
    def work():
        con = sqlite3.connect(config.db_path(), timeout=0)
        con.row_factory = sqlite3.Row
        try:
            prune(con, settings, stop=stop)
        finally:
            con.close()
    # Own this worker's lifetime rather than leaving file work in the server's
    # shared executor during shutdown.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='toucan-expiry') as pool:
        worker = asyncio.get_running_loop().run_in_executor(pool, work)
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop.set()
            try:
                await worker
            except Exception:
                pass
            raise


def prune(con, settings, *, stop=None):
    cutoff = time.time() - settings['media_retention_days'] * 86400
    directory = config.state_dir() / 'media'
    def candidates():
        for pattern in ('*.img', '*.part'):
            for path in directory.glob(pattern):
                if stop and stop.is_set():
                    return
                try:
                    if not path.is_symlink() and path.stat().st_mtime < cutoff:
                        yield path
                except FileNotFoundError:
                    pass
    paths = candidates()
    while not stop or not stop.is_set():
        # Enumerate outside the writer lock, retaining at most one batch.
        batch = list(islice(paths, 32))
        if not batch:
            break
        with con:
            con.execute('BEGIN IMMEDIATE')
            for path in batch:
                try:
                    # Revalidate under the publication lock: a newer worker may
                    # have replaced these bytes since enumeration.
                    if not path.is_symlink() and path.stat().st_mtime < cutoff:
                        path.unlink()
                except FileNotFoundError:
                    pass
    last = 0
    while not stop or not stop.is_set():
        rows = con.execute("SELECT rowid,conversation_id,id FROM messages WHERE rowid>? "
                           "AND attachments_json LIKE '%local_path%' ORDER BY rowid LIMIT 32", (last,)).fetchall()
        if not rows:
            break
        last = rows[-1]['rowid']
        with con:
            con.execute('BEGIN IMMEDIATE')
            for row in rows:
                current = con.execute('SELECT attachments_json FROM messages WHERE rowid=?', (row['rowid'],)).fetchone()
                if not current:
                    continue
                attachments = json.loads(current['attachments_json'])
                changed = False
                for item in attachments:
                    if item.get('local_path') and not os.path.isfile(item['local_path']):
                        item.pop('local_path')
                        item.update(download_status='expired')
                        changed = True
                if changed:
                    con.execute('UPDATE messages SET attachments_json=? WHERE conversation_id=? AND id=?',
                                (json.dumps(attachments), row['conversation_id'], row['id']))
