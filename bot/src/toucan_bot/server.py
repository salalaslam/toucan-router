"""The bot's HTTP server: the Teams messaging endpoint plus the setup link."""

from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager, suppress
import logging
import os
import re
import signal
import sqlite3
from urllib.parse import parse_qs

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from microsoft_teams.api import MessageActivity
from microsoft_teams.apps import ActivityContext, App
from microsoft_teams.apps.http import FastAPIAdapter

from . import catchup, config, media, package, pages, store

log = logging.getLogger("toucan_bot")

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PING = {"ping", "hi", "hello"}


async def verify_credentials(client_id: str, client_secret: str) -> str | None:
    """Ask Entra for a Bot Framework token. Returns None on success, else a reason to show."""
    url = f"https://login.microsoftonline.com/{config.load()['tenant_id']}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://api.botframework.com/.default",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, data=data)
    except httpx.HTTPError as e:
        return f"Couldn't reach Microsoft to check the secret ({type(e).__name__}). Try again in a minute."
    if resp.status_code == 200 and "access_token" in resp.json():
        return None
    try:
        desc = resp.json().get("error_description", "")
    except ValueError:
        desc = ""
    first_line = desc.splitlines()[0] if desc else f"HTTP {resp.status_code}"
    return f"Microsoft rejected the bot ID or secret: {first_line}"


def _tenant_of(activity) -> str | None:
    conv = activity.conversation
    if conv is not None and conv.tenant_id:
        return conv.tenant_id.lower()
    cd = activity.channel_data
    tenant = getattr(cd, "tenant", None) if cd is not None else None
    return (getattr(tenant, "id", None) or "").lower() or None


def _remember_conversation(con, activity) -> None:
    conv = activity.conversation
    store.upsert_conversation(
        con,
        id=conv.id,
        type=getattr(conv, "conversation_type", None),
        name=getattr(activity, "topic_name", None) or getattr(conv, "name", None),
        service_url=activity.service_url or "",
        tenant_id=_tenant_of(activity),
    )

    if getattr(activity, 'members_added', None) or getattr(activity, 'members_removed', None):
        store.invalidate_roster(con, conv.id)
    recipient_id = getattr(activity.recipient, 'id', None)
    for member in [activity.from_, *(getattr(activity, 'members_added', None) or [])]:
        if (member and member.id != recipient_id and getattr(member, 'role', None) != 'bot'
                and not member.id.startswith('28:')):
            store.remember_member(con, conv.id, member)
    for member in getattr(activity, 'members_removed', None) or []:
        con.execute('DELETE FROM members WHERE conversation_id=? AND id=?', (conv.id, member.id))
    con.commit()


def _store_message(con, a, *, edited=False, deleted=False, restored=False, attachments=None):
    raw = getattr(a, 'text', None) or ''
    if not raw:
        raw = next((a.content for a in getattr(a, 'attachments', None) or []
                    if a.content_type == 'text/html' and isinstance(a.content, str)), '')
    text = raw
    mentioned = False
    for entity in a.entities or []:
        if getattr(entity, 'type', None) == 'mention' and entity.mentioned.id == a.recipient.id:
            mentioned = True
            if entity.text:
                text = text.replace(entity.text, '')
    def encoded(values):
        return json.dumps([v.model_dump(mode='json', by_alias=True, exclude_none=True) for v in values or []])
    event_ts = a.timestamp.isoformat() if a.timestamp else store.now()
    # localTimestamp is the mutation instant in local time, never creation.
    created = None if edited or deleted or restored else a.timestamp
    ts = created.isoformat() if created else None
    # Missing user identity is incomplete data, not evidence of an application.
    application = a.from_.role == 'bot' or a.from_.id.startswith('28:')
    return store.record_message(
        con, conversation_id=a.conversation.id, id=a.id, ts=ts,
        sender_aad_id=None if application else a.from_.aad_object_id, sender_name=a.from_.name,
        sender_app_id=a.from_.id if application else None,
        text=store.plain_text(text), raw_html=raw, mentioned=mentioned, reply_to_id=a.reply_to_id,
        attachments_json=json.dumps(attachments) if attachments is not None else encoded(getattr(a, 'attachments', None)),
        quoted_json=encoded([e for e in a.entities or [] if getattr(e, 'type', '') != 'mention']),
        edited_at=event_ts if edited else None, deleted=deleted,
        deleted_at=event_ts if deleted else None, restored=restored,
        modified_at=event_ts if edited or deleted or restored else ts,
        content_present=not restored or bool(raw or getattr(a, 'attachments', None)),
    )


