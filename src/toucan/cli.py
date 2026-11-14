"""Small argparse interface. Claim IDs coordinate workers; they do not authenticate them."""
import argparse
import base64
import json
import re
import sqlite3
import sys

from .config import Refused, ValidationError, load, timestamp, warnings
from .core import App
from . import agentsetup, configedit, discovery, ledger, router, services, setup, telemetry
from .effects import Effects
from .storage import connect
from urllib.parse import unquote

# Printed with every inbox, so the rule is in front of the thread when it answers. Owner threads replied
# from memory to keep to the reply deadline, and dropped their own doubts to fit a short reply.
ACCURACY = ('Accuracy before speed. State something as fact only if you checked it in this turn: the code, the '
            'project documents, the chat history, or an official source you opened. Memory, an old note or what '
            'seems likely is not a check. Otherwise check it first, or say plainly it is not confirmed ("I think", '
            '"not confirmed yet"), and keep those words even in a short reply. If you cannot check before the '
            'reply is due, send a one-line acknowledgement with --promise instead of guessing. If the question '
            'uses a term you cannot match for certain to something in the chat history or documents, ask what '
            'they mean with --keep-open before answering. Before sending, reread the reply: every claim must be '
            'checked or marked as not confirmed.')


def media_id(url):
    """The Teams object ID behind a connector or Graph hostedContents image URL, or None."""
    url = unquote(str(url or ''))
    if found := re.search(r'/(?:attachments|objects)/([^/]+)/views/', url):
        return found.group(1)
    if found := re.search(r'/hostedContents/([^/]+)/', url):
        try:
            decoded = base64.b64decode(found.group(1) + '==', altchars=b'-_').decode('utf-8', 'replace')
        except ValueError:
            return None
        if found := re.search(r'/objects/([^/,]+)/views/', decoded):
            return found.group(1)
    return None


def thread_rules(cfg, thread):
    """The owner's standing rules for a thread: settings.thread_rules, then each owned project's rules."""
    texts = [cfg['settings']['thread_rules']]
    texts += [p.get('rules', '') for p in cfg['projects'].values() if p['thread'] == thread]
    return [t.strip() for t in texts if t.strip()]


def pause_note(app, pause):
    """What an owner thread does while work is paused (settings.pause_file)."""
    hhmm = lambda ts: app.local(ts).split()[1]
    when = (f' since {hhmm(pause["since"])}' if pause['since'] else '') + (f' until {hhmm(pause["until"])}' if pause['until'] else '')
    why = f' ({pause["reason"]})' if pause['reason'] else ''
    return (f'Work is paused{why}{when}. Keep answering the people who message you, with short replies. '
            'Start no delegates, subagents, background agents or long work until the pause ends: for anything '
            'that needs them, claim it, say briefly that you will pick it up after the pause, and keep it open '
            'with --keep-open or --promise. When this note is gone from `toucan inbox`, carry on as normal.')


