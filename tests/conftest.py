import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from toucan.config import load, timestamp
from toucan.core import App
from toucan.effects import Effects
from toucan.sources import Paths
from toucan.storage import connect

PHONE = '15550000001@s.whatsapp.net'
LID = '100000000000001@lid'
OWNER = '15550000000@s.whatsapp.net'
OTHER = '15550000002@s.whatsapp.net'


class World:
    def __init__(self, root):
        self.root = root
        self.now = timestamp('2026-10-17T12:00:00+05:00')
        self.paths = Paths(root / 'wa', root / 'bot.db', root / 't3.sqlite', root / 'media')
        self.paths.wa_service = 'example-wa.service'
        self.paths.teams_service = 'example-teams.service'
        self.paths.wa.mkdir()
        with sqlite3.connect(self.paths.bot_db) as db:
            db.executescript(Path('tests/bot_store.sql').read_text())
            db.execute("INSERT INTO meta VALUES ('schema_version','4')")
            db.execute("INSERT INTO conversations VALUES ('group:chat','groupChat',NULL,'',NULL,'2026-10-01T00:00:00Z','now')")
            db.execute("INSERT INTO catchup (conversation_id) VALUES ('group:chat')")
        self.wa_path = self.paths.wa / 'wacli.db'
        with sqlite3.connect(self.wa_path) as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE messages (
             rowid INTEGER PRIMARY KEY, chat_jid TEXT, chat_name TEXT, msg_id TEXT,
             sender_jid TEXT, sender_name TEXT, ts INTEGER, from_me INTEGER,
             text TEXT, display_text TEXT, quoted_msg_id TEXT, reaction_to_id TEXT, reaction_emoji TEXT,
             media_type TEXT, media_caption TEXT, filename TEXT, local_path TEXT,
             revoked INTEGER DEFAULT 0, deleted_for_me INTEGER DEFAULT 0, edited INTEGER DEFAULT 0);
            ''')
        with sqlite3.connect(self.paths.wa / 'session.db') as db:
            db.execute('CREATE TABLE whatsmeow_lid_map(lid TEXT PRIMARY KEY,pn TEXT NOT NULL UNIQUE)')
            db.execute('INSERT INTO whatsmeow_lid_map VALUES (?,?)', (LID.split('@')[0], PHONE.split('@')[0]))
        with sqlite3.connect(self.paths.t3) as db:
            db.execute('CREATE TABLE orchestration_v2_projection_messages (message_id TEXT PRIMARY KEY, thread_id TEXT, '
                       'role TEXT, created_at TEXT, payload_json TEXT, run_id TEXT)')
            db.executescript('''
            CREATE TABLE scheduled_tasks (task_id TEXT PRIMARY KEY, thread_id TEXT, prompt TEXT, webhook_token TEXT);
            CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT PRIMARY KEY, active_provider_thread_id TEXT);
            CREATE TABLE orchestration_v2_projection_provider_threads (provider_thread_id TEXT PRIMARY KEY, thread_id TEXT, payload_json TEXT);
            CREATE TABLE orchestration_v2_projection_runs (run_id TEXT PRIMARY KEY, thread_id TEXT, ordinal INTEGER, status TEXT,
                                                           completed_at TEXT, requested_at TEXT);
            CREATE TABLE orchestration_v2_projection_turn_items (turn_item_id TEXT PRIMARY KEY, run_id TEXT, ordinal INTEGER,
                                                                 type TEXT, status TEXT, payload_json TEXT);
            CREATE TABLE orchestration_v2_projection_subagents (subagent_id TEXT PRIMARY KEY, thread_id TEXT, status TEXT);
            ''')
        (self.paths.wa / 'HEARTBEAT').touch()
        self.hook = root / 'test.hook'
        self.hook.write_text('https://hook.invalid/api/hooks/test/SECRET-TOKEN')
        self.hook.chmod(0o600)
        example = Path('examples/toucan.example.toml').read_text()
        example = example.replace('~/.config/toucan/hooks/project-a.hook', str(self.hook))
        # Intentionally omit the configured LID alias: the session map must resolve it.
        example = example.replace(f'whatsapp = ["{PHONE}", "{LID}"]', f'whatsapp = ["{PHONE}"]')
        self.config_path = root / 'toucan.toml'
        example = example.replace('# owner_teams_id = "00000000-0000-0000-0000-000000000000"', 'owner_teams_id = "owner-entra"')
        example = example.replace('owner_whatsapp = ""', f'owner_whatsapp = "{OWNER}"')
        example = example.replace('owner_name = "the owner"', 'owner_name = "Example Owner"\nalert_url = "http://127.0.0.1:8090/alerts"')
        self.config_text = example + f'''
