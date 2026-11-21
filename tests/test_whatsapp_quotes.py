import json
import sqlite3
import subprocess

import pytest

from toucan.cli import main
from toucan.config import ValidationError, load
from toucan.core import App
from toucan.storage import connect
from conftest import LID, OTHER, PHONE

DAY = 86400


def configure(world, extra=''):
    world.config_text = world.config_text.replace('requesters = ["alice"]\nactive_from',
                                                  f'requesters = ["alice"]\n{extra}active_from', 1)
    world.config_path.write_text(world.config_text)
    world.cfg = load(world.config_path)
    world.app = App(world.cfg, world.db, world.fx, world.paths)


def message(world, req):
    return dict(world.db.execute('SELECT m.* FROM messages m JOIN requests r ON r.message_id=m.id WHERE r.id=?', (req,)).fetchone())


def media_columns(world):
    """The columns a real wacli store has for rebuilding a quoted photo; the test store leaves them out."""
    with sqlite3.connect(world.wa_path) as db:
        for column in ('mime_type', 'direct_path', 'media_key', 'file_sha256', 'file_enc_sha256', 'file_length', 'buttons'):
            db.execute(f'ALTER TABLE messages ADD COLUMN {column}')


def test_a_reply_shows_the_quoted_message_even_when_it_is_far_older_than_the_inbox(world, monkeypatch, capsys):
    world.seed()
    world.wa('old', ts=world.now - 30 * DAY, text='Order 15 is a blue mug')
    world.wa('reply', text='Is this one ready?', quoted_msg_id='old')
    req = world.app.sync()['created'][0]
    m = message(world, req)
    assert m['own_text'] == 'Is this one ready?'
    assert m['text'] == 'Is this one ready?\n\nQuote (from Alice Example):\n> Order 15 is a blue mug'
    assert json.loads(m['quoted_ids']) == ['old']
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    assert main(['inbox', '--thread', 'project-a'], effects=world.fx, paths=world.paths) == 0
    out = capsys.readouterr().out
    assert '| Is this one ready?\n| \n| Quote (from Alice Example):\n| > Order 15 is a blue mug\n' in out
    assert 'blue mug' not in out.split('Quote (from')[0]  # the old message itself is outside the context window


def test_a_quoted_photo_shows_its_caption_and_file_and_our_own_messages_are_marked(world):
    world.seed()
    world.wa('photo', ts=world.now - 3 * DAY, text='', display_text='[image]', media_type='image',
             media_caption='The blue one', filename='mug.jpg', local_path='/media/mug.jpg')
    world.wa('ours', ts=world.now - 2 * DAY, sender=None, from_me=1, text='We have two left')
    world.wa('bare', ts=world.now - DAY, text='', display_text='[image]', media_type='image')
    world.wa('r2', text='this one', quoted_msg_id='photo')
    world.wa('r3', text='ok, both', quoted_msg_id='ours')
    world.wa('r4', text='and this', quoted_msg_id='bare')
    world.app.sync()
    text = {r['msg_id']: r['text'] for r in world.db.execute('SELECT msg_id,text FROM messages')}
    assert text['r2'] == 'this one\n\nQuote (from Alice Example):\n> The blue one\n> [image mug.jpg] /media/mug.jpg'
    assert text['r3'] == 'ok, both\n\nQuote (sent from this number):\n> We have two left'
    assert text['r4'] == 'and this\n\nQuote (from Alice Example):\n> [image] not downloaded'


def test_a_quote_missing_from_the_store_is_named_and_filling_it_in_later_is_not_an_edit(world):
    world.seed()
    world.wa('reply', text='Same as before please', quoted_msg_id='gone')
    req = world.app.sync()['created'][0]
    assert message(world, req)['text'] == ('Same as before please\n\n'
                                           'Quote: an earlier message (gone) that is not in the local WhatsApp store.')
    world.wa('gone', ts=world.now - 5 * DAY, text='Two boxes of tea')
    world.advance(60)
    world.app.sync()
    assert message(world, req)['text'].endswith('Quote (from Alice Example):\n> Two boxes of tea')
    assert world.app.get(req)['source_changed'] == 0 and world.app.get(req)['change_pending'] == 0