def parser():
    p = argparse.ArgumentParser(prog='toucan')
    commands = p.add_subparsers(dest='command', required=True)
    for name in ('check-config', 'sync', 'inbox', 'claim', 'release', 'done', 'reply', 'react', 'send', 'close', 'escalate', 'status', 'log', 'import-pending', 'wait', 'resume', 'answer', 'rotate', 'init', 'doctor', 'upload', 'forward', 'bounce', 'digest', 'retriage', 'pause', 'unpause'):
        sub = commands.add_parser(name)
        sub.add_argument('--json', action='store_true')
        if name in ('check-config', 'doctor'):
            sub.add_argument('--show-ids', action='store_true')
        if name in ('init', 'doctor'):
            sub.add_argument('--config')
        if name == 'init':
            sub.add_argument('--force', action='store_true')
            sub.add_argument('--example', action='store_true')
            for flag in ('owner-name', 'timezone', 'owner-whatsapp', 'owner-teams-id'):
                sub.add_argument('--' + flag)
        if name == 'upload':
            sub.add_argument('file')
            sub.add_argument('--title')
        if name in ('inbox', 'claim', 'wait', 'resume', 'answer', 'send', 'rotate', 'forward', 'bounce'):
            sub.add_argument('--thread', required=True)
        elif name in ('release', 'done', 'reply', 'react', 'close', 'escalate', 'log'):
            sub.add_argument('--thread')
        if name == 'inbox':
            sub.add_argument('--rules', action='store_true', help='print the standing rules in full even if unchanged')
        if name == 'claim':
            sub.add_argument('requests', type=int, nargs='+')
        elif name == 'answer':
            sub.add_argument('card')
            sub.add_argument('option')
            sub.add_argument('--note', required=True)
        elif name == 'resume':
            sub.add_argument('request', type=int)
        elif name == 'forward':
            sub.add_argument('request', type=int)
            sub.add_argument('--to', required=True, help='thread key that should handle the request')
            sub.add_argument('--reason', default='')
            sub.add_argument('--claim', help='current claim, required when the request is claimed')
        elif name == 'bounce':
            sub.add_argument('request', type=int)
            sub.add_argument('--reason', required=True, help='why the request is not for this thread')
            sub.add_argument('--claim', help='current claim, required when the request is claimed')
        elif name in ('release', 'done', 'reply', 'react', 'close', 'escalate'):
            sub.add_argument('request', type=int)
            sub.add_argument('--claim', required=True)
        if name == 'done':
            sub.add_argument('--note', required=True)
        if name == 'escalate':
            from .cards import BAR, CATEGORIES
            sub.description = BAR + ' Toucan turns this into a short card; the owner answers "T<request> <letter>".'
            # Checked by cards.validate, not argparse, so a refusal explains the whole rule at once.
            sub.add_argument('--category', default='', help='why only the owner can decide this: ' + ', '.join(CATEGORIES))
            sub.add_argument('--want', default='', help='what the requester wants, one line under 120 characters')
            sub.add_argument('--question', default='', help='the one decision you need, under 160 characters')
            sub.add_argument('--option', action='append', default=[], help='a choice, under 80 characters; give 2 to 4')
            sub.add_argument('--pick', default='', help='letter of the option you suggest (A, B, ...)')
            sub.add_argument('--note', default='', help='full context, sent only when the owner replies "more"')
        if name in ('digest', 'retriage'):
            sub.add_argument('--dry-run', action='store_true')
        if name == 'pause':
            sub.description = ('Stop routing: sync reads nothing and wakes no one, and the daily list is not sent, '
                               'until `toucan unpause` or --until. Owner threads can still reply on requests they hold.')
            sub.add_argument('--until', help='when routing starts again by itself: 30m, 2h, 1d or an ISO time')
            sub.add_argument('--reason', default='', help='shown in `toucan status` and the log')
        if name == 'unpause':
            sub.description = 'Start routing again. The next sync catches up on messages that arrived during the pause.'
        if name == 'close':
            sub.add_argument('--reason', required=True)
        if name == 'sync':
            sub.add_argument('--dry-run', action='store_true')
            sub.add_argument('--shadow', action='store_true', help='keep state but send no wake-ups or alerts')
            sub.add_argument('--lookback-hours', type=float, default=48)
        if name in ('reply', 'send'):
            body = sub.add_mutually_exclusive_group(required=True)
            body.add_argument('--text')
            body.add_argument('--file')
            sub.add_argument('--caption', default='')
            sub.add_argument('--override', help='send even though Preflight would hold it; say why it must go now')
        if name == 'send':
            sub.add_argument('--route', required=True)
            sub.add_argument('--chat', help='chat ID, required when the route has several chats')
        if name == 'react':
            sub.add_argument('--message', help="Teams message ID (default: the request's triggering message)")
            sub.add_argument('--emoji', required=True, help='Teams reaction ID or friendly alias')
            sub.add_argument('--remove', action='store_true', help="remove the bot's own reaction")
        if name == 'reply':
            end = sub.add_mutually_exclusive_group(required=True)
            end.add_argument('--final', action='store_true', help='this reply finishes the request, which closes')
            end.add_argument('--promise', help='what this reply commits to; open until a --final reply')
            end.add_argument('--keep-open', action='store_true', help='a question or partial answer; stays open')
            sub.add_argument('--due', help='with --promise: 30m, 2h, 1d or an ISO time')
        if name == 'log':
            sub.add_argument('--request', type=int)
            sub.add_argument('--since', default='24h')
        if name == 'wait':
            sub.add_argument('--timeout', type=float, default=6600, help='seconds; stays under the 2-hour background limit')
        if name == 'rotate':
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument('--check', action='store_true')
            mode.add_argument('--reset', action='store_true')
        if name == 'import-pending':
            sub.add_argument('--route', required=True)
            sub.add_argument('--since', required=True)
    def options(sub):
        sub.add_argument('--json', action='store_true')
        sub.add_argument('--show-ids', action='store_true')
        sub.add_argument('--config')

    routing = commands.add_parser('router').add_subparsers(dest='action', required=True)
    for action in ('review', 'mark', 'stats'):
        sub = routing.add_parser(action)
        sub.add_argument('--json', action='store_true')
        if action == 'mark':
            sub.add_argument('request', type=int)
            sub.add_argument('verdict', choices=('right', 'wrong'))
            sub.add_argument('--note')
        else:
            sub.add_argument('--since', help='ISO time, or 30m, 2h, 1d ago')
        if action == 'review':
            sub.add_argument('--route')
            sub.add_argument('--undecided-only', action='store_true', help='only decisions not yet marked right or wrong')
    books = commands.add_parser('ledger').add_subparsers(dest='action', required=True)
    for action in ('show', 'replay', 'status', 'sync'):
        sub = books.add_parser(action)
        sub.add_argument('--json', action='store_true')
        if action in ('show', 'replay'):
            sub.add_argument('--project', required=True)
            sub.add_argument('--since', default='1d' if action == 'show' else '14d', help='ISO time, or 30m, 2h, 1d ago')
        if action == 'show':
            sub.add_argument('--person', help='person key')
            sub.add_argument('--topic', help='topic key such as sprint:2.1 or issue:12, or just 2.1 or 12')
    discover = commands.add_parser('discover').add_subparsers(dest='source', required=True)
    for source in ('whatsapp', 'teams', 'email', 'threads'):
        sub = discover.add_parser(source)
        options(sub)
        sub.add_argument('--limit', type=int, default=50)
    add = commands.add_parser('add').add_subparsers(dest='kind', required=True)
    remove = commands.add_parser('remove').add_subparsers(dest='kind', required=True)
    for kind in ('person', 'thread', 'route', 'project'):
        sub = add.add_parser(kind)
        options(sub)
        sub.add_argument('key')
        sub.add_argument('--replace', action='store_true')
        rem = remove.add_parser(kind)
        options(rem)
        rem.add_argument('key')
        if kind == 'person':
            sub.add_argument('--name', required=True)
            for flag in ('role', 'teams-id'):
                sub.add_argument('--' + flag)
            for flag in ('whatsapp', 'email'):
                sub.add_argument('--' + flag, action='append')
        elif kind == 'project':
            sub.add_argument('--name', required=True)
            sub.add_argument('--alias', dest='aliases', action='append', help='another name people use; repeat for more')
            sub.add_argument('--description', help='one line the router reads')
            sub.add_argument('--thread', required=True)
        elif kind == 'thread':
            for flag in ('project', 'fallback', 't3-thread'):
                sub.add_argument('--' + flag)
            sub.add_argument('--rotate-tokens', type=int)
            sub.add_argument('--no-waiter', dest='waiter', action='store_false', default=None,
                             help='short-turn thread that hands work to delegates: inbox stops asking for `toucan wait`')
        else:
            sub.add_argument('--source', required=True, choices=('whatsapp', 'teams', 'email'))
            for flag in ('chat', 'requester'):
                sub.add_argument('--' + flag, required=True, action='append')
            sub.add_argument('--thread', required=True)
            sub.add_argument('--mode', required=True, choices=('draft', 'act'))
            sub.add_argument('--router', help='off (default), shadow (log which thread the router would pick) or live (route)')
            for flag in ('label', 'reply-to', 'reply-prefix', 'active-from'):
                sub.add_argument('--' + flag)
            for flag in ('post-as-bot', 'owner-reply-closes', 'reply-via-whatsapp'):
                sub.add_argument('--' + flag, action='store_true', default=None)
    edit = commands.add_parser('config').add_subparsers(dest='action', required=True)
    for action in ('get', 'set', 'unset'):
        sub = edit.add_parser(action)
        options(sub)
        sub.add_argument('key', nargs='?' if action == 'get' else None)
        if action == 'set':
            sub.add_argument('value', nargs='?')
            sub.add_argument('--value', dest='values', action='append')
    sub = edit.add_parser('escalation')
    options(sub)
    sub.add_argument('project')
    sub.add_argument('--to')
    sub.add_argument('--from', dest='start')
    sub.add_argument('--wait-minutes', type=int)
    sub.add_argument('--clear', action='store_true')
    hook = commands.add_parser('hook').add_subparsers(dest='action', required=True)
    for action in ('set', 'check'):
        sub = hook.add_parser(action)
        options(sub)
        sub.add_argument('thread')
    sub = commands.add_parser('setup-prompt')
    options(sub)
    sub.add_argument('thread')
    sub.add_argument('--standby', action='store_true')
    for command in ('enable', 'disable'):
        sub = commands.add_parser(command)
        options(sub)
        if command == 'enable':
            sub.add_argument('--dry-run', action='store_true')
    sub = commands.add_parser('guide')
    options(sub)
    sub.add_argument('topic', nargs='?')
    sub.add_argument('--list', action='store_true')
    return p