[people.other]
name = "Alice Example"
teams_id = "other-entra"
whatsapp = ["{OTHER}"]

[[routes]]
name = "group"
source = "teams"
chats = ["group:chat"]
thread = "project-a"
mode = "act"
reply_via_whatsapp = true
requesters = ["alice", "other"]
active_from = "2026-10-17T00:00:00+05:00"
'''
        self.config_path.write_text(self.config_text)
        self.cfg = load(self.config_path)
        self.db_path = root / 'state.db'
        self.db = connect(self.db_path)
        self.posts, self.sends = [], []
        self.post_status = 202
        self.send_result = subprocess.CompletedProcess([], 0, '{"data":{"MsgID":"sent-id"}}', '')
        self.send_error = None
        self.downloads, self.transcribed = [], []
        self.download_ok, self.transcript = True, (0, 'Please check the invoice\n')
        self.fx = Effects(now=lambda: self.now, run=self.run, post=self.post)
        self.app = App(self.cfg, self.db, self.fx, self.paths)
        self.freshen()

    def freshen(self):
        from datetime import datetime, timezone
        with sqlite3.connect(self.paths.bot_db) as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('heartbeat',?)",
                       (datetime.fromtimestamp(self.now, timezone.utc).isoformat(),))
            # Healthy catch-up unless a test records a failure or removes the chat's state.
            db.execute("UPDATE catchup SET last_success=? WHERE conversation_id='group:chat' AND last_error IS NULL",
                       (datetime.fromtimestamp(self.now, timezone.utc).isoformat(),))
        for path in [self.paths.wa / 'HEARTBEAT']:
            os.utime(path, (self.now, self.now))

    def advance(self, seconds):
        self.now += seconds
        self.freshen()

    def run(self, argv):
        if argv in (['codex', 'login', 'status'], ['claude', 'auth', 'status']):
            return subprocess.CompletedProcess(argv, 0, '', '')
        if argv == ['ps', '-eo', 'args=']:
            return subprocess.CompletedProcess(argv, 0, '', '')
        if argv[0] == 'wacli' and '--store' in argv:
            index = argv.index('--store')
            argv = argv[:index] + argv[index + 2:]
        if argv == ['toucan-bot', 'status', '--json']:
            return subprocess.CompletedProcess(argv, 0, '{"ok":true}', '')
        if argv[0] == 'systemctl':
            return subprocess.CompletedProcess(argv, 0, 'Result=success\n', '')
        if argv[:5] == ['wacli', '--json', '--read-only', 'media', 'download']:
            self.downloads.append(argv[8])
            if not self.download_ok:
                return subprocess.CompletedProcess(argv, 1, '', 'media key missing')
            path = Path(argv[10]) / f'message-{argv[8]}.oga'
            path.write_bytes(b'OggS')
            return subprocess.CompletedProcess(argv, 0, json.dumps({'data': {'path': str(path)}}), '')
        if argv[0] == 'transcribe-test':
            self.transcribed.append(argv[1])
            return subprocess.CompletedProcess(argv, self.transcript[0], self.transcript[1], '')
        assert argv[0] in ('wacli', 'toucan-bot') and argv[1] == '--json', argv
        assert argv[2] == 'send' or (argv[0] == 'toucan-bot' and argv[2] == 'react'), argv
        self.sends.append(argv)
        if self.send_error:
            raise self.send_error
        return self.send_result

    def post(self, url, data, headers):
        self.posts.append((url, data, headers))
        if url.startswith('https://hook.invalid'):
            if isinstance(self.post_status, Exception):
                raise self.post_status
            return self.post_status
        return 200

    def wa(self, msg_id, sender=PHONE, ts=None, **values):
        m = dict(chat_jid=PHONE, msg_id=msg_id, sender_jid=sender,
                 sender_name='Alice Example', ts=int(self.now if ts is None else ts), from_me=0,
                 text='Please investigate PRIVATE MESSAGE', **{})
        m.update(values)
        with sqlite3.connect(self.wa_path) as db:
            db.execute(f'INSERT INTO messages ({",".join(m)}) VALUES ({",".join("?" for _ in m)})', tuple(m.values()))

    def t3(self, message_id, ts, text, created_by='user', thread='11111111-1111-1111-1111-111111111111', role='user', run=None):
        from datetime import datetime, timezone
        created = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        with sqlite3.connect(self.paths.t3) as db:
            db.execute('INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?)',
                       (message_id, thread, role, created, json.dumps(dict(createdBy=created_by, text=text)),
                        run and f'run-{thread}-{run}'))

    def t3_run(self, ordinal, status, ts, failure=None, thread='11111111-1111-1111-1111-111111111111'):
        """A T3 run; a failed one records its failure as an error turn item, as T3 does."""
        from datetime import datetime, timezone
        done = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        run = f'run-{thread}-{ordinal}'
        with sqlite3.connect(self.paths.t3) as db:
            db.execute('INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?)', (run, thread, ordinal, status, done, done))
            if failure:
                db.execute('INSERT INTO orchestration_v2_projection_turn_items VALUES (?,?,?,?,?,?)',
                           ('error-' + run, run, 1, 'error', 'failed', json.dumps(dict(title='Usage limit reached', failure=failure))))

    def t3_owner(self, thread='owner-thread', task='task:/project-a', used=412000, status='completed', pending=None):
        from datetime import datetime, timezone
        from urllib.parse import quote
        self.hook.write_text(f'https://hook.invalid/api/hooks/{quote(task, safe="")}/SECRET-TOKEN')
        payload = dict(contextUsage=dict(usedTokens=used, maxTokens=1000000), pendingBackgroundTasks=pending or [])
        with sqlite3.connect(self.paths.t3) as db:
            db.execute('INSERT OR REPLACE INTO scheduled_tasks VALUES (?,?,?,?)', (task, thread, 'Wake prompt', 'SECRET-TOKEN'))
            db.execute('INSERT OR REPLACE INTO orchestration_v2_projection_threads VALUES (?,?)', (thread, 'provider-' + thread))
            db.execute('INSERT OR REPLACE INTO orchestration_v2_projection_provider_threads VALUES (?,?,?)',
                       ('provider-' + thread, thread, json.dumps(payload)))
            requested = datetime.fromtimestamp(self.now, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            db.execute('INSERT OR REPLACE INTO orchestration_v2_projection_runs (run_id,thread_id,ordinal,status,requested_at) '
                       'VALUES (?,?,?,?,?)', ('run-' + thread, thread, 1, status, requested))

    def team(self, msg_id, sender='00000000-0000-0000-0000-000000000001', ts=None, **values):
        from datetime import datetime, timezone
        m = dict(conversation_id='group:chat', id=msg_id,
                 ts=datetime.fromtimestamp(self.now if ts is None else ts, timezone.utc).isoformat(),
                 sender_aad_id=sender, sender_name='Alice Example', text='Team PRIVATE MESSAGE',
                 mentioned=0, reply_to_id=None, received_at='2026-10-17T07:00:00Z')
        m.update(values)
        with sqlite3.connect(self.paths.bot_db) as db:
            db.execute(f'INSERT INTO messages ({",".join(m)}) VALUES ({",".join("?" for _ in m)}) '
                       'ON CONFLICT(conversation_id,id) DO UPDATE SET ' +
                       ','.join(f'{key}=excluded.{key}' for key in m if key not in ('conversation_id','id')), tuple(m.values()))

    def seed(self):
        result = self.app.sync()
        assert result['created'] == []
        assert self.hooks() == []
        self.advance(1)

    def request(self, msg_id='req', teams=False):
        (self.team if teams else self.wa)(msg_id)
        return self.app.sync()['created'][0]

    def hooks(self):
        return [json.loads(data) for url, data, _ in self.posts if url.startswith('https://hook.invalid')]

    def alerts(self):
        return self.app.rows('SELECT * FROM alerts')


@pytest.fixture(autouse=True)
def default_config_home(monkeypatch):
    # CI runners set XDG_CONFIG_HOME, which would override the HOME that tests point at tmp_path.
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setenv('TOUCAN_DB', str(w.db_path))
    yield w
    w.db.close()
