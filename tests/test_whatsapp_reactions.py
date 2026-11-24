import subprocess

import pytest

from toucan.config import ValidationError, load, whatsapp_emoji
from toucan.core import App
from toucan.storage import connect
from conftest import LID, OTHER

PHOTO = dict(text='', display_text='Sent image', media_type='image', filename='photo.jpg')
EYES, CHECK = '\U0001F440', '✅'
GROUP_CHAT = '120363000000000001@g.us'


def route(world, name='alice-whatsapp'):
    return next(r for r in world.cfg['routes'] if r['name'] == name)


def stages(world, ack='eyes', work='', done='check'):
    route(world)['stage_reactions'] = True
    world.cfg['settings'].update(ack_emoji=ack, work_emoji=work, done_emoji=done)


def reacts(world, since=0):
    """(message, reaction) per wacli reaction call; an empty reaction removes Toucan's."""
    return [(s[s.index('--id') + 1], s[s.index('--reaction') + 1]) for s in world.sends if s[2:4] == ['send', 'react']][since:]


def events(world, *kinds):
    return [(e['kind'], e['emoji']) for e in world.app.rows(
        "SELECT kind,json_extract(detail_json,'$.emoji') AS emoji FROM events WHERE kind IN (%s) ORDER BY id"
        % ','.join('?' * len(kinds)), kinds)]


def test_off_by_default_on_whatsapp_routes(world):
    world.cfg['settings'].update(ack_emoji='eyes', done_emoji='check')
    world.seed()
    req = world.request()
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Here it is')
    world.advance(60)
    world.app.sync()
    assert reacts(world) == []


def test_received_then_done_with_no_working_stage(world):
    stages(world)
    world.seed()
    req = world.request()
    assert reacts(world) == [('req', EYES)]
    assert world.sends[0] == ['wacli', '--json', 'send', 'react', '--to', '15550000001@s.whatsapp.net', '--id', 'req', '--reaction', EYES]
    claim = world.app.claim([req], 'project-a')
    world.advance(60)
    world.app.sync()
    assert reacts(world, 1) == []  # an empty work_emoji keeps the received reaction
    world.app.deliver(req, claim, text='Here it is')
    world.advance(60)
    world.app.sync()
    # WhatsApp keeps one reaction per sender: the check replaces the eyes, with no call to remove them.
    assert reacts(world, 1) == [('req', CHECK)]
    assert events(world, 'reaction_added', 'reaction_removed') == [
        ('reaction_added', 'eyes'), ('reaction_added', 'check'), ('reaction_removed', 'eyes')]
    world.advance(60)
    world.app.sync()
    assert len(reacts(world)) == 2  # settled


def test_a_working_stage_when_one_is_set(world):
    stages(world, work='eye')
    world.seed()
    req = world.request()
    world.app.claim([req], 'project-a')
    world.advance(60)
    world.app.sync()
    assert reacts(world) == [('req', EYES), ('req', '\U0001F441️')]


def test_only_the_newest_message_in_a_burst_has_the_reaction(world):
    stages(world)
    world.seed()
    first = world.request('m1')
    world.advance(30)
    world.wa('m2')
    world.app.sync()
    assert reacts(world) == [('m1', EYES), ('m2', EYES), ('m1', '')]  # the new one goes on first
    second = world.app.rows('SELECT id FROM requests ORDER BY id')[-1]['id']
    world.advance(60)
    world.app.deliver(first, world.app.claim([first], 'project-a'), text='One')
    world.advance(60)
    world.app.sync()
    assert reacts(world, 3) == []  # m2 is still open, so the burst stays at received
    world.app.deliver(second, world.app.claim([second], 'project-a'), text='Two')
    world.advance(60)
    world.app.sync()
    assert reacts(world, 3) == [('m2', CHECK)]


def test_an_albums_newest_photo_carries_the_reaction(world):
    stages(world)
    route(world)['photo_burst_seconds'] = 30
    world.seed()
    world.advance(30)
    world.wa('text', ts=world.now - 20, text='Add this product please')
    world.wa('photo-1', ts=world.now - 18, **PHOTO)
    world.wa('photo-2', ts=world.now - 16, **PHOTO)
    req, = world.app.sync()['created']
    assert reacts(world) == [('photo-2', EYES)]  # not on the text or the first photo
    world.advance(10)
    world.wa('photo-3', **PHOTO)  # joins the still-new request in a later sync
    assert world.app.sync()['created'] == []
    assert reacts(world, 1) == [('photo-3', EYES), ('photo-2', '')]
    world.advance(60)
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Added')
    world.advance(60)
    world.app.sync()
    assert reacts(world, 3) == [('photo-3', CHECK)]


def test_a_photo_stored_under_the_chats_other_jid_is_the_same_burst(world):
    stages(world)
    route(world)['photo_burst_seconds'] = 30
    world.seed()
    world.advance(30)
    world.wa('text', sender=LID, chat_jid=LID, ts=world.now - 5, text='Add this product please')
    world.wa('photo', **PHOTO)  # the same 1:1 chat, stored under the phone JID
    world.app.sync()
    assert reacts(world) == [('photo', EYES)]


