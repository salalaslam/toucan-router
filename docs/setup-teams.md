# Setting up Microsoft Teams

Toucan reads and answers Teams chats through one small bot, `toucan-bot`. You add the bot to
the chats you want to route. It stores live activity locally and recovers missed messages through
Microsoft Graph. It posts Toucan's replies as its own user, "Toucan" by default. The router
reads only the bot's local SQLite store and never calls Graph or imports the bot package.

Setting it up takes one Azure Bot registration, a public HTTPS URL that reaches your machine,
and a Teams admin to approve the app. Each chat needs resource-specific consent for
`ChatMessage.Read.Chat`. Graph uses a client-credentials token from the bot's stored app ID
and secret, with `https://graph.microsoft.com/.default`. This grants no file or mail access.

`toucan doctor` checks these whenever a route uses `source = "teams"`:

| Item | Checks |
|---|---|
| `bot.command` | `toucan-bot` is installed |
| `bot.status` | the bot is configured and registered |
| `teams.store` | the router can read the bot's store |
| `teams.catchup` | per-chat last success age and failures, including HTTP status |
| `teams.delivery` | ordinary messages found by Graph without live push in the last 24 hours |
| `teams.fresh` | the bot wrote its heartbeat in the last `stale_minutes` |
| `teams.service` | the systemd unit is active (only when `service_unit` is set) |
| `owner_teams_id` | your own Entra object ID is set |

## 1. Install and configure the bot

```sh
uv tool install toucan-teams-bot
mkdir -p ~/.config/toucan-bot && chmod 700 ~/.config/toucan-bot
```

```toml
# ~/.config/toucan-bot/config.toml   (or $TOUCAN_BOT_CONFIG)
tenant_id = "00000000-0000-0000-0000-000000000000"   # your Entra tenant; activity from others is dropped
public_url = "https://bot.example.com"                # public HTTPS URL that reaches the bot's port
port = 3978
org_name = "Example Org"        # shown on the setup, privacy and terms pages
contact = "it@example.com"      # shown on those pages
app_name = "Toucan"             # the bot's name in Teams
developer_name = "Example Org"  # the Teams app manifest's developer
website = "https://example.com"
owner_teams_id = "00000000-0000-0000-0000-000000000009" # your human Entra ID for name resolution
catchup_interval_seconds = 120
catchup_lookback_days = 7
roster_interval_seconds = 3600
media_max_bytes = 10485760
media_retention_days = 30
```

`TOUCAN_BOT_TENANT_ID`, `TOUCAN_BOT_PUBLIC_URL` and `TOUCAN_BOT_PORT` override the file.
The server refuses to start without `tenant_id` and `public_url`.

## 2. Expose it over HTTPS and run it

Microsoft must reach `PUBLIC_URL/api/messages`. Any HTTPS reverse proxy works, for example
Tailscale Funnel, Cloudflare Tunnel or Caddy. Then run the server as a user service:

```sh
cp contrib/systemd/toucan-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now toucan-bot.service
curl -fsS "$PUBLIC_URL/healthz"
```

While it runs, the bot writes a heartbeat to its store every minute. That is how Toucan tells a
quiet chat from a dead process. A heartbeat does not prove that Teams delivers messages;
check `teams.catchup` and `teams.delivery` too.

## 3. Register the bot and approve the app

```sh
toucan-bot setup-url
```

prints a private setup link. Open it, or send it to whoever administers Azure and Teams. The
page walks through three steps:

1. Create an Azure Bot (free F0 tier, single tenant), set its messaging endpoint to
   `PUBLIC_URL/api/messages`, add a client secret, and turn on the Microsoft Teams channel.
2. Paste the bot's app ID and secret into the form. The server checks them with Microsoft and
   stores them in `~/.config/toucan-bot/credentials.json` with mode 600.
3. Download the Teams app package and upload it in the Teams admin center
   (Manage apps, Upload new app), or allow custom app uploads for the people who need it.

`toucan-bot status --json` then reports the tenant, public URL and whether credentials are
stored, without showing secrets.

### Message access, upgrades and consent

Teams bots normally receive @mentions in group chats. `ChatMessage.Read.Chat` requests
ordinary-message delivery and authorizes Graph message reads for the specific chat.
Delivery still needs a live test in each original human chat, including human 1:1s where
Teams reports the bot's conversation type as `groupChat`.

The app manifest is version 1.1.0, independent of the Python package version, so it can
upgrade an installed 1.0.1 app. Uploading the ZIP to the catalog does not update grants in
existing chats. Upgrade or reinstall the app in each chat and approve the requested RSC
permission. An administrator should inspect `Get-MgBetaChatRscConfiguration`, including
app exceptions or preapproval policy. `DisabledForAllApps` blocks consent. The separate
user-personal-scope RSC setting is not the chat setting.

A message-list HTTP 403 means no RSC grant in that chat or an unsupported chat type. Check
membership, the per-chat upgrade and consent, tenant policy, and app identity. Do not assume
that a tenant-wide protected-API approval is needed for RSC. The bot records the status and
retries on the next interval without advancing that chat's cursor.

