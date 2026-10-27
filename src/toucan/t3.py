"""Optional read-only access to T3 internals; schema changes disable these features."""
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import timestamp
from .storage import readonly

REQUIRED = {
    'scheduled_tasks': {'task_id', 'thread_id', 'prompt', 'webhook_token'},
    'orchestration_v2_projection_messages': {'message_id', 'thread_id', 'role', 'created_at', 'payload_json'},
    'orchestration_v2_projection_threads': {'thread_id', 'active_provider_thread_id'},
    'orchestration_v2_projection_provider_threads': {'provider_thread_id', 'thread_id', 'payload_json'},
    'orchestration_v2_projection_runs': {'thread_id', 'ordinal', 'status'},
    'orchestration_v2_projection_subagents': {'thread_id', 'status'},
}


def probe(paths):
    try:
        db = readonly(paths.t3)
        try:
            return all(columns <= {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
                       for table, columns in REQUIRED.items())
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return False


def thread_exists(paths, thread_id):
    if not probe(paths):
        return False
    try:
        db = readonly(paths.t3)
        try:
            return bool(db.execute('SELECT 1 FROM orchestration_v2_projection_threads WHERE thread_id=?', (thread_id,)).fetchone())
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return False


def owner_said(paths, thread_id, since):
    """What the owner typed in a T3 thread after `since`. Agents, wake-ups and thread
    messages from other agents carry createdBy 'agent'; slash commands are not answers."""
    if not probe(paths):
        return None
    try:
        db = readonly(paths.t3)
        try:
            rows = db.execute("SELECT message_id,created_at,json_extract(payload_json,'$.text') AS text "
                              "FROM orchestration_v2_projection_messages WHERE thread_id=? AND role='user' "
                              "AND json_extract(payload_json,'$.createdBy')='user' ORDER BY created_at", (thread_id,)).fetchall()
        finally:
            db.close()
        said = [dict(id=r['message_id'], ts=timestamp(r['created_at']), text=(r['text'] or '').strip()) for r in rows]
        return [m for m in said if m['ts'] > since and m['text'] and not m['text'].startswith('/')]
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        return None


def hook_info(url):
    """Only the task ID and origin are safe to show; discard the token."""
    try:
        parts = urlsplit(url)
        path = parts.path.split('/')
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or len(path) != 5 or path[:3] != ['', 'api', 'hooks']
                or not path[3] or not path[4]):
            return None
        parts.port  # reject invalid ports
        return dict(task_id=unquote(path[3]), origin=f'{parts.scheme}://{parts.netloc}')
    except ValueError:
        return None


def hook_thread(paths, url):
    info = hook_info(url)
    if not info or not probe(paths):
        return None
    try:
        db = readonly(paths.t3)
        try:
            row = db.execute('SELECT thread_id FROM scheduled_tasks WHERE task_id=?', (info['task_id'],)).fetchone()
        finally:
            db.close()
        return row['thread_id'] if row and row['thread_id'] else None
    except (OSError, sqlite3.Error):
        return None


def hook_file_thread(paths, path):
    try:
        return hook_thread(paths, Path(path).expanduser().read_text().strip())
    except (OSError, UnicodeError):
        return None


FINISHED = ('completed', 'failed', 'cancelled', 'interrupted')