def test_closed_without_a_reply_loses_the_reaction(world):
    stages(world)
    world.seed()
    req = world.request()
    world.app.transition(req, world.app.claim([req], 'project-a'), 'close', 'just thanks', thread='project-a')
    world.advance(60)
    world.app.sync()
    assert reacts(world) == [('req', EYES), ('req', '')]
    world.advance(60)
    world.app.sync()
    assert len(reacts(world)) == 2


def test_group_messages_name_their_sender(world):
    world.cfg['routes'].append(dict(route(world), name='alice-group', chats=[GROUP_CHAT], stage_reactions=True))
    stages(world, done='')
    world.seed()
    world.wa('in-group', chat_jid=GROUP_CHAT, sender=OTHER)
    world.cfg['routes'][-1]['requesters'] = ['alice', 'other']
    world.app.sync()
    assert world.sends[-1] == ['wacli', '--json', 'send', 'react', '--to', GROUP_CHAT, '--id', 'in-group', '--reaction', EYES,
                               '--sender', OTHER]


def test_failed_reactions_retry_then_give_up_and_never_block_the_reply(world):
    stages(world)
    world.seed()
    world.send_result = subprocess.CompletedProcess([], 1, '', 'not connected')
    req = world.request()
    for _ in range(3):
        world.advance(60)
        report = world.app.sync()
    assert reacts(world) == [('req', EYES)] * 3
    assert 'WhatsApp (wacli) may be down' in report['problems'][next(k for k in report['problems'] if k.startswith('ack:'))]
    world.send_result = subprocess.CompletedProcess([], 0, '{"data":{"MsgID":"sent-id"}}', '')
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Here it is')
    assert world.app.get(req)['state'] == 'closed'
    world.advance(60)
    world.app.sync()
    assert reacts(world, 3) == [('req', CHECK)]  # a new stage gets its own tries


def test_a_hung_reaction_is_uncertain_and_cleared_later(world):
    stages(world)
    world.seed()
    world.send_error = subprocess.TimeoutExpired(['wacli'], 20)
    req = world.request()
    world.send_error = None
    world.app.transition(req, world.app.claim([req], 'project-a'), 'close', 'just thanks', thread='project-a')
    world.advance(60)
    world.app.sync()
    # The eyes may have gone on, so they are removed.
    assert reacts(world) == [('req', EYES), ('req', '')]
    assert events(world, 'reaction_uncertain', 'reaction_removed') == [('reaction_uncertain', 'eyes'), ('reaction_removed', 'eyes')]


def test_dry_run_sends_no_reaction(world):
    stages(world)
    world.seed()
    world.wa('req')
    dry_db = connect(world.db_path, dry=True)
    try:
        assert App(world.cfg, dry_db, world.fx, world.paths, dry=True).sync()['acks'] == []
    finally:
        dry_db.close()
    assert reacts(world) == []


@pytest.mark.parametrize('value,emoji', [
    ('eyes', EYES), ('check', CHECK), ('done', CHECK), ('thumbsup', '\U0001F44D'), ('1f440_eyes', EYES),
    ('2705_whiteheavycheckmark', CHECK), ('holdon', '\u23F3'), (CHECK, CHECK), ('custom_reaction', None), ('', None)])
def test_stage_emoji_settings_map_to_whatsapp_emoji(value, emoji):
    assert whatsapp_emoji(value) == emoji


@pytest.mark.parametrize('old,new,match', [
    ('chats = ["group:chat"]', 'chats = ["group:chat"]\nstage_reactions = true', 'only on WhatsApp act routes'),
    ('source = "whatsapp"', 'source = "whatsapp"\nstage_reactions = "yes"', 'stage_reactions must be true or false'),
    ('source = "whatsapp"', 'source = "whatsapp"\nstage_reactions = true\nreply_to = "alice"', 'without reply_to'),
    ('mode = "act"\nrequesters = ["alice"]\nactive_from = "2026-10-17T00:00:00+00:00"\n',
     'mode = "draft"\nrequesters = ["alice"]\nactive_from = "2026-10-17T00:00:00+00:00"\nstage_reactions = true\n', 'act routes'),
    ('source = "whatsapp"', 'source = "whatsapp"\nstage_reactions = true', None),
])
def test_stage_reactions_config(world, old, new, match):
    assert old in world.config_text
    world.config_path.write_text(world.config_text.replace(old, new, 1))
    if match:
        with pytest.raises(ValidationError, match=match):
            load(world.config_path)
    else:
        assert next(r for r in load(world.config_path)['routes'] if r['source'] == 'whatsapp')['stage_reactions'] is True


def test_a_stage_emoji_whatsapp_cannot_show_is_refused(world):
    text = world.config_text.replace('source = "whatsapp"', 'source = "whatsapp"\nstage_reactions = true', 1)
    world.config_path.write_text(text.replace('ack_emoji = ""', 'ack_emoji = "custom_reaction"'))
    with pytest.raises(ValidationError, match='settings.ack_emoji'):
        load(world.config_path)
