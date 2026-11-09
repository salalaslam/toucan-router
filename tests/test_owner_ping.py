"""The owner sends "ping" to the assistant number and gets one "pong" back."""
from conftest import OWNER, PHONE


def owner_says(world, msg_id, text, sender=OWNER, **values):
    world.wa(msg_id, chat_jid=OWNER, sender=sender, sender_name='Example Owner', text=text, **values)
    world.advance(60)
    return world.app.sync()


def pongs(world):
    return [argv for argv in world.sends if any(str(a).startswith('pong ') for a in argv)]


def test_ping_gets_one_pong_with_status(world):
    world.seed()
    result = owner_says(world, 'p1', 'Ping!')
    assert result['pings'] == ['p1']
    assert len(pongs(world)) == 1
    assert '0 open requests, 0 cards waiting on you.' in pongs(world)[0][-1]
    assert OWNER in pongs(world)[0]
    world.advance(60)
    assert world.app.sync()['pings'] == []
    assert len(pongs(world)) == 1


def test_only_a_bare_ping_from_the_owner_is_answered(world):
    world.seed()
    owner_says(world, 'p1', 'ping the team about the release')
    owner_says(world, 'p2', 'ping', from_me=1, sender=None)
    owner_says(world, 'p3', 'ping', is_forwarded=1)
    assert pongs(world) == []


def test_old_ping_gets_no_pong(world):
    world.seed()
    world.wa('old', chat_jid=OWNER, sender=OWNER, text='ping', ts=world.now - 3600)
    world.advance(60)
    assert world.app.sync()['pings'] == []
    assert pongs(world) == []


def test_ping_from_someone_else_is_ignored(world):
    world.seed()
    world.wa('p1', chat_jid=PHONE, sender=PHONE, text='ping')
    world.advance(60)
    assert world.app.sync().get('pings') == []
    assert pongs(world) == []
