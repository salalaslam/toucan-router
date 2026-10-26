import pytest

from toucan_bot import names

CHATS = {
    "alice-1on1": {"name": "Alice  Example", "type": "oneOnOne", "person_aliases": ["Alice Example"]},
    "carol-1on1": {"name": "Carol  Example", "type": "oneOnOne", "person_aliases": ["Carol Example"]},
    "project-a-group": {"name": "Alice  Example, Bob Example, Owner Example, David Example", "type": "groupChat"},
    "project-a-mgmt": {"name": "Project A (Management)", "type": "groupChat"},
    "project-a-dev": {"name": "Project A (Dev)", "type": "groupChat"},
    "bot-1on1": {"name": "Owner Example", "type": "personal"},
}
BOT_IN = ["alice-1on1", "project-a-group", "project-a-mgmt", "project-a-dev", "bot-1on1"]


def resolve(query):
    return names.resolve(query, BOT_IN, CHATS)


def test_a_person_means_their_1on1_even_when_a_group_also_matches():
    assert resolve("alice") == "alice-1on1"


def test_a_chat_name_finds_the_group():
    assert resolve("project a (management)") == "project-a-mgmt"


def test_a_conversation_id_is_used_as_is():
    assert resolve("project-a-group") == "project-a-group"


def test_a_chat_the_bot_is_not_in_says_so():
    with pytest.raises(names.NotFound, match="Toucan is not in the chat with Carol Example"):
        resolve("carol")


def test_several_matches_are_listed_not_guessed():
    with pytest.raises(names.NotFound, match="several chats"):
        resolve("project a")


def test_an_unknown_name_matches_nothing():
    with pytest.raises(names.NotFound, match="no Teams chat"):
        resolve("nobody")


def test_old_personal_chat_resolves_before_and_after_migration(tmp_path, monkeypatch):
    import sqlite3
    from toucan_bot import config, store
    monkeypatch.setenv('TOUCAN_BOT_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('TOUCAN_CONFIG', str(tmp_path / 'absent.toml'))
    with sqlite3.connect(config.db_path()) as con:
        con.executescript('''CREATE TABLE conversations (
          id TEXT PRIMARY KEY, type TEXT, name TEXT, service_url TEXT NOT NULL,
          tenant_id TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
          CREATE TABLE messages (conversation_id TEXT, id TEXT, ts TEXT NOT NULL,
          sender_aad_id TEXT, sender_name TEXT, text TEXT NOT NULL, mentioned INTEGER NOT NULL,
          reply_to_id TEXT, received_at TEXT NOT NULL, PRIMARY KEY(conversation_id,id));
          INSERT INTO conversations VALUES ('personal','personal',NULL,'https://example.com',NULL,'now','now');
          INSERT INTO messages VALUES ('personal','m1','2026-10-24T10:00:00Z',
          '00000000-0000-0000-0000-000000000001','Alice Example','hello',0,NULL,'now');
          INSERT INTO messages VALUES ('personal','bot-message','2026-10-24T10:01:00Z',
          NULL,'Toucan','hello',0,NULL,'now');''')
    with pytest.raises(names.NotFound, match='--conversation'):
        names.resolve('Alice', ['personal'], names.load_chats())
    with store.connect(config.db_path()) as con:
        members = con.execute('SELECT aad_id,name FROM members').fetchall()
        assert [tuple(row) for row in members] == [('00000000-0000-0000-0000-000000000001', 'Alice Example')]
        con.execute('DELETE FROM members')
    with pytest.raises(names.NotFound, match='--conversation'):
        names.resolve('Alice', ['personal'], names.load_chats())
