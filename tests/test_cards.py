"""Decision cards: the escalation gate, answers by card ID, "more", the daily list and re-triage."""
import subprocess

import pytest

from conftest import OWNER, PHONE
from toucan.config import Refused

CARD = dict(category='deploy', want='Ship the billing fix', question='Release PR 109 to production now?',
            options=['Yes, release now', 'Wait for PR 110'], pick='A')


def escalate(world, msg_id='req', note='PR 109 is reviewed; 110 is still open.', **card):
    req = world.request(msg_id)
    fields = {**CARD, **card}
    result = world.app.escalate(req, world.app.claim([req], 'project-a'), note=note, **fields)
    return req, result


def owner_says(world, msg_id, text, **values):
    world.wa(msg_id, chat_jid=OWNER, sender=OWNER, sender_name='Example Owner', text=text, **values)
    world.advance(3 * 60)
    return world.app.sync()


def stored_answer(world, req):
    return [m['text'] for m in world.app.rows(
        'SELECT m.text FROM owner_answers a JOIN messages m ON m.id=a.message_id WHERE a.request_id=? ORDER BY m.id', (req,))]


@pytest.mark.parametrize('change,problem', [
    (dict(category='curious'), '--category must be one of'),
    (dict(question='x' * 161), 'keep it under 160'),
    (dict(options=['Only one']), 'give 2 to 4 --option choices, not 1'),
    (dict(options=['a', 'b', 'c', 'd', 'e']), 'not 5'),
    (dict(pick='C'), '--pick must be the letter'),
    (dict(want=''), '--want is required'),
])
def test_the_gate_refuses_anything_but_a_short_decision_and_sends_nothing(world, change, problem):
    world.seed()
    with pytest.raises(Refused, match=problem):
        escalate(world, **change)
    assert world.sends == [] and world.app.rows('SELECT * FROM cards') == []


def test_a_card_is_short_and_letter_answers_spell_out_the_option(world):
    world.seed()
    req, result = escalate(world)
    assert result['card'] == f'T{req}' and world.app.get(req)['state'] == 'escalated'
    assert world.sends[-1][7] == (f'*T{req}* · project-a · Alice Example\nWants: Ship the billing fix\n'
                                  'Decide: Release PR 109 to production now?\nA) Yes, release now  ← suggested\n'
                                  f'B) Wait for PR 110\nReply "T{req} A", quote this, or "more"')
    assert 'PR 109 is reviewed' not in world.sends[-1][7]
    assert owner_says(world, 'answer', f't{req} b, and tell Alice')['answers'] == [req]
    assert stored_answer(world, req) == ['B) Wait for PR 110. and tell Alice']
    assert world.app.get(req)['state'] == 'new'
    assert world.app.rows('SELECT answered_at FROM cards')[0]['answered_at'] is not None


def test_a_card_id_answers_that_card_even_with_several_open(world):
    world.seed()
    first, _ = escalate(world, 'one')
    world.send_result = subprocess.CompletedProcess([], 0, '{"data":{"MsgID":"second-id"}}', '')
    second, _ = escalate(world, 'two', question='Give Alice staging access?', category='access')
    assert owner_says(world, 'answer', f'T{first} A')['answers'] == [first]
    assert world.app.get(second)['state'] == 'escalated'
    assert stored_answer(world, first) == ['A) Yes, release now']


def test_unknown_card_id_gets_one_line_back(world):
    world.seed()
    req, _ = escalate(world)
    assert owner_says(world, 'typo', 'T999 A')['answers'] == []
    assert world.sends[-1][7] == f"T999 isn't waiting on you. Open: T{req}."