### Catch-up and message state

At startup and every 120 seconds by default, the bot reads
`GET /v1.0/chats/{id}/messages` for each known non-personal conversation. It orders by
`lastModifiedDateTime desc`, filters by a per-chat cursor, and follows every page. The first
read covers messages modified in the last seven days by default, including older messages edited recently.
Their creation dates can be years earlier, and message records have no automatic expiry.
Later reads overlap the cursor by one second and
advance it only after all pages succeed. Page reads are paced at one request per second per
chat. Throttled or unavailable pages retry with Retry-After and bounded backoff; page position
and retry time survive a restart. Missed messages can be delayed by an interval plus
API and paging time. Messages last modified before the first-read window are not recovered
automatically through Graph. The router discovers inserts and later content or visibility
changes through a durable change sequence, independent of creation-time lookback. Its saved
progress includes the bot store's generation; a replacement, rebuild or source sequence
below saved progress triggers a rescan. This also handles restoring a bot.db backup.
Seed and active_from cutoffs still decide which recovered or restored messages become
requests, with each message eligible at most once.

Live activity IDs and Graph message IDs share the same per-chat message key, following the
previous Graph-backed router's use of message IDs for Connector reactions. The store merges
both paths and records `live_seen_at`, `graph_seen_at` and `first_source`. Check matching IDs
in a live test before cutover. Text, HTML, file references, cards, quotes and forwards remain
available to the owner agent. Application senders are context only, even when an app claims
a user's identity. An edit received before its original remains context until its creation
time and requester identity are known. Mutation timestamps, including localTimestamp, are
never creation times. Graph createdDateTime is authoritative; the router can then create a
deferred request once, subject to the original seed and active_from cutoffs. Seed history
stays context. A stale edit cannot undo a delete. An explicit undelete activity or a newer
Graph snapshot without `deletedDateTime` restores the message. An undelete notification
without content preserves the existing body and attachments.

### Screenshots and shared files

Live inline `image/*` attachments download from their `contentUrl` using Connector app
authentication. Graph-only inline images try that message's `hostedContents/.../$value`
with the Graph token. Microsoft's chat hosted-content permission table omits
`ChatMessage.Read.Chat`, so a successful message read does not promise image access. A 403
records `download_status = "unavailable"` and `http_status = 403`; catch-up continues.
Timeouts, 429 and server errors stay pending in a durable media queue and retry with backoff
independently of the message cursor. Download endpoints are rebuilt from validated IDs;
Graph image IDs must belong to the stored chat and message. Encoded traversal and redirects
are refused. Connector images use validated opaque attachment IDs from signed live activity.
The validator preserves a checked regional service prefix, including `/amer/v3/attachments/`
and `/amer-client-ss.msg/v3/attachments/`, without permitting traversal or other hosts.
An image 401 stays pending. On the next due retry, the bot obtains a fresh token for the
correct Graph or Connector scope from its stored credentials. Repeated authentication
failures back off; 403/404 image failures remain unavailable.
Image jobs commit with each message write for both delivery paths. Connector and Graph
image retries run independently of message catch-up. Image-directory creation and permission
errors leave the job pending with backoff and cannot stop message recovery.
Concurrent handlers and maintenance claim image jobs before HTTP and check ownership again
before publishing bytes or metadata. A crashed worker's claim expires after five minutes;
cancellation releases the claim. Late results preserve available or expired screenshots
and any newer attachment edits. Expiry and publication share a database write lock.
Expiry scans run off the HTTP event loop, in batches of at most 32 file candidates or
messages per transaction. Each batch rechecks current state before deleting or updating it.
Heartbeat, expiry, catch-up, Connector and Graph image retries, and roster refresh run independently and
recover from SQLite contention and other failures
with cancellation-aware backoff. Check the status `background` object and doctor's
`teams.background` item as well as catch-up and live-delivery health.

Images have a 10 MiB cap by default and live under the bot state directory's `media/`
subdirectory, mode 700 with mode-600 files. The bot removes images after 30 days by default
and marks their stored paths expired. It never downloads non-image files. A shared PNG file
reference is a file, not an inline screenshot. Inbox shows screenshots' local paths, and
shared files' names and SharePoint links with a note that the bot cannot download files
and the owner's own Microsoft login is needed. Files, image URLs and cards remain untrusted.
Local screenshot expiry continues after credentials are removed, while the server is running.

Microsoft references:

