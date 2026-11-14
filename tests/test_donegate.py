"""The done gate: "done", "fixed", "ready" or "live" to a person needs proof or an honest "not checked yet"."""
import json
import subprocess
from datetime import datetime, timezone

import pytest

from conftest import PHONE
from toucan.config import Refused, ValidationError, load
from toucan.donegate import claims

PROOF = 'https://github.com/example/repo/issues/12#issuecomment-99'


def enforce(world, mode='enforce'):
    world.cfg['settings']['done_gate'] = mode
    world.seed()
    req = world.request()
    return req, world.app.claim([req], 'project-a')


def comment(world, posted, body='Checked on dev with your file:\n\n![screenshot](https://example.invalid/s.png)',
            issue=12, code=0):
    calls, plain = [], world.fx.run

    def run(argv, timeout=None):
        if argv[0] != 'gh':
            return plain(argv)
        calls.append(argv)
        stamp = datetime.fromtimestamp(posted, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        data = dict(created_at=stamp, body=body, issue_url=f'https://api.github.com/repos/example/repo/issues/{issue}')
        return subprocess.CompletedProcess(argv, code, json.dumps(data) if code == 0 else '', '')
    world.fx.run = run
    return calls


def gate_events(world):
    return [json.loads(e['detail_json']) for e in world.app.rows("SELECT detail_json FROM events WHERE kind='done_gate' ORDER BY id")]


def test_off_by_default(world):
    world.seed()
    req = world.request()
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Fixed and live on dev.', keep_open=True)
    assert world.sends and gate_events(world) == []


def test_enforce_refuses_an_unproven_claim_with_no_override(world):
    req, claim = enforce(world)
    with pytest.raises(Refused, match='fixed, live but has no proof.*no override'):
        world.app.deliver(req, claim, text='Fixed and live on dev.', keep_open=True, override='client is waiting')
    assert world.sends == [] and world.app.rows('SELECT * FROM outbox') == []
    [event] = gate_events(world)
    assert event['verdict'] == 'hold' and event['refused'] and event['claims'] == ['Fixed', 'live']


@pytest.mark.parametrize('text', [
    'Deployed to dev, not checked yet. Left to check: your invoice file, by 4 pm.',
    "I'll tell you here once it's live on the main site.",
    "It isn't fixed yet; the cause is the date parser.",
    'Got it. Tell me when the laptop is ready.',
    'The ask link is ready: https://ask.example.invalid/q/7',
    'Starting now.',
])
def test_plans_negations_honest_status_and_delivered_links_pass(world, text):
    req, claim = enforce(world)
    world.app.deliver(req, claim, text=text, keep_open=True)
    assert world.sends[-1][-1] == text


@pytest.mark.parametrize('text', [
    'Ready on dev: https://dev.example.invalid/contracts',
    'The screens now match the approved wireframes.',
    'Done, it works now.',
])
def test_software_claims_need_proof_even_with_a_link(world, text):
    req, claim = enforce(world)
    with pytest.raises(Refused, match='no proof'):
        world.app.deliver(req, claim, text=text, keep_open=True)


def test_proof_comment_after_the_request_lets_it_through(world):
    req, claim = enforce(world)
    calls = comment(world, world.now)
    world.app.deliver(req, claim, text=f'Fixed on dev, checked with your file: {PROOF}', keep_open=True)
    assert calls == [['gh', 'api', 'repos/example/repo/issues/comments/99']]
    assert world.sends and gate_events(world)[-1]['verdict'] == 'pass'


@pytest.mark.parametrize('kw, problem', [
    (dict(posted_offset=-3600), 'posted before this request'),
    (dict(body='Fixed on dev.'), 'no screenshot or output'),
    (dict(issue=13), 'not a comment on #12'),
    (dict(code=1), 'could not read'),
])
def test_weak_proof_is_held(world, kw, problem):
    req, claim = enforce(world)
    offset = kw.pop('posted_offset', 0)
    comment(world, world.now + offset, **kw)
    with pytest.raises(Refused, match=problem):
        world.app.deliver(req, claim, text=f'Fixed on dev: {PROOF}', keep_open=True)
    assert world.sends == []


def test_shadow_records_the_hold_and_sends(world):
    req, claim = enforce(world, 'shadow')
    world.app.deliver(req, claim, text='Fixed and live.', keep_open=True)
    assert world.sends[-1][5] == PHONE
    [event] = gate_events(world)
    assert event['verdict'] == 'hold' and event['mode'] == 'shadow' and not event.get('refused')


def test_drafts_and_escalations_to_the_owner_are_not_gated(world):
    req, claim = enforce(world)
    world.app.deliver(req, claim, text='Is it fixed and live? Need your call.', escalation=True)
    assert gate_events(world) == []


def test_bad_mode_is_refused(world):
    world.config_path.write_text(world.config_text.replace('done_gate = "off"', 'done_gate = "on"', 1))
    with pytest.raises(ValidationError, match='done_gate'):
        load(world.config_path)


def test_claims_ignore_plans_and_negations():
    assert claims("Once it's live I'll say. It is not fixed.") == ([], [])
    assert claims('Ready on dev. The report is done.') == (['Ready'], ['done'])
    assert claims("The screens don't match the approved wireframes. It doesn\u2019t work now. I won't say done yet.") == ([], [])
