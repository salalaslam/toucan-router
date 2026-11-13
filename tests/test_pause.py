import json

import pytest

from toucan.cli import main
from toucan.config import ValidationError
from test_cards import escalate


def cli(world, monkeypatch, capsys, *args):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    capsys.readouterr()
    code = main([*args, '--json'], effects=world.fx, paths=world.paths)
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_paused_sync_reads_nothing_and_status_shows_the_pause(world, monkeypatch, capsys):
    world.seed()
    code, result = cli(world, monkeypatch, capsys, 'pause', '--reason', 'maintenance')
    assert code == 0 and result['paused']['reason'] == 'maintenance' and not result['already_paused']
    world.wa('during-pause')
    posts, sends = len(world.posts), len(world.sends)
    code, report = cli(world, monkeypatch, capsys, 'sync')
    assert code == 0 and report['paused']['reason'] == 'maintenance'
    assert report['ingested'] == 0 and report['created'] == [] and report['problems'] == {}
    assert len(world.posts) == posts and len(world.sends) == sends
    assert world.app.rows("SELECT * FROM messages WHERE msg_id='during-pause'") == []
    assert cli(world, monkeypatch, capsys, 'status')[1]['paused']['reason'] == 'maintenance'


def test_unpause_catches_up_on_messages_from_the_pause(world):
    world.seed()
    world.app.pause_routing()
    world.wa('during-pause')
    assert world.app.sync()['paused']
    world.advance(600)
    result = world.app.unpause_routing()
    assert result['unpaused'] and 'warning' not in result
    created = world.app.sync()['created']
    assert len(created) == 1 and world.app.get(created[0])['state'] == 'new'
    assert len(world.hooks()) == 1
    kinds = [e['kind'] for e in world.app.rows("SELECT kind FROM events WHERE kind LIKE 'routing_%' ORDER BY id")]
    assert kinds == ['routing_paused', 'routing_unpaused']
    assert world.app.unpause_routing() == dict(unpaused=False, paused=None)


def test_pause_until_ends_by_itself_at_the_next_sync(world, monkeypatch, capsys):
    world.seed()
    assert cli(world, monkeypatch, capsys, 'pause', '--until', '30m')[0] == 0
    world.wa('during-pause')
    world.advance(10 * 60)
    assert world.app.sync()['paused']
    world.advance(21 * 60)
    report = world.app.sync()
    assert report['resumed']['unpaused'] and len(report['created']) == 1
    assert world.app.routing_pause() is None
    event = world.app.rows("SELECT detail_json FROM events WHERE kind='routing_unpaused'")[-1]
    assert json.loads(event['detail_json'])['automatic'] is True


def test_pausing_again_keeps_the_start_and_replaces_end_and_reason(world):
    world.seed()
    first = world.app.pause_routing(world.now + 3600, 'first')['paused']
    world.advance(60)
    again = world.app.pause_routing(None, 'second')
    assert again['already_paused'] and again['paused'] == dict(since=first['since'], until=None, reason='second')
    with pytest.raises(ValidationError):
        world.app.pause_routing(world.now - 1)


def test_long_pause_warns_that_old_whatsapp_and_email_messages_are_skipped(world):
    world.seed()
    world.app.pause_routing()
    world.advance(49 * 3600)
    assert 'older than that will not become requests' in world.app.unpause_routing()['warning']


def test_digest_waits_while_paused_and_keeps_the_cards(world):
    world.seed()
    req, _ = escalate(world)
    world.advance(86400)
    world.app.pause_routing()
    sends = len(world.sends)
    report = world.app.digest()
    assert report['paused'] and not report['sent'] and len(world.sends) == sends
    world.app.unpause_routing()
    assert world.app.digest()['waiting'] == [f'T{req}']