- [RSC message delivery](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-messages-with-rsc)
- [List chat messages and query parameters](https://learn.microsoft.com/en-us/graph/api/chat-list-messages?view=graph-rest-1.0)
- [Grant RSC and check tenant chat policy](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/grant-resource-specific-consent)
- [Regional Connector file and image URL examples](https://devblogs.microsoft.com/microsoft365dev/working-with-files-in-your-microsoft-teams-bot/)
- [Graph 401 authentication errors](https://learn.microsoft.com/en-us/graph/resolve-auth-errors#401-unauthorized-error-is-your-token-valid)
- [Receive inline images](https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/in-depth-guides/file-handling/receiving-inline-images)
- [Hosted-content permissions and bytes](https://learn.microsoft.com/en-us/graph/api/chatmessagehostedcontent-get?view=graph-rest-1.0)
- [Receive edit and undelete activity](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability)
- [Per-chat message throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits#chat-messages)
- [Retry-After handling](https://learn.microsoft.com/en-us/graph/throttling)
- [Chat rosters through the Connector](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/get-teams-context)

## 4. Add the bot to chats and find the IDs

Add the Toucan app to each chat you want to route, and send a message in it so the bot learns
about the chat. Then:

```sh
toucan-bot conversations            # chat IDs, types and names
toucan-bot messages --json          # recent messages with each sender's sender_aad_id
```

Use the chat ID in the route's `chats`, and each person's `sender_aad_id` as their `teams_id`.
Your own ID goes in `settings.owner_teams_id`, so Toucan can tell your messages from everyone
else's.

## 5. Route a chat

```sh
toucan discover teams --json
toucan config set settings.owner_teams_id 00000000-0000-0000-0000-000000000009
toucan add person bob --name 'Bob Example' --teams-id 00000000-0000-0000-0000-000000000002
toucan add route team-chat --source teams --chat '19:EXAMPLE@thread.v2' --thread project-a \
  --requester bob --mode act --post-as-bot
```

That writes this config:

```toml
[settings]
owner_teams_id = "00000000-0000-0000-0000-000000000009"

[sources.teams]
# bot_db = "~/.local/state/toucan-bot/bot.db"   # the bot's store (default)
# stale_minutes = 5                              # heartbeat age before the store counts as stale
# service_unit = "toucan-bot.service"            # optional systemd check

[people.bob]
name = "Bob Example"
teams_id = "00000000-0000-0000-0000-000000000002"

[[routes]]
name = "team-chat"
source = "teams"
chats = ["19:EXAMPLE@thread.v2"]
thread = "project-a"
mode = "act"
post_as_bot = true          # reply in the chat as the bot
owner_reply_closes = false  # true: if you answer in the chat first, Toucan stands down
requesters = ["bob"]
active_from = "2026-01-01T00:00:00+00:00"
```

Where a Teams reply goes:

| Route | Reply goes to |
|---|---|
| `mode = "draft"` | you, as a WhatsApp draft |
| `act` with `post_as_bot = true` | the Teams chat, posted by the bot |
| `act` with `reply_via_whatsapp = true` | the asker's WhatsApp (a draft to you if they have no phone JID) |
| `act` with neither | you, as a WhatsApp draft |

Any message you post in the chat after a draft arrives counts as posting that draft.

## Bot commands

```sh
toucan-bot send --to 'Project A' --text 'The build is green.' --dry-run
toucan-bot send --to Bob --text '...'           # your own human 1:1 with Bob, with the bot added
toucan-bot react --to 'Project A' --message ID --emoji eyes
toucan-bot delete --conversation ID --activity ID
toucan-bot reset --yes                          # forget the stored app ID and secret
```

`--to` resolves person names only from a complete Connector roster containing exactly the
owner and that person. Unknown members or missing owner membership prevent confirmation.
A person's confirmed human 1:1 takes precedence over group topics and route aliases,
including exact group aliases. Member names never create aliases for unnamed groups.
Use a group topic, a Teams route's `name` or `label`, or an explicit conversation ID for those
chats. Bot personal chats require `--conversation ID`. Ambiguous names are refused.

The bot fetches rosters when added, after membership changes and every hour by default.
Each roster fetch and membership change advances a per-chat generation and invalidates the
prior confirmation. A response from an older generation cannot overwrite a newer roster or
restore aliases while the current fetch is pending or failed. Names remain unavailable
until a current fetch succeeds.
Name resolution reads members, confirmation and generation from one SQLite snapshot, so a
fetch completing during resolution cannot confirm an older, incomplete member list.
Status reports each failed chat in `roster.errors`, and doctor warns in `teams.roster`.
A failed refresh leaves person aliases unavailable. Other chats still refresh before the
supervisor receives the aggregate failure and schedules a retry with backoff.
Set `owner_teams_id` in the bot config or `settings.owner_teams_id` in the router config.
Matching ignores case; exact matches take precedence among group aliases only.
The bot posts text only; send files another way.

`toucan-bot send` exits with code 4 when Teams may have accepted the message but the reply
never arrived. Check the chat before sending again. The router records such sends as
`uncertain` and won't retry them on its own. `--json` works before or after the subcommand.

## Moving from teams-reader

Earlier versions read Teams through teams-reader. `sources.teams.reader_state` and
`settings.stale_minutes` are gone, and `toucan check-config` warns if they're still in your
config. Add the bot to your routed chats, set `owner_teams_id`, and keep your existing route
chat IDs and `teams_id` values: they are the same IDs.