def thread_usage(paths, thread_id):
    """Unknown usage or activity must never allow an automatic rotation."""
    if not probe(paths):
        return None
    try:
        db = readonly(paths.t3)
        try:
            row = db.execute('SELECT p.payload_json FROM orchestration_v2_projection_threads t '
                             'JOIN orchestration_v2_projection_provider_threads p '
                             'ON p.provider_thread_id=t.active_provider_thread_id AND p.thread_id=t.thread_id '
                             'WHERE t.thread_id=?', (thread_id,)).fetchone()
            run = db.execute('SELECT status FROM orchestration_v2_projection_runs WHERE thread_id=? '
                             'ORDER BY ordinal DESC LIMIT 1', (thread_id,)).fetchone()
            # A background agent still working would report back to the old thread.
            agents = db.execute('SELECT 1 FROM orchestration_v2_projection_subagents WHERE thread_id=? '
                                f'AND status NOT IN ({",".join("?" * len(FINISHED))}) LIMIT 1', (thread_id, *FINISHED)).fetchone()
        finally:
            db.close()
        payload = json.loads(row['payload_json']) if row else None
        usage = payload['contextUsage']
        used, maximum, pending = usage['usedTokens'], usage['maxTokens'], payload.get('pendingBackgroundTasks')
        if type(used) is not int or used < 0 or type(maximum) is not int or maximum <= 0:
            return None
        busy = run['status'] not in FINISHED if run else None
        if agents or isinstance(pending, list) and pending:
            busy = True
        elif not isinstance(pending, list):
            busy = None
        return dict(used_tokens=used, max_tokens=maximum, busy=busy)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        return None


# How Claude Code and the proxy report a model rate limit as the turn's last message.
# Codex writes a typographic apostrophe ("You’ve").
LIMITED = re.compile(r"(Agent terminated early due to an API error: )?API Error: .*\b(429|529|rate_limit_error|overloaded_error)\b"
                     r"|You['\u2019]ve hit your [\w ]*limit")
# Failure codes T3 records on a run that a provider ended for quota or capacity.
LIMIT_CODES = ('usageLimitExceeded', 'serverOverloaded', 'api_error_429', 'api_error_529')


def rate_limited(paths, thread_id):
    """When the thread's last turn stopped at a model rate limit, when that was; otherwise None.

    The latest run decides first: a failed run can end without any chat message (Codex at its
    usage limit, for one), and a newer running or finished run means an older limit message is
    stale. A message without a run ID counts only if it is newer than the latest run's request.
    Older T3 versions without these tables fall back to the latest message alone."""
    if not probe(paths):
        return None
    try:
        db = readonly(paths.t3)
        try:
            try:
                row = db.execute("SELECT created_at,role,run_id,json_extract(payload_json,'$.text') AS text "
                                 "FROM orchestration_v2_projection_messages WHERE thread_id=? "
                                 "ORDER BY created_at DESC,rowid DESC LIMIT 1", (thread_id,)).fetchone()
                latest = db.execute('SELECT run_id,status,requested_at,completed_at FROM orchestration_v2_projection_runs '
                                    'WHERE thread_id=? ORDER BY ordinal DESC LIMIT 1', (thread_id,)).fetchone()
                failure = latest and latest['status'] == 'failed' and db.execute(
                    "SELECT json_extract(payload_json,'$.failure.class') AS class,json_extract(payload_json,'$.failure.code') AS code,"
                    "json_extract(payload_json,'$.failure.message') AS message FROM orchestration_v2_projection_turn_items "
                    "WHERE run_id=? AND type='error' AND status='failed' ORDER BY ordinal DESC LIMIT 1", (latest['run_id'],)).fetchone()
            except sqlite3.OperationalError:
                row = db.execute("SELECT created_at,role,NULL AS run_id,json_extract(payload_json,'$.text') AS text "
                                 "FROM orchestration_v2_projection_messages WHERE thread_id=? "
                                 "ORDER BY created_at DESC,rowid DESC LIMIT 1", (thread_id,)).fetchone()
                latest = failure = None
        finally:
            db.close()
        if latest:
            if failure and latest['completed_at'] and (failure['class'] == 'usage_limit' or failure['code'] in LIMIT_CODES
                                                        or LIMITED.match(failure['message'] or '')):
                return timestamp(latest['completed_at'])
            if not row or latest['status'] not in FINISHED:
                return None  # a turn is running
            if (row['run_id'] != latest['run_id'] if row['run_id'] else
                    not latest['requested_at'] or timestamp(row['created_at']) < timestamp(latest['requested_at'])):
                return None  # the message belongs to an older turn
        if row and row['role'] == 'assistant' and LIMITED.match(row['text'] or ''):
            return timestamp(row['created_at'])
        return None
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        return None
