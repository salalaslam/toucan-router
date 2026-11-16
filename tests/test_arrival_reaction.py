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


def stages(world):
    world.cfg['settings'].update(ack_emoji='eye', work_emoji='eyes', done_emoji='check')


def changes(world, since=0):
    return [(s[s.index('--message') + 1], s[s.index('--emoji') + 1], '--remove' in s) for s in reacts(world)[since:]]


def test_reaction_moves_from_received_to_working_to_done(world):
    bot(world)
    stages(world)
    world.seed()
    req = world.request(teams=True)
    assert changes(world) == [('req', 'eye', False)]
    claim = world.app.claim([req], 'project-a')
    world.advance(60)
    world.app.sync()
    assert changes(world, 1) == [('req', 'eyes', False), ('req', 'eye', True)]  # the new one goes on first
    world.app.deliver(req, claim, text='Here it is')
    world.advance(60)
    world.app.sync()
    assert changes(world, 3) == [('req', 'check', False), ('req', 'eyes', True)]
    world.advance(60)
    world.app.sync()
    assert len(reacts(world)) == 5  # settled: nothing more to change


def test_only_the_newest_message_in_a_burst_has_the_reaction(world):
    bot(world)
    stages(world)
    world.seed()
    first = world.request('m1', teams=True)
    world.advance(30)
    world.team('m2')
    world.app.sync()
    assert changes(world) == [('m1', 'eye', False), ('m2', 'eye', False), ('m1', 'eye', True)]
    claim = world.app.claim([first], 'project-a')
    world.advance(60)
    world.app.sync()
    assert changes(world, 3) == []  # m2 is still unclaimed, so the burst stays at received
    second = world.app.rows("SELECT id FROM requests ORDER BY id")[-1]['id']
    claim2 = world.app.claim([second], 'project-a')
    world.advance(60)
    world.app.sync()
    assert changes(world, 3) == [('m2', 'eyes', False), ('m2', 'eye', True)]
    world.app.deliver(first, claim, text='One')
    world.app.deliver(second, claim2, text='Two')
    world.advance(60)
    world.app.sync()
    assert changes(world, 5) == [('m2', 'check', False), ('m2', 'eyes', True)]
    world.advance(600)
    world.team('m3')  # after the answers: a new burst, and m2 keeps its check
    world.app.sync()
    assert changes(world, 7) == [('m3', 'eye', False)]


def test_owner_reaction_replaces_the_stage_reaction(world):
    bot(world)
    stages(world)
    world.seed()
    req = world.request(teams=True)
    claim = world.app.claim([req], 'project-a')
    world.app.react(req, claim, 'thumbsup', thread='project-a')
    world.app.transition(req, claim, 'close', 'thanks, nothing to answer', thread='project-a')
    world.advance(60)
    world.app.sync()
    assert changes(world) == [('req', 'eye', False), ('req', 'thumbsup', False), ('req', 'eye', True)]
    world.advance(60)
    world.app.sync()
    assert len(reacts(world)) == 3


def test_closed_without_a_reply_loses_the_reaction(world):
    bot(world)
    stages(world)
    world.seed()
    req = world.request(teams=True)
    world.app.transition(req, world.app.claim([req], 'project-a'), 'close', 'tags someone else', thread='project-a')
    world.advance(60)
    world.app.sync()
    assert changes(world) == [('req', 'eye', False), ('req', 'eye', True)]
