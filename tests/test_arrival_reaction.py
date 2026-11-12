import subprocess


def bot(world, emoji='eyes'):
    next(r for r in world.cfg['routes'] if r['name'] == 'group')['post_as_bot'] = True
    world.cfg['settings']['ack_emoji'] = emoji
    world.send_result = subprocess.CompletedProcess([], 0, '{"ok": true}', '')


def reacts(world):
    return [s for s in world.sends if s[2] == 'react']


def test_new_teams_request_gets_eyes_on_arrival(world):
    bot(world)
    world.seed()
    req = world.request(teams=True)
    assert reacts(world) == [['toucan-bot', '--json', 'react', '--conversation', 'group:chat', '--message', 'req', '--emoji', 'eyes']]
    kinds = [e['kind'] for e in world.app.rows("SELECT kind FROM events WHERE kind LIKE 'reaction_%' ORDER BY id")]
    assert kinds == ['reaction_pending', 'reaction_added']
    world.advance(60)
    world.app.sync()
    assert len(reacts(world)) == 1  # not added again
    claim = world.app.claim([req], 'project-a')
    assert world.app.react(req, claim, 'eyes', thread='project-a')['skipped'] == 'already reacted'
    world.app.react(req, claim, 'eyes', remove=True, thread='project-a')
    world.advance(60)
    world.app.sync()
    assert [('--remove' in s) for s in reacts(world)] == [False, True]  # a removed reaction is not put back


def test_no_arrival_reaction_without_bot_route_or_setting(world):
    world.cfg['settings']['ack_emoji'] = 'eyes'
    world.seed()
    world.request(teams=True)  # route does not post as the bot
    world.request('wa-req')  # WhatsApp
    assert reacts(world) == []
    bot(world, emoji='')
    world.advance(60)
    world.request('second', teams=True)
    assert reacts(world) == []


def test_failed_or_hung_reaction_retries_then_gives_up(world):
    bot(world)
    world.seed()
    world.send_error = subprocess.TimeoutExpired(['toucan-bot'], 20)
    world.request(teams=True)
    for _ in range(4):
        world.advance(60)
        report = world.app.sync()
    assert len(reacts(world)) == 3
    assert any(key.startswith('ack:') for key in report['problems'])
    kinds = [e['kind'] for e in world.app.rows("SELECT kind FROM events WHERE kind='reaction_uncertain'")]
    assert len(kinds) == 3


def test_failed_reaction_succeeds_on_a_later_sync(world):
    bot(world)
    world.seed()
    world.send_result = subprocess.CompletedProcess([], 1, '', 'roster failed')
    world.request(teams=True)
    world.send_result = subprocess.CompletedProcess([], 0, '{"ok": true}', '')
    world.advance(60)
    world.app.sync()
    world.advance(60)
    world.app.sync()
    assert len(reacts(world)) == 2
    assert [e['kind'] for e in world.app.rows("SELECT kind FROM events WHERE kind IN ('reaction_failed','reaction_added') ORDER BY id")] == \
        ['reaction_failed', 'reaction_added']


def test_backlog_older_than_two_hours_gets_no_reaction(world):
    bot(world)
    world.seed()
    world.team('old')  # arrives, but sync is down for three hours
    world.advance(3 * 3600)
    assert world.app.sync()['created']
    assert reacts(world) == []