def test_more_sends_the_detail_once_and_a_quote_on_it_answers_the_card(world):
    world.seed()
    req, _ = escalate(world)
    world.wa('sent-id', chat_jid=OWNER, sender=None, from_me=1, text='card')
    world.send_result = subprocess.CompletedProcess([], 0, '{"data":{"MsgID":"detail-id"}}', '')
    assert owner_says(world, 'more', 'More', quoted_msg_id='sent-id')['answers'] == []
    detail = world.sends[-1][7]
    assert detail.startswith(f'*T{req}* detail · project-a') and 'PR 109 is reviewed; 110 is still open.' in detail
    assert world.app.get(req)['state'] == 'escalated'
    sends = len(world.sends)
    world.app.sync()
    assert len(world.sends) == sends
    world.wa('detail-id', chat_jid=OWNER, sender=None, from_me=1, text=detail)
    assert owner_says(world, 'go', 'a', quoted_msg_id='detail-id')['answers'] == [req]
    assert stored_answer(world, req) == ['A) Yes, release now']


def test_more_with_an_id(world):
    world.seed()
    req, _ = escalate(world)
    owner_says(world, 'more', f'more T{req}')
    assert world.sends[-1][7].startswith(f'*T{req}* detail')
    owner_says(world, 'more2', f'T{req} more')
    assert world.sends[-1][7].startswith(f'*T{req}* detail') and world.app.get(req)['state'] == 'escalated'


def test_asking_the_same_question_again_adds_to_the_card_silently_and_a_new_question_is_a_new_card(world):
    world.seed()
    req, _ = escalate(world)
    owner_says(world, 'answer', f'T{req} A')
    claim = world.app.claim([req], 'project-a')
    sends = len(world.sends)
    result = world.app.escalate(req, claim, note='Release failed a check; same question.', **CARD)
    assert result['sent'] is False and result['outcome'] == 'updated' and len(world.sends) == sends
    assert world.app.get(req)['state'] == 'escalated'
    assert f'T{req}' in world.app.digest()['waiting']
    owner_says(world, 'more', f'more T{req}')
    assert 'Release failed a check; same question.' in world.sends[-1][7]
    sends = len(world.sends)
    owner_says(world, 'answer2', f'T{req} B')
    claim = world.app.claim([req], 'project-a')
    world.app.escalate(req, claim, note='', **{**CARD, 'question': 'Roll back the failed release?', 'category': 'deploy'})
    assert len(world.sends) == sends + 1 and 'Decide: Roll back the failed release?' in world.sends[-1][7]
    assert len(world.app.rows('SELECT * FROM cards WHERE closed_at IS NULL')) == 1


def test_digest_lists_waiting_cards_and_counts_what_threads_settled_yesterday(world):
    world.seed()
    assert world.app.digest() == dict(waiting=[], handled_yesterday=0, sent=False, text=None)
    done = world.request('done')
    world.app.transition(done, world.app.claim([done], 'project-a'), 'close', 'answered in chat')
    req, _ = escalate(world)
    world.advance(86400)
    sends = len(world.sends)
    report = world.app.digest()
    assert report['waiting'] == [f'T{req}'] and report['handled_yesterday'] == 1 and report['sent']
    assert len(world.sends) == sends + 1
    text = world.sends[-1][7]
    assert text.startswith('*Waiting on you: 1*') and 'Release PR 109 to production now?' in text
    assert 'A) Yes, release now ←' in text and '1 handled without you yesterday.' in text
    owner_says(world, 'answer', f'T{req} A')
    assert world.app.digest()['sent'] is False


def test_retriage_hands_old_escalations_back_and_their_new_cards_wait_for_the_digest(world):
    world.seed()
    old = world.request('old')
    world.app.deliver(old, world.app.claim([old], 'project-a'), text='Old-style escalation', escalation=True)
    world.db.execute('DELETE FROM cards')  # as if it was escalated before cards existed
    assert world.app.retriage() == dict(requests=[old], threads=['project-a'])
    assert world.app.get(old)['state'] == 'new'
    sends = len(world.sends)
    result = world.app.escalate(old, world.app.claim([old], 'project-a'), **CARD)
    assert result['outcome'] == 'held' and len(world.sends) == sends and world.app.get(old)['state'] == 'escalated'
    assert world.app.digest()['waiting'] == [f'T{old}'] and len(world.sends) == sends + 1
    assert world.app.rows('SELECT held FROM cards') == [dict(held=0)]
    assert owner_says(world, 'answer', f'T{old} B')['answers'] == [old]