def moment(value, now, sign):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([mhd])', value)
    return now + sign * float(match[1]) * {'m': 60, 'h': 3600, 'd': 86400}[match[2]] if match else timestamp(value)


def dispatch(args, app):
    c = args.command
    if c == 'forward':
        return app.forward(args.request, args.to, args.thread, args.reason, args.claim)
    if c == 'bounce':
        if not args.reason.strip():
            raise ValidationError('--reason cannot be empty')
        return app.bounce(args.request, args.thread, args.reason, args.claim)
    if c == 'router':
        if args.action == 'mark':
            return router.mark(app, args.request, args.verdict, args.note)
        since = moment(args.since, app.fx.now(), -1) if args.since else None
        if args.action == 'stats':
            return router.stats(app, since)
        return router.review(app, since, args.route, args.undecided_only)
    if c == 'ledger':
        if args.action == 'status':
            return ledger.status(app)
        if args.action == 'sync':
            return ledger.github_sync(app, force=True)
        since = moment(args.since, app.fx.now(), -1)
        if args.action == 'replay':
            return ledger.replay(app, args.project, since)
        items = ledger.show(app, args.project, since, args.person, args.topic)
        if args.json:
            return items
        lines = [f'{i["at"]}  {i["platform"]:<8} {"→" if i["direction"] == "out" else "←"} '
                 f'{", ".join(app.cfg["people"][p]["name"] for p in i["people"]) or "-"}'
                 f'{"  [" + ", ".join(i["topics"]) + "]" if i["topics"] else ""}\n    '
                 + ' '.join((i['text'] or '').split())[:240] for i in items]
        return '\n'.join(lines) or 'Nothing in that window.'
    if c == 'sync':
        if not 0 < args.lookback_hours < float('inf'):
            raise ValidationError('lookback-hours must be positive')
        return app.sync(args.lookback_hours)
    if c == 'wait':
        if not 0 < args.timeout < float('inf'):
            raise ValidationError('timeout must be positive')
        found = app.wait(args.thread, args.timeout)
        if found and found.get('replaced'):
            stop = (f'This thread no longer owns {args.thread} (replaced by {found["replaced"]}). '
                    f'Do not start `toucan wait --thread {args.thread}` again; stop now.')
            if args.json:
                return dict(**found, next_steps=stop)
            raise Refused(stop)
        rearm = f'Then start `toucan wait --thread {args.thread}` again as a background command.'
        if args.json:
            return dict(fired=bool(found), **(found or {}), next_steps=rearm)
        if not found:
            return f'No new requests for {args.thread}. {rearm}'
        ids = ' '.join(map(str, found['requests']))
        return (f'{args.thread}: requests {ids} need you (latest {found["latest"]}). Pause what you are doing: run '
                f'`toucan inbox --thread {args.thread}`, claim them and acknowledge within two minutes; send the answer once you have checked it. {rearm}')
    if c == 'rotate':
        result = app.rotate(args.thread, args.check, args.reset)
        return result if args.json or args.check or args.reset else result['instructions']
    if c == 'inbox':
        messages = app.inbox(args.thread)
        commands = (f'toucan claim REQ... --thread {args.thread}; '
                    'toucan reply REQ --claim ID (--final | --promise WHAT --due 2h | --keep-open) --text TEXT; '
                    'toucan done REQ --claim ID --note TEXT; toucan close REQ --claim ID --reason TEXT; '
                    f'only what {app.settings["owner_name"]} alone can decide: toucan escalate REQ --claim ID --category C --want W --question Q '
                    '--option A --option B --pick A --note DETAIL (see toucan escalate --help). '
                    'Routine waiting is yours to track: record the dependency and wait without reminders. '
                    'Do not ask the owner whether to keep a blocked request open')
        if any(router.is_live(r) for r in app.cfg['routes']):
            commands += f'; not yours: toucan bounce REQ --thread {args.thread} [--claim ID] --reason TEXT'
        waiting = app.waiter_running(args.thread)
        rules = thread_rules(app.cfg, args.thread)
        try:
            sent = ledger.recent(app, args.thread)
        except Exception:
            sent = []
        pause = app.pause()
        if args.json:
            return dict(next_steps=commands, accuracy=ACCURACY, pause=pause and pause_note(app, pause), rules=rules,
                        waiter_running=waiting, recently_sent=sent, untrusted_messages=messages)
        blocks = [commands, ACCURACY]
        if pause:
            blocks.append(pause_note(app, pause))
        if sent:
            blocks.append('Sent recently (`toucan ledger show` has the full timeline). Combine updates to the same person, '
                          'and check this before sending more:\n' + '\n'.join('- ' + line for line in sent))
        if rules and (args.rules or app.rules_due(args.thread, rules)):
            blocks.append('Standing rules for this thread, from the owner\'s config:\n\n' + '\n\n'.join(rules))
        elif rules:
            blocks.append(f'Standing rules unchanged since you last saw them; `toucan inbox --thread {args.thread} --rules` prints them again.')
        # A thread whose turns stay short (a desk that hands work to delegates) sets waiter = false.
        if not waiting and app.cfg['threads'][args.thread].get('waiter', True):
            blocks.append(f'No `toucan wait --thread {args.thread}` is running. Start it now as a background command '
                          '(it exits when a new request arrives, even mid-task), and start it again each time it exits.')
        owner = app.settings['owner_whatsapp']
        owners = {key for key, person in app.cfg['people'].items() if owner and owner in person.get('whatsapp', [])}
        owner_jids = {owner} if owner else set()
        for key in owners:
            owner_jids.update(app.cfg['people'][key].get('whatsapp', []))
        retriage = {int(r['key'].split(':', 1)[1]) for r in app.rows("SELECT key FROM meta WHERE key LIKE 'retriage:%'")}
        for m in messages:
            req = (f'answer to request {m["answers_request"]}' if m['answers_request'] else
                   f'request {m["request_id"]}, state {m["state"]}' if m['request_id'] else
                   f'request {m["other_request"]} belongs to thread {m["other_thread"]}; context only here' if m['other_request'] else
                   'context only')
            if m['request_id'] in retriage and m['state'] in ('new', 'claimed', 'done'):
                req += (f', was escalated to {app.settings["owner_name"]} before decision cards and is back for a re-check: '
                        'settle it yourself, ask the requester, or close it if it is stale; escalate again only if it still '
                        'needs a decision only the owner can make, with `toucan escalate` (see --help)')
            if m['request_id'] and m['part']:
                req += (f', only the part of this message about {m["part_label"]} (shown first below); '
                        f'replies start with "{m["part_label"]}:" automatically')
                if m['other_request']:
                    req += f'; thread {m["other_thread"]} has another part'
            if m['request_id'] and m['route_reason']:
                req += f', routed here because {json.dumps(m["route_reason"], ensure_ascii=False)}'
            route = next((r for r in app.cfg['routes'] if r['name'] == m['route']), {})
            if m['request_id'] and route.get('reply_to'):
                req += f', replies go to {app.cfg["people"][route["reply_to"]]["name"]}\'s WhatsApp, not this chat'
            tags = json.loads(m['mentions_json'] or '[]')
            others = [t['name'] for t in tags if t['who'] == 'person']
            if m['request_id'] and others and all(t['who'] == 'person' for t in tags):
                req += (f', tags {", ".join(json.dumps(n) for n in others)} and not Toucan: it is for them, so close it '
                        'without replying unless the text also asks Toucan, even if you got the same task elsewhere')
            if m['request_id'] and m['promise'] and m['state'] in ('new', 'claimed', 'done'):
                req += f', promised {json.dumps(m["promise"])} by {app.local(m["promise_due"])}'
            kind = ('OWNER ANSWER' if m['answers_request'] else 'OWNER REQUEST'
                    if m['source'] == 'whatsapp' and (m['sender_raw'] in owner_jids or m['sender_key'] in owners)
                    else 'UNTRUSTED CONTENT')
            label = m['sender_name'] or m['sender_raw'] or 'toucan'
            flags = ' '.join(k for k in ('edited', 'deleted') if m[k])
            # Prefix every untrusted line so message text cannot forge a delimiter.
            content = m['text'] + '\nAttachments: ' + m['attachments_json']
            if m['request_id'] and m['part']:
                content = f'This request is only this part of the message: {m["part"]}\n\nThe whole message:\n{content}'
            if m['source'] == 'teams':
                attachments = json.loads(m['attachments_json'])
                # Live push and Graph catch-up can both store the same image; a saved copy covers the other.
                saved = {media_id(a.get('contentUrl')) for a in attachments if a.get('local_path')} - {None}
                for attachment in attachments:
                    if (not attachment.get('local_path') and attachment.get('download_status')
                            and media_id(attachment.get('contentUrl')) in saved):
                        continue
                    if attachment.get('local_path'):
                        content += '\nScreenshot: ' + attachment['local_path']
                    elif attachment.get('contentUrl') and attachment.get('access_note'):
                        content += ('\nFile: ' + (attachment.get('name') or 'unnamed file') + ' ' + attachment['contentUrl'] +
                                    "\nThe bot can't download files. The owner's own Microsoft login is needed.")
                    elif attachment.get('download_status'):
                        content += ('\nScreenshot: ' + attachment['download_status'] + ' ' + str(attachment.get('http_status', '')) +
                                    "\nThe bot didn't save this image. Fetch it with the owner's own Microsoft login.")
                own = m['own_text'] if m['own_text'] is not None else m['text']
                if not (own or '').strip() and not attachments and not m['deleted']:
                    content += ("\nNo text or attachments reached the bot. It may be a video or other media the bot "
                                "can't receive. Fetch it with the owner's own Microsoft login.")
            content = '\n'.join('| ' + line for line in content.splitlines())
            where = f'{m["source"]}/{m["chat"]}' + (f' message {m["msg_id"]}' if m['source'] == 'teams' else '')
            blocks.append(f'----- {kind} from {json.dumps(label)} via {where} '
                          f'at {app.local(m["ts"]) if m["ts"] else "unknown creation time"} ({req}) {flags} -----\n{content}\n----- END -----')
        return '\n\n'.join(blocks)
    if c == 'claim':
        return dict(claim_id=app.claim(args.requests, args.thread))
    if c == 'answer':
        return app.answer(args.card, args.option, args.thread, args.note)
    if c == 'resume':
        return app.resume(args.request, args.thread)
    if c == 'react':
        return app.react(args.request, args.claim, args.emoji, message=args.message, remove=args.remove, thread=args.thread)
    if c in ('release', 'done', 'close'):
        app.transition(args.request, args.claim, c, getattr(args, 'note', getattr(args, 'reason', '')), args.thread)
        return dict(request=args.request, action=c)
    if c in ('reply', 'send', 'escalate'):
        if c == 'escalate':
            return app.escalate(args.request, args.claim, args.category, args.want, args.question, args.option, args.pick,
                                args.note, thread=args.thread)
        if args.caption and not args.file:
            raise ValidationError('--caption requires --file')
        if args.text is not None and not args.text.strip():
            raise ValidationError(f'{c} text cannot be empty')
        if c == 'send':
            return app.send(args.route, args.thread, text=args.text, file=args.file, caption=args.caption, chat=args.chat,
                            override=args.override)
        if (args.promise is None) != (args.due is None) or (args.promise is not None and not args.promise.strip()):
            raise ValidationError('--promise needs text and --due; --due only goes with --promise')
        due = args.due and moment(args.due, app.fx.now(), 1)
        if due is not None and due <= app.fx.now():
            raise ValidationError('--due must be in the future')
        return app.deliver(args.request, args.claim, text=args.text, file=args.file, caption=args.caption,
                           keep_open=args.keep_open, thread=args.thread, promise=args.promise, due=due, override=args.override)
    if c == 'digest':
        return app.digest()
    if c == 'pause':
        return app.pause_routing(args.until and moment(args.until, app.fx.now(), 1), args.reason)
    if c == 'unpause':
        return app.unpause_routing()
    if c == 'retriage':
        if app.dry:
            return dict(requests=[r['id'] for r in app.rows("SELECT r.id FROM requests r WHERE r.state='escalated' "
                                                            'AND NOT EXISTS (SELECT 1 FROM cards c WHERE c.request_id=r.id)')])
        return app.retriage()
    if c == 'import-pending':
        return dict(created=app.import_pending(args.route, timestamp(args.since)))
    if c == 'status':
        return dict(paused=app.routing_pause(app.fx.now()),
                    router=router.health(app.db, app.cfg, app.fx.now()) if router.routes(app.cfg) else None,
                    requests=app.rows("SELECT thread,state,count(*) AS count FROM requests WHERE state IN ('new','claimed','done','escalated') "
                                      'AND routing IS NULL GROUP BY thread,state'),
                    held=router.held_requests(app.db),
                    outbox=app.rows("SELECT * FROM outbox WHERE state!='sent' OR (request_id IS NULL AND created_at>=?)",
                                    (app.fx.now() - 86400,)), alerts=app.rows('SELECT * FROM alerts'),
                    promises=app.rows("SELECT id,thread,promise,promise_due FROM requests "
                                      "WHERE state IN ('new','held','claimed','done') AND promise IS NOT NULL"),
                    last_sync=app.rows("SELECT value FROM meta WHERE key='last_sync'"),
                    threads={key: app.thread_status(key) for key in app.cfg['threads']},
                    waiters={key: app.waiter_running(key) for key in app.cfg['threads']})
    if c == 'log':
        since = moment(args.since, app.fx.now(), -1)
        clauses, params = ['ts>=?'], [since]
        for column, value in [('thread', args.thread), ('request_id', args.request)]:
            if value is not None:
                clauses.append(column + '=?')
                params.append(value)
        return app.rows('SELECT * FROM events WHERE ' + ' AND '.join(clauses) + ' ORDER BY id', params)


