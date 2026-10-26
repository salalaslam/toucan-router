"""toucan-bot: run the Teams bot and post as Toucan.

Exit codes: 0 success, 1 operational failure, 2 usage or missing setup,
4 ambiguous send (may have been delivered; inspect before retrying).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from . import config, names, store


def _print(rows, as_json: bool, fmt) -> None:
    if as_json:
        print(json.dumps([dict(r) for r in rows], indent=2))
    else:
        for r in rows:
            print(fmt(r))


# The Bot Connector sometimes answers 401 to a fresh token and accepts the same send seconds later.
# A 401 means nothing was posted, so retrying can't duplicate the message.
RETRY_401_DELAYS = (3, 10, 30)
REACTION_RETRIES = 3
REACTION_ALIASES = {
    "eyes": "1f440_eyes",
    "check": "2705_whiteheavycheckmark",
    "done": "2705_whiteheavycheckmark",
    "pin": "1f4cc_pushpin",
    "thumbsup": "like",
}


def _is_401(e: Exception) -> bool:
    response = getattr(e, "response", None)
    return getattr(response, "status_code", None) == 401


async def _with_app(creds: config.Credentials, what: str, call):
    from microsoft_teams.apps import App

    for delay in (*RETRY_401_DELAYS, None):
        app = App(client_id=creds.client_id, client_secret=creds.client_secret, tenant_id=config.app_tenant(creds))
        await app.initialize()
        try:
            return await call(app)
        except Exception as e:  # noqa: BLE001 - only a 401 is retried
            if delay is None or not _is_401(e):
                raise
            print(f"toucan-bot: {what} got 401, retrying in {delay}s", file=sys.stderr)
            await asyncio.sleep(delay)


class UncertainSend(Exception):
    """The connector may have accepted the message; never retry automatically."""


async def _send(creds: config.Credentials, conversation_id: str, service_url: str, text: str) -> str | None:
    async def call(app):
        try:
            sent = await app.send(conversation_id, text, service_url=service_url)
        except Exception as error:
            # Token acquisition happens inside app.send too. Its failures precede
            # the connector POST and cannot have delivered the chat message.
            request = getattr(error, 'request', None)
            connector = request is None or str(request.url).startswith(service_url.rstrip('/') + '/')
            status = getattr(getattr(error, 'response', None), 'status_code', None)
            before_send = isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            if connector and not before_send and (
                    isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError)) or
                    status is not None and status >= 500):
                raise UncertainSend('delivery uncertain; inspect the chat before sending again') from error
            raise
        return getattr(sent, "id", None)

    return await _with_app(creds, "send", call)


async def _delete(creds: config.Credentials, conversation_id: str, service_url: str, activity_id: str) -> None:
    async def call(app):
        await app.api.conversations.activities_client.delete(conversation_id, activity_id, service_url=service_url)

    await _with_app(creds, "delete", call)


def _retry_after(response) -> float:
    value = response.headers.get("Retry-After", "1")
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 1
    return max(0, delay) if math.isfinite(delay) else 1


async def _react(creds: config.Credentials, conversation_id: str, service_url: str,
                 activity_id: str, emoji: str, remove: bool = False) -> None:
    retries = 0

    async def call(app):
        nonlocal retries
        conversations = app.api.from_service_url(service_url).conversations
        react = conversations.delete_reaction if remove else conversations.add_reaction
        while True:
            try:
                return await react(conversation_id, activity_id, emoji)
            except Exception as e:  # noqa: BLE001 - only a 429 is retried here
                response = getattr(e, "response", None)
                if getattr(response, "status_code", None) != 429 or retries >= REACTION_RETRIES:
                    raise
                retries += 1
                delay = _retry_after(response)
                print(f"toucan-bot: react got 429, retrying in {delay:g}s", file=sys.stderr)
                await asyncio.sleep(delay)

    await _with_app(creds, "react", call)


def cmd_react(args) -> int:
    creds = config.load_credentials()
    if creds is None:
        print("toucan-bot: not registered yet; finish the setup link first", file=sys.stderr)
        return 2
    if not args.message.strip() or not args.emoji.strip():
        print("toucan-bot: message ID and emoji cannot be empty", file=sys.stderr)
        return 2
    con = store.connect(config.db_path())
    chats = names.load_chats()
    conversation = args.conversation
    if args.to:
        try:
            conversation = names.resolve(args.to, [r["id"] for r in store.conversations(con)], chats)
        except names.NotFound as e:
            print(f"toucan-bot: {e}", file=sys.stderr)
            return 2
    conv = store.get_conversation(con, conversation)
    if conv is None:
        print("toucan-bot: unknown conversation; the bot must be added to it or messaged first", file=sys.stderr)
        return 2
    emoji = REACTION_ALIASES.get(args.emoji, args.emoji)
    to = names.label(chats[conv["id"]]) if conv["id"] in chats else conv["name"] or conv["id"]
    if not args.dry_run:
        try:
            asyncio.run(_react(creds, conv["id"], conv["service_url"], args.message, emoji, args.remove))
        except Exception as e:  # noqa: BLE001 - report any reaction failure as operational
            status = getattr(getattr(e, "response", None), "status_code", None)
            reason = ("HTTP 403: bot cannot access this chat or message; check that it is still in the chat" if status == 403
                      else "HTTP 404: message or conversation no longer exists, or the bot is no longer in the chat" if status == 404
                      else "HTTP 429: reaction rate limit still exceeded after retries" if status == 429
                      else f"{type(e).__name__}: {e}")
            print(f"toucan-bot: react failed: {reason}", file=sys.stderr)
            return 1
    result = {"conversation_id": conv["id"], "to": to, "activity_id": args.message,
              "emoji": emoji, "removed": args.remove, "dry_run": args.dry_run}
    action = "remove" if args.remove else "add"
    print(json.dumps(result) if args.json else
          f"{'would ' + action if args.dry_run else 'removed' if args.remove else 'added'} {emoji} "
          f"{'from' if args.remove else 'to'} message {args.message} in {to}: {conv['id']}")
    return 0


def cmd_send(args) -> int:
    creds = config.load_credentials()
    if creds is None:
        print("toucan-bot: not registered yet; finish the setup link first", file=sys.stderr)
        return 2
    con = store.connect(config.db_path())
    conversation = args.conversation
    if args.to:
        chats = names.load_chats()
        try:
            conversation = names.resolve(args.to, [r["id"] for r in store.conversations(con)], chats)
        except names.NotFound as e:
            print(f"toucan-bot: {e}", file=sys.stderr)
            return 2
    conv = store.get_conversation(con, conversation)
    if conv is None:
        print("toucan-bot: unknown conversation; the bot must be added to it or messaged first", file=sys.stderr)
        return 2
    if args.dry_run:
        chats = names.load_chats()
        print(f"would send to {names.label(chats[conv['id']]) if conv['id'] in chats else conv['name']}: {conv['id']}")
        return 0
    text = args.text if args.text is not None else sys.stdin.read()
    if not text.strip():
        print("toucan-bot: empty message", file=sys.stderr)
        return 2
    try:
        activity_id = asyncio.run(_send(creds, conv["id"], conv["service_url"], text))
    except UncertainSend as e:
        print(f"toucan-bot: {e}", file=sys.stderr)
        return 4
    except Exception as e:  # noqa: BLE001 - failures known to precede delivery
        print(f"toucan-bot: send failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    try:
        store.record_sent(con, conversation_id=conv["id"], activity_id=activity_id, text=text)
        chats = names.load_chats()
        to = names.label(chats[conv["id"]]) if conv["id"] in chats else conv["name"] or conv["id"]
        print(json.dumps({"conversation_id": conv["id"], "to": to, "activity_id": activity_id}) if args.json
              else f"sent to {to}: {activity_id or '(no activity ID)'}")
    except Exception:  # the POST succeeded; a local failure must not invite a duplicate
        print("toucan-bot: message accepted but receipt unavailable; delivery uncertain", file=sys.stderr)
        return 4
    return 0


def cmd_delete(args) -> int:
    creds = config.load_credentials()
    if creds is None:
        print("toucan-bot: not registered yet; finish the setup link first", file=sys.stderr)
        return 2
    con = store.connect(config.db_path())
    conv = store.get_conversation(con, args.conversation)
    if conv is None:
        print("toucan-bot: unknown conversation; the bot must be added to it or messaged first", file=sys.stderr)
        return 2
    try:
        asyncio.run(_delete(creds, conv["id"], conv["service_url"], args.activity))
    except Exception as e:  # noqa: BLE001 - report any delete failure as operational
        print(f"toucan-bot: delete failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("deleted")
    return 0


def cmd_conversations(args) -> int:
    con = store.connect(config.db_path())
    chats = names.load_chats()
    rows = [{**dict(r), "name": names.label(chats[r["id"]]) if r["id"] in chats else r["name"]}
            for r in store.conversations(con)]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['last_seen']}  {r['type'] or '?':9}  {r['name'] or '(no name)'}  {r['id']}")
    return 0


def cmd_messages(args) -> int:
    con = store.connect(config.db_path())
    _print(store.recent_messages(con, args.limit), args.json,
           lambda r: f"{r['ts']}  {r['sender_name']}: {r['text']}  [{r['conversation_id']}]")
    return 0


def cmd_setup_url(args) -> int:
    print(config.setup_url())
    return 0


def cmd_status(args) -> int:
    cfg = config.load()
    creds = config.load_credentials()
    def identity(value):
        return value if args.show_ids or not value else "…" + value[-4:]
    warning = config.credential_warning(creds, cfg)
    ready = bool(cfg["tenant_id"] and cfg["public_url"] and creds and not warning)
    out = {"ok": ready, "tenant_id": identity(cfg["tenant_id"]), "public_url": cfg["public_url"],
           "credentials": bool(creds and not warning), "registered": creds is not None, "warning": warning,
           "bot_id": identity(creds.client_id) if creds else None,
           "app_type": creds.app_type if creds else None,
           "endpoint": f"{cfg['public_url']}/api/messages" if cfg["public_url"] else "", "local_port": cfg["port"]}
    try:
        # Status must not create or migrate a store just to inspect it.
        with sqlite3.connect(config.db_path().resolve().as_uri() + '?mode=ro', uri=True) as con:
            con.row_factory = sqlite3.Row
            con.execute('BEGIN')  # catch-up state and delivery counts must describe one snapshot
            out.update(store.coverage(con))
    except (OSError, sqlite3.Error, ValueError):
        out.update(catchup={'error': 'bot store missing or outdated; restart the upgraded bot'}, delivery=None)
    print(json.dumps(out, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in out.items()))
    return 0 if ready else 1


def cmd_app_type(args) -> int:
    creds = config.load_credentials()
    if creds is None:
        print("toucan-bot: not registered yet; finish the setup link first", file=sys.stderr)
        return 2
    config.save_credentials(dataclasses.replace(creds, app_type=args.app_type))
    print(f"app type: {args.app_type}; restart the bot: systemctl --user restart toucan-bot")
    return 0


def cmd_reset(args) -> int:
    if not args.yes:
        print("toucan-bot: this deletes the stored bot ID and secret; rerun with --yes", file=sys.stderr)
        return 2
    print("removed" if config.clear_credentials() else "nothing to remove")
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    serve()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="toucan-bot", description=__doc__.splitlines()[0],
                                epilog=" ".join(__doc__.splitlines()[2:]))
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the bot server (systemd runs this)").set_defaults(fn=cmd_serve)
    status = sub.add_parser("status", help="configuration, registration and endpoint")
    status.add_argument("--show-ids", action="store_true", help="show full tenant and application IDs")
    status.set_defaults(fn=cmd_status)
    sub.add_parser("setup-url", help="print the private setup link").set_defaults(fn=cmd_setup_url)
    s = sub.add_parser("send", help="post a message as Toucan",
                       epilog="Exit 4: ambiguous send; may have been delivered. Inspect before retrying.")
    to = s.add_mutually_exclusive_group(required=True)
    to.add_argument("--to", help="person (their 1:1 with the signed-in user) or chat name, e.g. 'Alice' or 'Project A'")
    to.add_argument("--conversation", help="conversation ID from `toucan-bot conversations`")
    s.add_argument("--text", help="message text (default: read stdin)")
    s.add_argument("--dry-run", action="store_true", help="print which chat it would go to, and send nothing")
    s.set_defaults(fn=cmd_send)
    d = sub.add_parser("delete", help="delete a message Toucan posted")
    d.add_argument("--conversation", required=True, help="conversation ID from `toucan-bot conversations`")
    d.add_argument("--activity", required=True, help="activity ID that `send` printed")
    d.set_defaults(fn=cmd_delete)
    r = sub.add_parser("react", help="add or remove Toucan's reaction to a Teams message")
    to = r.add_mutually_exclusive_group(required=True)
    to.add_argument("--to", help="person (their 1:1 with the signed-in user) or chat name, like `send --to`")
    to.add_argument("--conversation", help="conversation ID from `toucan-bot conversations`")
    r.add_argument("--message", required=True, help="Teams message ID from the bot store")
    r.add_argument("--emoji", required=True, help="Teams reaction ID or eyes, check, done, pin, thumbsup")
    r.add_argument("--remove", action="store_true", help="remove the bot's own reaction")
    r.add_argument("--dry-run", action="store_true", help="print the chat, message and reaction without changing it")
    r.set_defaults(fn=cmd_react)
    sub.add_parser("conversations", help="chats the bot knows about").set_defaults(fn=cmd_conversations)
    m = sub.add_parser("messages", help="recent messages the bot received")
    m.add_argument("--limit", type=int, default=20)
    m.set_defaults(fn=cmd_messages)
    a = sub.add_parser("app-type", help="set how the bot is registered with the Bot Framework")
    a.add_argument("app_type", choices=config.APP_TYPES)
    a.set_defaults(fn=cmd_app_type)
    r = sub.add_parser("reset", help="delete the stored bot ID and secret")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(fn=cmd_reset)
    for command in sub.choices.values():
        command.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                             help='machine-readable output')
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (OSError, ValueError, KeyError) as error:
        reason = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "cannot read bot configuration or credentials"
        print(f"toucan-bot: {reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