def test_a_deleted_quote_and_a_long_quote(world):
    world.seed()
    world.wa('del', ts=world.now - DAY, text='secret', revoked=1)
    world.wa('long', ts=world.now - DAY, text='x' * 2000)
    world.wa('r1', text='?', quoted_msg_id='del')
    world.wa('r2', text='!', quoted_msg_id='long')
    world.app.sync()
    text = {r['msg_id']: r['text'] for r in world.db.execute('SELECT msg_id,text FROM messages')}
    assert text['r1'] == '?\n\nQuote (from Alice Example):\n> (deleted)'
    assert text['r2'] == '!\n\nQuote (from Alice Example):\n> ' + 'x' * 1500 + '…'


def test_a_voice_note_that_quotes_keeps_the_quote_below_its_transcript(world):
    world.config_text = world.config_text.replace('[settings]\n', '[settings]\ntranscribe = "transcribe-test"\n', 1)
    configure(world)
    world.seed()
    world.wa('old', ts=world.now - DAY, text='Order 15')
    world.wa('vn', text='', display_text='[Audio]', media_type='audio', quoted_msg_id='old')
    req = world.app.sync()['created'][0]
    m = message(world, req)
    assert m['own_text'] == 'Voice note, transcribed automatically:\nPlease check the invoice'
    assert m['text'] == m['own_text'] + '\n\nQuote (from Alice Example):\n> Order 15'


def test_replies_quote_the_request_only_when_the_route_asks(world):
    world.seed()
    req = world.request('plain')
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Done')
    assert '--reply-to' not in world.sends[-1]
    configure(world, 'quote_replies = true\n')
    req = world.request('quoted')
    claim = world.app.claim([req], 'project-a')
    world.app.deliver(req, claim, text='On it', promise='the fix', due=world.now + 3600)
    world.app.deliver(req, claim, text='Fixed')
    assert world.sends[-2:] == [['wacli', '--json', 'send', 'text', '--to', PHONE, '--message', 'On it', '--reply-to', 'quoted'],
                                ['wacli', '--json', 'send', 'text', '--to', PHONE, '--message', 'Fixed', '--reply-to', 'quoted']]
    assert [r['quote_id'] for r in world.db.execute('SELECT quote_id FROM outbox ORDER BY id')] == [None, 'quoted', 'quoted']


def test_a_request_from_the_lid_chat_is_quoted_in_the_reply_to_the_phone_chat(world):
    configure(world, 'quote_replies = true\n')
    world.seed()
    world.wa('lid-msg', chat_jid=LID, sender=LID)
    req = world.app.sync()['created'][0]
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Yes')
    assert world.sends[-1][-6:] == ['--to', PHONE, '--message', 'Yes', '--reply-to', 'lid-msg']


def test_a_reply_that_goes_to_another_chat_or_cannot_be_quoted_is_sent_unquoted(world):
    group = '120363000000000001@g.us'
    world.config_text += f'''
[[routes]]
name = "client-group"
source = "whatsapp"
chats = ["{group}"]
thread = "project-a"
mode = "act"
reply_to = "other"
quote_replies = true
requesters = ["alice"]
active_from = "2026-10-17T00:00:00+05:00"
'''
    configure(world, 'quote_replies = true\n')
    media_columns(world)
    world.seed()
    world.wa('in-group', chat_jid=group)
    world.wa('photo', text='', display_text='[image]', media_type='image', local_path='/media/a.jpg')  # no media keys
    world.wa('full', text='', display_text='[image]', media_type='image', mime_type='image/jpeg', direct_path='/v/t62',
             media_key=b'k', file_sha256=b's', file_enc_sha256=b'e', file_length=10)
    world.wa('buttons', text='Pick one', buttons='[{"id":"a"}]')
    created = world.app.sync()['created']
    for req in created:
        world.app.deliver(req, world.app.claim([req], 'project-a'), text='Thanks')
    sent = {message(world, req)['msg_id']: argv for req, argv in zip(created, world.sends)}
    assert sent['in-group'][4:6] == ['--to', OTHER] and '--reply-to' not in sent['in-group']
    assert '--reply-to' not in sent['photo'] and '--reply-to' not in sent['buttons']
    assert sent['full'][-2:] == ['--reply-to', 'full']