def main(argv=None, *, effects=None, paths=None):
    args = parser().parse_args(argv)
    db = None
    try:
        if args.command == 'init':
            result = setup.init(args.config, args.force, example=args.example, owner_name=args.owner_name,
                                timezone=args.timezone, owner_whatsapp=args.owner_whatsapp, owner_teams_id=args.owner_teams_id)
            print(json.dumps(result) if args.json else result['message'])
            return 0
        if args.command == 'doctor':
            items = setup.doctor(args.config, effects, paths, args.show_ids)
            print(json.dumps(items) if args.json else setup.doctor_text(items))
            return int(any(i['status'] in ('todo', 'warn') for i in items))
        if args.command in ('add', 'remove', 'config', 'hook', 'discover', 'setup-prompt', 'enable', 'disable', 'guide'):
            result = agent_command(args, effects or Effects(), paths)
            print(result if isinstance(result, str) and not args.json else json.dumps(result, indent=None if args.json else 2, ensure_ascii=False))
            if isinstance(result, dict) and result.get('status') == 'refused':
                return 3
            return 1 if args.command == 'hook' and result.get('status') == 'invalid' else 0
        cfg = load()
        if args.command == 'upload':
            result = setup.upload(cfg['settings'], args.file, args.title)
            print(json.dumps(result) if args.json else result['url'])
            return 0
        if args.command == 'check-config':
            result = dict(people=list(cfg['people']), threads=list(cfg['threads']), projects=list(cfg['projects']),
                          routes=[{k: r[k] for k in ('name', 'source', 'chats', 'thread', 'mode', 'requesters', 'router')} for r in cfg['routes']],
                          warnings=warnings(cfg, paths))
            result['todos'] = [i for i in setup.state_checks(cfg) if i['status'] == 'todo']
            if not args.show_ids:
                for route in result['routes']:
                    route['chats'] = [setup.redact(chat) for chat in route['chats']]
        else:
            dry = getattr(args, 'dry_run', False)
            db = connect(dry=dry)
            result = dispatch(args, App(cfg, db, effects, paths, dry, getattr(args, 'shadow', False)))
        print(result if isinstance(result, str) and not args.json else json.dumps(result, indent=None if args.json else 2, ensure_ascii=False))
        if db is not None and not getattr(args, 'dry_run', False):
            sys.stdout.flush()
            telemetry.report(cfg, db, effects or Effects())
        if (args.command == 'rotate' and args.check and not result['done'] or
                args.command == 'wait' and isinstance(result, dict) and result.get('replaced')):
            return 3
        return 1 if args.command == 'sync' and result['problems'] else 0
    except Refused as error:
        print(str(error), file=sys.stderr)
        return 3
    except ValidationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, RuntimeError) as error:
        # Exceptions may contain provider URLs; only deliberate application errors are printable.
        print(str(error) if isinstance(error, RuntimeError) else f'{type(error).__name__}: operation failed', file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()


def agent_command(args, effects, paths):
    c = args.command
    path = setup.config_path(args.config)
    if c == 'guide':
        return agentsetup.guide(args.topic, args.list)
    if c == 'config':
        if args.action == 'escalation':
            return configedit.escalation(path, args.project, args.to, args.start, args.wait_minutes, args.clear)
        values = None
        if args.action == 'set':
            if args.value is not None and args.values:
                raise ValidationError('use VALUE or repeated --value, not both')
            values = args.values if args.values else [args.value] if args.value is not None else None
        result = configedit.edit(path, args.action, args.key, values, bool(getattr(args, 'values', None)))
        if args.action == 'get':
            result['value'] = agentsetup.safe(result['value'], args.json or args.show_ids, (args.key or '').split('.')[-1])
        return result
    if c == 'remove':
        return configedit.remove(path, args.kind, args.key)
    if c == 'add':
        if args.kind == 'person':
            names = ('name', 'role', 'whatsapp', 'teams_id', 'email')
        elif args.kind == 'thread':
            names = ('project', 'fallback', 't3_thread', 'rotate_tokens', 'waiter')
        elif args.kind == 'project':
            names = ('name', 'aliases', 'description', 'thread')
        else:
            names = ('source', 'thread', 'mode', 'router', 'label', 'post_as_bot', 'owner_reply_closes', 'reply_to', 'reply_prefix', 'reply_via_whatsapp', 'active_from')
        entry = {key: getattr(args, key) for key in names if getattr(args, key) is not None}
        if args.kind == 'thread':
            entry['hook_file'] = str(agentsetup.hooks_dir() / (configedit.key_name(args.key) + '.hook'))
        if args.kind == 'route':
            entry.update(chats=args.chat, requesters=args.requester)
        return configedit.add(path, args.kind, args.key, entry, args.replace, effects.now())
    if c == 'enable':
        return services.enable(path, effects, paths, args.dry_run)
    if c == 'disable':
        return services.disable(path, effects, paths)
    cfg = load(path)
    if c == 'hook':
        return agentsetup.hook(args.action, args.thread, cfg, paths)
    if c == 'setup-prompt':
        result = agentsetup.setup_prompt(cfg, args.thread, args.standby)
        return result if args.json else json.dumps(result, indent=2)
    result = discovery.discover(args.source, cfg, effects, paths, args.limit)
    return agentsetup.safe(result, args.json or args.show_ids)
