import json
import os
import sys

import pytest

from toucan.config import ValidationError, load
from toucan import sources, t3


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    data = tmp_path / 'mail.json'
    data.write_text('[]')
    command = tmp_path / 'notmuch'
    command.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
messages = json.load(open(os.environ['MAIL_FIXTURE']))
if sys.argv[1] == 'search':
    if '--output=summary' in sys.argv:
        print(json.dumps([{'timestamp': max(m['timestamp'] for m in messages)}] if messages else []))
    else:
        print(json.dumps(['id:' + m['id'] for m in messages]))
else:
    mid = sys.argv[-1][3:].strip('"')
    print(json.dumps([[m, []] for m in messages if m['id'] == mid]))
''')
    command.chmod(0o700)
    monkeypatch.setenv('MAIL_FIXTURE', str(data))
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ.get('PATH', ''))
    return data


def email_route(world):
    key = next(iter(world.cfg['people']))
    world.cfg['people'][key]['email'] = ['alice@example.com']
    route = dict(name='mailbox', source='email', chats=['requests@example.com'],
                 thread=next(iter(world.cfg['threads'])), mode='draft', requesters=[key], active_ts=0)
    world.cfg['routes'] = [route]
    return route


def message(mid, now, sender='Alice Example <ALICE@example.com>', headers=None, body=None):
    return dict(id=mid, timestamp=now, match=True,
                headers=dict(From=sender, To='requests@example.com', Subject='Review this change', **(headers or {})),
                body=body or [{'content-type': 'text/plain', 'content': 'Check the invoice.'}])


def test_email_allowed_sender_requests_and_replies_as_draft(world, mailbox):
    route = email_route(world)
    world.app.sync()
    world.advance(1)
    mailbox.write_text(json.dumps([message('allowed@example.com', world.now),
                                  message('unknown@example.com', world.now, sender='unknown@example.com')]))
    report = world.app.sync()
    assert len(report['created']) == 1
    req = report['created'][0]
    row = world.db.execute('SELECT * FROM messages WHERE msg_id=?', ('allowed@example.com',)).fetchone()
    assert row['text'] == 'Subject: Review this change\n\nCheck the invoice.'
    assert world.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
    assert world.app.sync()['created'] == []
    claim = world.app.claim([req], route['thread'])
    world.app.deliver(req, claim, text='The change is ready.')
    out = world.db.execute('SELECT * FROM outbox WHERE request_id=?', (req,)).fetchone()
    assert out['kind'] == 'draft'
    assert out['to_chat'] == world.cfg['settings']['owner_whatsapp']
    assert world.sends[-1][0] == 'wacli'


def test_email_recipient_headers_and_html_fallback(world, mailbox):
    route = email_route(world)
    allowed = message('cc@example.com', world.now, headers={'Cc': 'requests@example.com'},
                      body=[{'content-type': 'multipart/alternative', 'content': [
                          {'content-type': 'text/html', 'content': '<p>Review <b>this</b>.</p>'}]}])
    allowed['headers']['To'] = 'someone@example.com'
    delivered = message('delivery@example.com', world.now, headers={'Delivered-To': 'requests@example.com'})
    delivered['headers']['To'] = 'someone@example.com'
    wrong = message('wrong@example.com', world.now)
    wrong['headers']['To'] = 'someone@example.com'
    archive = mailbox.parent / 'delivery.eml'
    archive.write_text('From: alice@example.com\nTo: someone@example.com\n'
                       'Delivered-To: requests@example.com\n\nCheck the invoice.')
    raw_delivery = message('raw@example.com', world.now)
    raw_delivery['headers']['To'] = 'someone@example.com'
    raw_delivery['filename'] = [str(archive)]
    mailbox.write_text(json.dumps([allowed, delivered, raw_delivery, wrong]))
    rows = sources.email(world.paths, 'requests@example.com', world.now - 1, [], route, world.cfg)
    assert [m['msg_id'] for m in rows] == ['cc@example.com', 'delivery@example.com', 'raw@example.com']
    assert rows[0]['text'] == 'Subject: Review this change\n\nReview this.'


def test_email_act_mode_is_rejected(world):
    text = world.config_text + '''
[[routes]]
name = "mailbox"
source = "email"
chats = ["requests@example.com"]
thread = "''' + next(iter(world.cfg['threads'])) + '''"
mode = "act"
requesters = ["''' + next(iter(world.cfg['people'])) + '''"]
active_from = "2026-10-17T00:00:00Z"
'''
    world.config_path.write_text(text)
    with pytest.raises(ValidationError, match='email is read-only'):
        load(world.config_path)


def test_incompatible_t3_schema_disables_features_but_wakes(world):
    world.seed()
    world.request()
    import sqlite3
    with sqlite3.connect(world.paths.t3) as db:
        db.execute('DROP TABLE orchestration_v2_projection_runs')
    assert t3.probe(world.paths) is False
    assert t3.owner_said(world.paths, 'thread', 0) is None
    assert t3.rate_limited(world.paths, 'thread') is None
    assert t3.thread_usage(world.paths, 'thread') is None
    assert t3.hook_file_thread(world.paths, world.hook) is None
    world.advance(1800)
    assert world.app.sync()['wakes']


def test_delayed_email_uses_lookback_seed_and_active_from(world, mailbox):
    route = email_route(world)
    seed = world.now
    world.seed()
    world.advance(7200)
    world.app.sync()
    mailbox.write_text(json.dumps([
        message('delayed@example.com', seed + 60),
        message('seed-history@example.com', seed - 60),
        message('before-active@example.com', seed + 30),
    ]))
    route['active_ts'] = seed + 45
    assert world.app.sync(lookback=1)['created'] == []
    report = world.app.sync(lookback=48)
    assert len(report['created']) == 1
    assert world.db.execute('SELECT msg_id FROM messages WHERE id=(SELECT message_id FROM requests)').fetchone()[0] == 'delayed@example.com'
    assert world.app.sync()['created'] == []


def test_email_to_two_route_addresses_is_one_request_even_after_address_order_changes(world, mailbox, monkeypatch):
    route = email_route(world)
    route['chats'].append('support@example.com')
    world.seed()
    mail = message('both@example.com', world.now, headers={'Cc': 'support@example.com'})
    mailbox.write_text(json.dumps([mail]))
    calls, read = [], sources.email

    def capture(*args, **kwargs):
        calls.append(args[1])
        return read(*args, **kwargs)

    monkeypatch.setattr(sources, 'email', capture)
    assert len(world.app.sync()['created']) == 1
    assert len(calls) == 1
    route['chats'].reverse()
    assert world.app.sync()['created'] == []
    assert world.app.import_pending(route['name'], world.now - 60) == []
    assert world.db.execute('SELECT count(*) FROM requests').fetchone()[0] == 1


@pytest.mark.parametrize('source', ['email', 'teams'])
def test_non_whatsapp_from_owner_is_untrusted_in_inbox(world, mailbox, source):
    from argparse import Namespace
    from toucan.cli import dispatch
    from conftest import OWNER

    route = (email_route(world) if source == 'email' else
             next(r for r in world.cfg['routes'] if r['source'] == 'teams'))
    world.cfg['people']['alice']['whatsapp'] = [OWNER]
    world.seed()
    if source == 'email':
        mailbox.write_text(json.dumps([message('owner@example.com', world.now)]))
    else:
        world.team('owner-message')
    world.app.sync()
    result = dispatch(Namespace(command='inbox', thread=route['thread'], json=False), world.app)
    assert '----- UNTRUSTED CONTENT' in result
    assert 'OWNER REQUEST' not in result