def test_drafts_go_to_the_drafts_chat_when_set(world):
    world.cfg['settings']['drafts_whatsapp'] = '120363000000000001@g.us'
    world.cfg['routes'][0]['mode'] = 'draft'
    world.seed()
    req = world.request()
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Here is the draft')
    assert world.sends[-1][5] == '120363000000000001@g.us' and world.sends[-1][7].startswith('Draft for')
    assert PHONE != world.sends[-1][5]


# Regressions found in review

def test_an_old_answer_is_not_read_again_against_a_newer_card(world):
    world.seed()
    req, _ = escalate(world)
    owner_says(world, 'answer', f'T{req} B')
    claim = world.app.claim([req], 'project-a')
    world.app.escalate(req, claim, **{**CARD, 'question': 'Roll back instead?', 'options': ['Roll back', 'Keep it']})
    assert world.app.sync()['answers'] == [] and world.app.get(req)['state'] == 'escalated'
    assert stored_answer(world, req) == ['B) Wait for PR 110']


def test_held_cards_stay_in_later_lists_until_answered(world):
    world.seed()
    old = world.request('old')
    world.app.deliver(old, world.app.claim([old], 'project-a'), text='Old-style escalation', escalation=True)
    world.db.execute('DELETE FROM cards')
    world.app.retriage()
    world.app.escalate(old, world.app.claim([old], 'project-a'), **CARD)
    assert world.app.digest()['sent']
    world.advance(86400)
    assert world.app.digest()['waiting'] == [f'T{old}']


def test_a_sentence_starting_with_a_is_not_option_a(world):
    world.seed()
    req, _ = escalate(world)
    owner_says(world, 'answer', 'a bit later, after lunch')
    assert stored_answer(world, req) == ['a bit later, after lunch']


def test_an_answer_to_a_closed_card_gets_no_notice_later(world):
    world.seed()
    first, _ = escalate(world, 'one')
    owner_says(world, 'answer', f'T{first} A')
    world.app.deliver(first, world.app.claim([first], 'project-a'), text='Released')
    escalate(world, 'two', question='Give Alice staging access?', category='access')
    sends = len(world.sends)
    world.advance(3 * 60)
    world.app.sync()
    assert len(world.sends) == sends


def test_more_is_consumed_and_never_becomes_a_request_or_inbox_context(world):
    world.seed()
    req, _ = escalate(world)
    owner_says(world, 'more', f'more T{req}')
    row = world.db.execute("SELECT route FROM messages WHERE msg_id='more'").fetchone()
    assert row['route'] is None and world.app.rows('SELECT count(*) AS n FROM requests')[0]['n'] == 1
    assert all(m['msg_id'] != 'more' for m in world.app.inbox('project-a'))


def test_a_forwarded_message_starting_with_a_card_id_is_not_an_answer(world):
    world.seed()
    req, _ = escalate(world)
    assert owner_says(world, 'fwd', f'T{req} B', is_forwarded=1)['answers'] == []
    assert world.app.get(req)['state'] == 'escalated'


def test_resume_needs_an_owner_message_after_a_silent_update(world):
    world.seed()
    req, _ = escalate(world)
    world.t3('go', world.now + 1, 'go ahead')
    world.advance(10)
    claim = world.app.resume(req, 'project-a')['claim_id']
    world.advance(60)
    world.app.escalate(req, claim, note='still blocked', **CARD)
    with pytest.raises(Refused, match='not written'):
        world.app.resume(req, 'project-a')
