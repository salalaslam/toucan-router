import gc
import json
import sqlite3
import subprocess

import pytest

from conftest import LID, PHONE
from toucan.cli import main
from toucan.effects import Effects


def test_whatsapp_metadata_is_recent_alias_aware_and_readonly(world, capsys):
    world.wa('old', chat_name='Alice Example', ts=world.now - 1)
    world.wa('new', chat_jid=LID, chat_name='Alice Example')
    world.wa('group', chat_jid='15550000003@g.us', chat_name='Project A', ts=world.now + 1)
    # Fixture writer connections can checkpoint WAL when collected.
    gc.collect()
    before = world.wa_path.read_bytes()
    assert main(['discover', 'whatsapp', '--config', str(world.config_path), '--limit', '2', '--json'], paths=world.paths) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [dict(jid='15550000003@g.us', name='Project A', kind='group', lid_aliases=[],
                         last_message_ts=world.now + 1, routed=False),
                    dict(jid=LID, name='Alice Example', kind='person', lid_aliases=[LID],
                         last_message_ts=world.now, routed=True)]
    assert 'PRIVATE MESSAGE' not in json.dumps(rows)
    assert world.wa_path.read_bytes() == before
    assert main(['discover', 'whatsapp', '--config', str(world.config_path)], paths=world.paths) == 0
    assert LID not in capsys.readouterr().out
    assert main(['discover', 'whatsapp', '--config', str(world.config_path), '--show-ids'], paths=world.paths) == 0
    assert LID in capsys.readouterr().out


def test_teams_discovery_lists_chat_members_without_bodies_or_service_urls(world, capsys):
    world.team('new')
    with sqlite3.connect(world.paths.bot_db) as db:
        db.execute('INSERT OR REPLACE INTO conversations VALUES (?,?,?,?,?,?,?)',
                   ('group:chat', 'groupChat', 'Project A', 'https://secret.example.com', None, '2026-10-17', '2026-10-17'))
        db.execute('INSERT INTO members VALUES (?,?,?,?)',
                   ('group:chat', 'member-a', '00000000-0000-0000-0000-000000000000', 'Alice Example'))
    assert main(['discover', 'teams', '--config', str(world.config_path), '--json'], paths=world.paths) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [dict(conversation_id='group:chat', type='groupChat', name='Project A',
                         members=[dict(aad_id='00000000-0000-0000-0000-000000000000', name='Alice Example')],
                         last_message_ts='2026-10-17T07:00:00+00:00', routed=True)]
    assert 'PRIVATE MESSAGE' not in json.dumps(rows)
    assert 'secret.example.com' not in json.dumps(rows)


def test_email_discovery_counts_recent_addresses_without_reading_bodies(world, capsys):
    calls = []
    world.paths.notmuch_config = str(world.root / 'notmuch-config')

    def run(argv):
        calls.append(argv)
        if 'search' in argv:
            data = ['id:recent@example.com']
        elif '--output=sender' in argv:
            data = [dict(address='ALICE@example.com', name='Alice Example', count=2)]
        else:
            data = [dict(address='alice@example.com', name='Alice Example', count=1),
                    dict(address='requests@example.com', name='', count=2)]
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), '')

    assert main(['discover', 'email', '--config', str(world.config_path), '--limit', '10', '--json'],
                paths=world.paths, effects=Effects(run=run)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [dict(address='alice@example.com', name='Alice Example', sender_count=2, recipient_count=1),
                    dict(address='requests@example.com', name='', sender_count=0, recipient_count=2)]
    assert all(argv[1] == '--config=' + world.paths.notmuch_config for argv in calls)
    assert '--limit=10' in calls[0]
    assert all('show' not in argv for argv in calls)
    assert all('id:"recent@example.com"' in argv[-1] for argv in calls[1:])


def test_thread_discovery_reports_metadata_with_activity_and_schema_gate(world, capsys):
    world.t3_owner(thread='thread-a')
    world.t3('activity', world.now, 'PRIVATE MESSAGE', thread='thread-a')
    with sqlite3.connect(world.paths.t3) as db:
        db.execute('ALTER TABLE orchestration_v2_projection_threads ADD COLUMN title TEXT')
        db.execute('ALTER TABLE orchestration_v2_projection_threads ADD COLUMN project_id TEXT')
        db.execute("UPDATE orchestration_v2_projection_threads SET title='Owner thread',project_id='project-a'")
    assert main(['discover', 'threads', '--config', str(world.config_path), '--json'], paths=world.paths) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [dict(thread_id='thread-a', title='Owner thread', project='project-a', last_activity='2026-10-17T07:00:00.000Z')]
    assert 'PRIVATE MESSAGE' not in json.dumps(rows) and 'SECRET-TOKEN' not in json.dumps(rows)
    with sqlite3.connect(world.paths.t3) as db:
        db.execute('DROP TABLE scheduled_tasks')
    assert main(['discover', 'threads', '--config', str(world.config_path), '--json'], paths=world.paths) == 1
    assert 'schema unsupported' in capsys.readouterr().err


@pytest.mark.parametrize('kind', ['whatsapp', 'teams', 'threads'])
def test_missing_discovery_store_is_not_created(world, kind, capsys):
    path = world.root / 'missing'
    if kind == 'whatsapp':
        world.paths.wa = path
    elif kind == 'teams':
        world.paths.bot_db = path
    else:
        world.paths.t3 = path
    assert main(['discover', kind, '--config', str(world.config_path), '--json'], paths=world.paths) == 1
    assert 'unavailable' in capsys.readouterr().err
    assert not path.exists()


@pytest.mark.parametrize('limit', [0, -1])
def test_discovery_rejects_nonpositive_limits(world, limit, capsys):
    assert main(['discover', 'whatsapp', '--config', str(world.config_path), '--limit', str(limit)], paths=world.paths) == 2
    assert 'positive' in capsys.readouterr().err
