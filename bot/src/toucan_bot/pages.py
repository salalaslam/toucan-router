"""HTML for the bot setup link and organization policies."""
from html import escape

from . import config

STYLE = """
body { font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 760px;
       margin: 32px auto; padding: 0 20px; color: #1d1d1f; }
h1 { font-size: 22px; margin-bottom: 4px; }
h2 { font-size: 17px; margin: 30px 0 8px; }
.sub { color: #6e6e73; margin-top: 0; }
.status { border: 1px solid #d2d2d7; border-radius: 10px; padding: 12px 18px; background: #fafafa; }
.status div { margin: 4px 0; }
.ok { color: #1a7f37; font-weight: 600; }
.wait { color: #9a6700; font-weight: 600; }
.done { color: #6e6e73; }
.msg { border-radius: 8px; padding: 10px 14px; margin: 16px 0; }
.msg.good { background: #e8f5ec; border: 1px solid #a7d8b5; }
.msg.bad { background: #fdecec; border: 1px solid #f0b4b4; }
code { font-size: 13px; background: #f2f2f4; padding: 1px 5px; border-radius: 4px; word-break: break-all; }
ol li, ul li { margin: 5px 0; }
label { display: block; font-weight: 600; margin: 12px 0 4px; }
input[type=text], input[type=password] { width: 100%; box-sizing: border-box; padding: 8px 10px;
       font: 14px ui-monospace, monospace; border: 1px solid #c7c7cc; border-radius: 6px; }
button, .btn { margin-top: 14px; padding: 9px 18px; font-size: 15px; border: 0; border-radius: 8px;
       background: #1F6F5C; color: white; cursor: pointer; text-decoration: none; display: inline-block; }
.muted { color: #6e6e73; font-size: 13px; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        '<meta name=robots content="noindex, nofollow">'
        f"<title>{escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )


def setup_page(*, token: str, bot_id: str | None, notice: str = "", error: str = "") -> str:
    cfg = {k: escape(str(v)) for k, v in config.load().items()}
    app, org = cfg["app_name"], cfg["org_name"]
    base = f"/setup/{escape(token)}"
    flash = (f'<div class="msg good">{escape(notice)}</div>' if notice else "")
    flash += f'<div class="msg bad">{escape(error)}</div>' if error else ""
    if bot_id:
        registration = f'<p class=ok>Registered. App ID <code>{escape(bot_id)}</code>.</p>'
        steps = f"""
<h2>Approve the app in Teams</h2>
<ol>
<li><a class=btn href="{base}/toucan-teams-app.zip">Download the app package</a>.</li>
<li>In the Teams admin center, open <b>Teams apps</b>, then <b>Manage apps</b>,
select <b>Upload new app</b>, and upload the zip. Replace any earlier version of this app.</li>
<li>Allow <b>{app}</b> and make it available to the people who will use it.
If custom apps are disabled, enable them in your organization's app policies.</li>
<li>Upgrade or reinstall the app in each approved human chat and consent to
<code>ChatMessage.Read.Chat</code> there. Updating the catalog alone does not grant access.</li>
<li>An administrator should check <code>Get-MgBetaChatRscConfiguration</code> and any app exceptions.
<code>DisabledForAllApps</code> blocks chat consent. This is separate from personal-scope RSC.</li>
<li>Test an ordinary message without an @mention in each chat. HTTP 403 during catch-up means
no RSC grant for that chat or an unsupported chat type. A hosted-image 403 can occur even
when reading messages succeeds.</li>
</ol>"""
    else:
        registration = '<p class=wait>Not registered yet.</p>'
        steps = f"""