def build(creds: config.Credentials | None = None, *, port: int | None = None) -> tuple[App, FastAPI]:
    settings = config.require_setup()
    if warning := config.credential_warning(creds, settings):
        raise ValueError(warning)
    port = port or settings["port"]
    # Never wait synchronously for another writer on the HTTP event loop.
    con = store.connect(config.db_path(), timeout=0)
    token = config.setup_token()
    store.heartbeat(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('catchup_interval_seconds',?)", (str(settings['catchup_interval_seconds']),))
    con.execute("DELETE FROM meta WHERE key='task:maintenance'")
    con.commit()

    background = {}

    async def supervise(name, work, interval):
        state = background[name] = dict(last_success=None, last_error=None, last_failure=None,
                                        failures=0, interval_seconds=interval)
        delay = 1
        while True:
            try:
                await work()
                state.update(last_success=store.now(), last_error=None)
                with con:
                    con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('task:' + name, json.dumps(state)))
                delay = 1
                await asyncio.sleep(interval)
            except Exception as error:
                con.rollback()
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError from None
                state.update(last_error=type(error).__name__, last_failure=store.now(), failures=state['failures'] + 1)
                log.warning('%s failed; retrying in %ss: %s', name, delay, type(error).__name__)
                try:
                    with con:
                        con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('task:' + name, json.dumps(state)))
                except sqlite3.Error:
                    # A contending writer can also block health persistence. Keep
                    # the failure in memory and persist its count after recovery.
                    con.rollback()
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)

    async def pulse():
        store.heartbeat(con)

    @asynccontextmanager
    async def lifespan(web):
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            async def retry_images(source):
                token = (await app.token_provider.get_app_token('https://graph.microsoft.com/.default', settings['tenant_id'])
                         if source == 'graph' else await app.token_provider.get_app_token())
                if token is None:
                    raise ValueError('Image token unavailable')
                await media.retry_pending(con, client, str(token), settings, source=source)
            async def refresh_rosters():
                failed = False
                for chat in store.conversations(con):
                    if await catchup.roster(con, app, chat) is False:
                        failed = True
                if failed:
                    raise RuntimeError('Roster refresh failed; see per-chat roster errors')
            interval = settings['catchup_interval_seconds']
            tasks = [asyncio.create_task(supervise('heartbeat', pulse, 60)),
                     asyncio.create_task(supervise('expiry', lambda: media.expire(settings), interval))]
            if creds:
                tasks += [asyncio.create_task(supervise('catchup', lambda: catchup.sync(con, client, app, settings), interval)),
                          asyncio.create_task(supervise('media', lambda: retry_images('live'), interval)),
                          asyncio.create_task(supervise('graph_media', lambda: retry_images('graph'), interval)),
                          asyncio.create_task(supervise('roster', refresh_rosters, settings['roster_interval_seconds']))]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with suppress(asyncio.CancelledError):
                        await task
                con.close()

    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def server_factory(fa: FastAPI) -> uvicorn.Server:
        # A reverse proxy forwards the public endpoint to this loopback listener.
        return uvicorn.Server(uvicorn.Config(fa, host="127.0.0.1", port=port, log_level="info", proxy_headers=True,
                                             access_log=False))

    adapter = FastAPIAdapter(app=web, server_factory=server_factory)
    if creds:
        tenant = config.app_tenant(creds)
        log.info("bot tokens from %s", "botframework.com (multi-tenant)" if tenant is None else f"tenant {tenant}")
        app = App(client_id=creds.client_id, client_secret=creds.client_secret, tenant_id=tenant,
                  http_server_adapter=adapter)
    else:
        # Without credentials the SDK answers 401 to every request on /api/messages.
        app = App(http_server_adapter=adapter)

    async def images(a):
        if creds:
            try:
                token = await app.token_provider.get_app_token()
                if token is None:
                    raise ValueError('Connector token unavailable')
                async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                    await media.retry_pending(con, client, str(token), settings, source='live', chat=a.conversation.id)
            except Exception as error:
                # Token outages leave the durable work pending for maintenance.
                log.warning('image authentication failed: %s', type(error).__name__)

    def allowed(activity):
        if _tenant_of(activity) != settings["tenant_id"]:
            log.warning("ignored activity from a different tenant")
            return False
        return True

    @app.on_message
    async def on_message(ctx: ActivityContext[MessageActivity]):
        a = ctx.activity
        if not allowed(a):
            return
        _remember_conversation(con, a)
        # Persist before downloading so an interrupted download cannot lose the message.
        new = _store_message(con, a)
        await images(a)
        text = store.plain_text(a.text or '')
        if new and getattr(a.conversation, "conversation_type", None) == "personal" and text.lower() in PING:
            await ctx.send(f"{settings['app_name']} is connected.")

    @app.on_message_update
    async def on_message_update(ctx):
        a = ctx.activity
        if allowed(a):
            _remember_conversation(con, a)
            restored = getattr(a.channel_data, 'event_type', None) == 'undeleteMessage'
            _store_message(con, a, edited=not restored, restored=restored)
            await images(a)

    @app.on_message_delete
    async def on_message_delete(ctx):
        a = ctx.activity
        if allowed(a):
            _remember_conversation(con, a)
            _store_message(con, a, deleted=True)

    @app.on_install_add
    async def on_install(ctx):
        a = ctx.activity
        if allowed(a):
            _remember_conversation(con, a)

            if creds:
                await catchup.roster(con, app, store.get_conversation(con, a.conversation.id))

    @app.on_message_reaction
    async def on_message_reaction(ctx):
        a = ctx.activity
        if allowed(a) and a.reactions_added:
            log.info("message reaction in %s on %s from %s: %s", a.conversation.id, a.reply_to_id,
                     a.from_.id, [r.type for r in a.reactions_added])

    @app.on_conversation_update
    async def on_conversation_update(ctx):
        a = ctx.activity
        if allowed(a):
            _remember_conversation(con, a)
            if creds:
                await catchup.roster(con, app, store.get_conversation(con, a.conversation.id))

    def check_token(given: str) -> bool:
        return hmac.compare_digest(given.encode(), token.encode())

    def current_bot_id() -> str | None:
        c = config.load_credentials()
        return c.client_id if c else None

    @web.get("/healthz")
    async def healthz():
        return {"ok": not any(t['last_error'] for t in background.values()),
                "registered": current_bot_id() is not None, 'background': background}

    @web.get("/privacy", response_class=HTMLResponse)
    async def privacy():
        return pages.privacy_page()

    @web.get("/terms", response_class=HTMLResponse)
    async def terms():
        return pages.terms_page()

    @web.get("/setup/{given}", response_class=HTMLResponse)
    async def setup_get(given: str, ok: str = ""):
        if not check_token(given):
            return Response(status_code=404)
        notice = "Saved. Microsoft accepted the bot ID and secret, and the bot has restarted with them." if ok else ""
        return pages.setup_page(token=token, bot_id=current_bot_id(), notice=notice)

    @web.post("/setup/{given}", response_class=HTMLResponse)
    async def setup_post(given: str, request: Request):
        if not check_token(given):
            return Response(status_code=404)
        if current_bot_id() is not None:
            return HTMLResponse(pages.setup_page(token=token, bot_id=current_bot_id(),
                                                 error="The bot is already registered. Ask the administrator to reset it first."),
                                status_code=409)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        client_id = (form.get("client_id") or [""])[0].strip()
        client_secret = (form.get("client_secret") or [""])[0].strip()
        if not GUID.match(client_id) or not client_secret:
            return HTMLResponse(pages.setup_page(token=token, bot_id=None,
                                                 error="Enter the bot ID (a GUID) and the client secret value."),
                                status_code=400)
        reason = await verify_credentials(client_id, client_secret)
        if reason:
            log.warning("setup: credential check failed")
            return HTMLResponse(pages.setup_page(token=token, bot_id=None, error=reason), status_code=400)
        config.save_credentials(config.Credentials(client_id, client_secret))
        package.build(client_id, config.package_path())
        log.info("setup: credentials saved for %s; restarting", client_id)
        # systemd (Restart=always) brings the bot back with the new credentials.
        asyncio.get_running_loop().call_later(1.5, os.kill, os.getpid(), signal.SIGTERM)
        return Response(status_code=303, headers={"Location": f"/setup/{token}?ok=1"})

    @web.get("/setup/{given}/toucan-teams-app.zip")
    async def setup_package(given: str):
        if not check_token(given):
            return Response(status_code=404)
        bot_id = current_bot_id()
        if bot_id is None:
            return JSONResponse({"error": "not registered yet"}, status_code=409)
        path = config.package_path()
        package.build(bot_id, path)
        return FileResponse(path, media_type="application/zip", filename="toucan-teams-app.zip")

    return app, web


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = config.require_setup()
    creds = config.load_credentials()
    log.info("starting on 127.0.0.1:%d, %s", settings["port"], "registered" if creds else "waiting for setup")
    app, _ = build(creds, port=settings["port"])
    asyncio.run(app.start(settings["port"]))
