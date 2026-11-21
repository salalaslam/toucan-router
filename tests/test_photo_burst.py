import json
import sqlite3

from toucan import router
from toucan.cli import main
from toucan.core import App
from toucan.storage import connect
from conftest import OTHER, PHONE

PHOTO = dict(text='', display_text='Sent image', media_type='image', filename='photo.jpg')


def burst(world, seconds=30):
    route = next(r for r in world.cfg['routes'] if r['name'] == 'alice-whatsapp')
    route['photo_burst_seconds'] = seconds
    world.seed()
    world.advance(30)  # messages a little in the past still come after the seed
    return route


def linked(world):
    return {r['msg_id']: r['request_id'] for r in world.app.rows(
        'SELECT m.msg_id,l.request_id FROM request_messages l JOIN messages m ON m.id=l.message_id')}


def test_caption_less_photos_after_a_request_join_it_and_show_in_its_inbox(world):
    burst(world)
    world.wa('text', ts=world.now - 20, text='Add this product please')
    world.wa('photo-2', ts=world.now - 5, **PHOTO)  # read before photo-1: the merge goes in time order
    world.wa('photo-1', ts=world.now - 18, **PHOTO)
    result = world.app.sync()
    assert len(result['created']) == 1
    req = result['created'][0]
    route = next(r for r in result['routes'] if r['route'] == 'alice-whatsapp')
    assert [m['msg_id'] for m in route['merged']] == ['photo-1', 'photo-2']
    assert linked(world) == {'photo-1': req, 'photo-2': req}
    # Every message is kept, and the inbox lists both photos under the one request.
    inbox = [m for m in world.app.inbox('project-a') if m['chat'] == PHONE]
    assert [(m['msg_id'], m['request_id'], m['merged_into']) for m in inbox] == [
        ('text', req, None), ('photo-1', req, req), ('photo-2', req, req)]
    assert json.loads(inbox[1]['attachments_json']) == [dict(media_type='image', filename='photo.jpg')]
    assert world.hooks()[0]['count'] == 1


def test_a_photo_in_a_later_sync_joins_a_request_that_is_still_new(world):
    burst(world)
    req = world.request('text')
    world.app.inbox('project-a')
    world.advance(10)
    world.wa('photo', **PHOTO)
    assert world.app.sync()['created'] == []
    assert linked(world) == {'photo': req}
    # The thread already read the inbox, so `toucan wait` reports the change; the wake budget is unchanged.
    row = world.app.get(req)
    assert row['change_pending'] == 1 and row['wakes'] == 1
    assert world.app.rows("SELECT kind FROM events WHERE kind='photo_merged'") == [dict(kind='photo_merged')]


def test_photos_never_join_claimed_late_split_or_other_peoples_requests(world):
    burst(world)
    req = world.request('text')
    world.app.claim([req], 'project-a')
    world.advance(5)
    world.wa('after-claim', **PHOTO)
    world.advance(60)
    world.wa('second', text='Another question')
    world.advance(40)
    world.wa('too-late', **PHOTO)  # 40 seconds after the request before it
    world.wa('from-bob', sender=OTHER, **PHOTO)  # alice's chat, but another person
    world.wa('quote', quoted_msg_id='second', **PHOTO)  # a reply to a message is its own request
    world.wa('caption', **dict(PHOTO, text='This one in blue'))
    created = world.app.sync()['created']
    assert linked(world) == {}
    assert len(created) == 5  # after-claim, second, too-late, quote, caption; from-bob is not a requester


def test_a_request_split_across_threads_or_held_by_the_router_takes_no_photos(world):
    burst(world)
    req = world.request('text')
    world.db.execute("INSERT INTO requests (message_id,route,thread,state,created_at) "
                     "SELECT message_id,route,'project-b',state,created_at FROM requests WHERE id=?", (req,))
    world.advance(5)
    world.wa('photo', **PHOTO)
    assert len(world.app.sync()['created']) == 1
    world.advance(60)
    held = world.request('held')
    world.db.execute("UPDATE requests SET state='held',routing='pending' WHERE id=?", (held,))
    world.advance(5)
    world.wa('photo-2', **PHOTO)
    assert len(world.app.sync()['created']) == 1
    assert linked(world) == {}


def test_off_by_default_and_text_always_makes_its_own_request(world):
    world.seed()
    world.advance(30)
    world.wa('text', ts=world.now - 5, text='First')
    world.wa('photo', **PHOTO)
    assert len(world.app.sync()['created']) == 2
    world.cfg['routes'][0]['photo_burst_seconds'] = 30
    world.advance(5)
    world.wa('more-text', text='And also this')
    assert len(world.app.sync()['created']) == 1
    assert linked(world) == {}


def test_edits_and_downloads_of_a_merged_photo_reach_its_request(world):
    burst(world)
    req = world.request('text')
    world.advance(5)
    world.wa('photo', **PHOTO)
    world.app.sync()
    world.app.inbox('project-a')
    with sqlite3.connect(world.wa_path) as db:
        db.execute("UPDATE messages SET local_path='/tmp/photo.jpg' WHERE msg_id='photo'")
    world.app.sync()
    assert world.app.get(req)['change_pending'] == 1
    assert world.app.rows("SELECT kind FROM events WHERE kind='attachment_ready'") == [dict(kind='attachment_ready')]
    world.app.inbox('project-a')
    with sqlite3.connect(world.wa_path) as db:
        db.execute("UPDATE messages SET revoked=1 WHERE msg_id='photo'")
    world.app.sync()
    assert world.app.get(req)['source_changed'] == 1


def test_a_quote_of_a_merged_photo_leads_to_its_request(world):
    burst(world)
    req = world.request('text')
    world.advance(5)
    world.wa('photo', **PHOTO)
    world.app.sync()
    m = dict(source='whatsapp', route='alice-whatsapp', quoted_ids=json.dumps(['photo']), request=-1)
    assert router.linked(world.db, m) == (req, 'project-a')


def test_dry_run_merges_in_its_copy_only(world):
    burst(world)
    world.request('text')
    world.advance(5)
    world.wa('photo', **PHOTO)
    before = list(world.db.iterdump())
    dry_db = connect(world.db_path, dry=True)
    try:
        result = App(world.cfg, dry_db, world.fx, world.paths, dry=True).sync()
        assert result['created'] == []
        assert [m['msg_id'] for r in result['routes'] for m in r['merged']] == ['photo']
    finally:
        dry_db.close()
    assert list(world.db.iterdump()) == before


def test_inbox_cli_marks_a_merged_photo(world, monkeypatch, capsys):
    world.config_path.write_text(world.config_text.replace(
        'requesters = ["alice"]\n', 'requesters = ["alice"]\nphoto_burst_seconds = 30\n', 1))
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    world.seed()
    world.advance(30)
    world.wa('text', ts=world.now - 5, text='Add this product please')
    world.wa('photo', **PHOTO)
    assert main(['sync', '--json'], effects=world.fx, paths=world.paths) == 0
    capsys.readouterr()
    assert main(['inbox', '--thread', 'project-a'], effects=world.fx, paths=world.paths) == 0
    out = capsys.readouterr().out
    assert 'a photo sent right after its message, part of the same request' in out