<h2>Step 1. Register the bot</h2>
<ol>
<li>Sign in to the <a href="https://portal.azure.com/#create/Microsoft.AzureBot">Azure portal</a>
with your organization's account and create an Azure Bot.</li>
<li>Choose a unique bot handle, your subscription and resource group,
<b>Global</b> residency and the <b>Free (F0)</b> pricing tier.</li>
<li>Select <b>Single Tenant</b> and <b>Create new Microsoft App ID</b>, then create the resource.</li>
<li>Open <b>Settings</b>, then <b>Configuration</b>. Set the messaging endpoint to
<code>{cfg["public_url"]}/api/messages</code> and apply it. Copy the Microsoft App ID.</li>
<li>Select <b>Manage Password</b>, then <b>Certificates &amp; secrets</b>. Create a client secret
and copy its value while it is visible.</li>
<li>Open <b>Channels</b>, add <b>Microsoft Teams</b>, accept the terms and apply.</li>
</ol>
<h2>Step 2. Save the bot credentials</h2>
<p>This form checks the credentials with Microsoft and stores them on the bot server.
The secret is never displayed again. The form closes after successful registration.</p>
<form method=post action="{base}">
<label for=client_id>Bot ID, application client ID</label>
<input type=text id=client_id name=client_id required pattern="[0-9a-fA-F-]{{36}}"
placeholder="00000000-0000-0000-0000-000000000000" autocomplete=off>
<label for=client_secret>Client secret value</label>
<input type=password id=client_secret name=client_secret required autocomplete=off>
<button type=submit>Check and save</button>
</form>
<h2>Step 3. Approve the app in Teams</h2>
<p>The app package download appears here after registration.</p>"""
    return _page(f"{app} setup", f"""
<h1>{app} bot for Teams</h1>
<p class=sub>Setup and approval for {org}</p>
<p>{app} posts replies from an AI assistant to approved project chats.</p>
<div class=status><div>Bot server: <span class=ok>Online</span></div>{registration}</div>
{flash}
<h2>What it does</h2>
<ul>
<li>Accepts activity only from the configured organization's tenant.</li>
<li>Receives live messages through the Bot Framework and catches up through Microsoft Graph
with its own app credentials and per-chat <code>ChatMessage.Read.Chat</code> consent.
Catch-up runs at startup and every {cfg['catchup_interval_seconds']} seconds. Its first read covers
messages modified in the last {cfg['catchup_lookback_days']} days, including older messages edited recently.
Live delivery may omit ordinary messages or other apps' posts.</li>
<li>Stores inline screenshots privately for {cfg['media_retention_days']} days, up to
{cfg['media_max_bytes']} bytes each. Transient image failures retry separately from message catch-up; permanent denials such as
403/404 are unavailable. Expired image credentials are renewed on a later retry. Graph-only images may be unavailable when Microsoft denies access.</li>
<li>Keeps shared-file names and links only. The bot cannot download files; the owner's own
Microsoft login is needed to open them. It cannot read mail or chats without consent.</li>
<li>Stores every message in chats it has been added to, including messages from people Toucan doesn't route,
edits, deletions and restorations, with sender name and Teams ID or application sender ID.</li>
<li>Posts as {app}. Microsoft signs incoming requests and the server verifies them.</li>
</ul>
{steps}
<p class=muted>Questions: {cfg["contact"] or "your bot administrator"}</p>""")


def privacy_page() -> str:
    cfg = {k: escape(str(v)) for k, v in config.load().items()}
    return _page(f'{cfg["app_name"]} privacy', f"""
<h1>{cfg["app_name"]} privacy statement</h1>
<p>This bot is operated by {cfg["org_name"]}. It stores every message in chats it has been added to,
including messages from people Toucan doesn't route, edits, deletions and restorations,
with sender name and Teams ID or application sender ID. It receives live activity and uses its own
app identity and per-chat consent to recover missed messages through Microsoft Graph at startup
and every {cfg['catchup_interval_seconds']} seconds. Its first read covers
messages modified in the last {cfg['catchup_lookback_days']} days, including older messages edited recently.
Message creation dates can be years earlier. It records which delivery paths supplied each message.
Inline screenshots are stored privately for {cfg['media_retention_days']} days, up to
{cfg['media_max_bytes']} bytes per image. Transient image failures retry with backoff; denied or missing images remain
unavailable. Retry records contain no credentials. Shared files keep names and links only; the bot cannot
download them and the owner's own Microsoft login is needed. Message records have no automatic
expiry. An AI assistant uses those
messages to answer project requests. For access or deletion questions, contact
{cfg["contact"] or "your bot administrator"}.</p>""")


def terms_page() -> str:
    cfg = {k: escape(str(v)) for k, v in config.load().items()}
    return _page(f'{cfg["app_name"]} terms', f"""
<h1>{cfg["app_name"]} terms of use</h1>
<p>This bot is for approved users of {cfg["org_name"]}. Its replies come from an AI assistant
and can be wrong. Check important information before acting on it.
Contact: {cfg["contact"] or "your bot administrator"}.</p>""")