def test_drafts_and_escalations_never_quote(world):
    world.config_text = world.config_text.replace('mode = "act"\nrequesters = ["alice"]', 'mode = "draft"\nrequesters = ["alice"]', 1)
    configure(world, 'quote_replies = true\n')
    world.seed()
    req = world.request('d')
    world.app.deliver(req, world.app.claim([req], 'project-a'), text='Draft answer')
    assert '--reply-to' not in world.sends[-1]


def test_when_wacli_refuses_the_quote_the_reply_goes_out_unquoted(world):
    configure(world, 'quote_replies = true\n')
    world.seed()
    req = world.request('q')
    results = [subprocess.CompletedProcess([], 1, '', 'quoted message q not found in local store for chat x; run `wacli sync` first'),
               subprocess.CompletedProcess([], 0, '{"data":{"MsgID":"sent-id"}}', '')]
    real = world.run
    world.fx.run = lambda argv: (real(argv), results.pop(0))[1] if argv[0] == 'wacli' and argv[2] == 'send' else real(argv)
    result = world.app.deliver(req, world.app.claim([req], 'project-a'), text='Here you go')
    assert result['state'] == 'sent' and world.app.get(req)['state'] == 'closed'
    assert world.sends[-2][-2:] == ['--reply-to', 'q'] and '--reply-to' not in world.sends[-1]
    assert [e['kind'] for e in world.db.execute("SELECT kind FROM events WHERE kind IN ('quote_dropped','delivered') ORDER BY id")] == \
        ['quote_dropped', 'delivered']


def test_other_send_failures_are_not_retried(world):
    configure(world, 'quote_replies = true\n')
    world.seed()
    req = world.request('q')
    world.send_result = subprocess.CompletedProcess([], 1, '', 'not connected')
    with pytest.raises(RuntimeError):
        world.app.deliver(req, world.app.claim([req], 'project-a'), text='Here you go')
    assert len([a for a in world.sends if a[2] == 'send']) == 1


@pytest.mark.parametrize('route,error', [
    ('[[routes]]\nname = "t"\nsource = "teams"\nchats = ["group:other"]\nthread = "project-a"\nmode = "act"\n'
     'quote_replies = true\nrequesters = ["alice"]\nactive_from = "2026-10-17T00:00:00+05:00"\n', 'only on WhatsApp routes'),
    ('[[routes]]\nname = "w"\nsource = "whatsapp"\nchats = ["15550000009@s.whatsapp.net"]\nthread = "project-a"\nmode = "act"\n'
     'quote_replies = "yes"\nrequesters = ["alice"]\nactive_from = "2026-10-17T00:00:00+05:00"\n', 'quote_replies must be true or false'),
])
def test_quote_replies_is_a_whatsapp_only_boolean(world, route, error):
    world.config_path.write_text(world.config_text + '\n' + route)
    with pytest.raises(ValidationError, match=error):
        load(world.config_path)


def test_add_route_takes_quote_replies(world, monkeypatch, capsys):
    monkeypatch.setenv('TOUCAN_CONFIG', str(world.config_path))
    assert main(['add', 'route', 'bob-chat', '--source', 'whatsapp', '--chat', '15550000009@s.whatsapp.net', '--thread', 'project-a',
                 '--requester', 'alice', '--mode', 'act', '--quote-replies', '--json'], effects=world.fx, paths=world.paths) == 0
    route = next(r for r in load(world.config_path)['routes'] if r['name'] == 'bob-chat')
    assert route['quote_replies'] is True


def test_an_existing_outbox_gains_the_quote_column(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE outbox (id INTEGER PRIMARY KEY, attempt_id TEXT UNIQUE, request_id INTEGER, kind TEXT, '
                   'source TEXT, to_chat TEXT, text TEXT, file TEXT, state TEXT, error TEXT, created_at REAL, '
                   'updated_at REAL, external_id TEXT)')
        db.execute("INSERT INTO outbox (id,attempt_id,kind,state) VALUES (1,'a','send','sent')")
    db = connect(path)
    try:
        assert [dict(r) for r in db.execute('SELECT id,quote_id FROM outbox')] == [dict(id=1, quote_id=None)]
    finally:
        db.close()
