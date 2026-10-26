# Configuration reference

Toucan reads one TOML file: `~/.config/toucan/toucan.toml`, or the path in `$TOUCAN_CONFIG`.
State lives in `~/.local/state/toucan/toucan.db`, or `$TOUCAN_DB`. `toucan init` writes a
starting config from [examples/toucan.example.toml](../examples/toucan.example.toml).

Change it with `toucan add`, `toucan remove` and `toucan config set|get|unset` rather than by
hand; those commands validate the whole file before writing. Run `toucan check-config` after
any manual edit. It rejects invalid values and warns about keys it
doesn't know, so a typo can't silently do nothing. `toucan doctor` goes further and checks
that the tools and files the config points at actually work.

Secrets never go in this file. Webhook URLs live in hook files and the alert URL can live in
`alert_url_file`. The bot keeps its credentials in `~/.config/toucan-bot/credentials.json` (mode 600).

## `[settings]`

| Key | Default | Meaning |
|---|---|---|
| `owner_name` | `"the owner"` | Your name, as agents see it in prompts and escalations. |
| `owner_teams_id` | none | Your Entra object ID. Toucan uses it to tell your messages in Teams chats from everyone else's. Needed when any route uses Teams. |
| `owner_whatsapp` | none | Your personal WhatsApp JID (`15550000000@s.whatsapp.net`). Drafts and escalations go here. Required before any draft or escalation can be sent. |
| `timezone` | `"UTC"` | IANA zone used for every time Toucan shows (`Europe/London`, `America/New_York`). |
| `alert_url` | none | URL that receives alerts. See [setup-alerts.md](setup-alerts.md). |
| `alert_url_file` | none | Mode-600 file containing the alert URL, for secret ntfy topics. When set, it wins over `alert_url` and must contain a valid HTTP URL. |
| `upload_command` | `"toucan-upload-loom"` | Command used by `toucan upload`. The default comes from the optional `toucan-tools` package. See [setup-upload.md](setup-upload.md). |
| `transcribe` | none | Command that takes an audio file path and prints its transcript. Voice notes are transcribed before they reach a thread. |
| `rate_per_hour` | 20 | Most WhatsApp messages Toucan sends to one recipient per hour, counting drafts and escalations to you. |
| `rewake_minutes` | 15 | Wake an owner thread again after this long while requests stay unclaimed. |
| `wake_gap_minutes` | 2 | Shortest gap between two wakes of the same thread. |
| `max_wakes` | 3 | Wakes per request before Toucan stops and alerts. |
| `ack_alert_minutes` | 5 | Alert when a woken thread hasn't claimed a request after this long. |
| `claimed_stale_hours` | 4 | Alert when a claimed request has no reply or promise after this long. |
| `done_unclosed_minutes` | 30 | Alert when a request marked done still has no reply. |
| `whatsapp_stale_minutes` | 120 | The WhatsApp store counts as stale after this long. wacli writes only when messages arrive, so raise it on quiet numbers. |
| `context_hours` | 24 | How much earlier chat history the inbox shows as context. |
| `fallback_minutes` | 5 | Hand a rate-limited thread's unclaimed request to its fallback after this long. |
| `rotate_tokens` | 200000 | Ask an idle owner thread to hand over to a fresh thread at this context size. |
| `rotate_stall_minutes` | 60 | Alert when a handover hasn't finished after this long. |

`ntfy` is accepted as an old name for `alert_url`.

## `[sources.whatsapp]`, `[sources.teams]`, `[sources.email]`, `[t3]`

| Key | Default | Meaning |
|---|---|---|
| `sources.whatsapp.store` | `~/.local/state/wacli` | wacli's store folder. |
| `sources.whatsapp.account` | `"wa"` | Label stored with each WhatsApp message. It is part of each message's identity, so don't change it once Toucan has synced. |
| `sources.whatsapp.service_unit` | none | systemd user unit that runs `wacli sync`. When set, health checks it is active. |
| `sources.teams.bot_db` | `~/.local/state/toucan-bot/bot.db` | The Teams bot's store, opened read-only. |
| `sources.teams.stale_minutes` | 5 | The bot counts as down when its heartbeat is older than this. |
| `sources.teams.service_unit` | none | systemd user unit that runs `toucan-bot serve`. When set, health checks it is active. |
| `sources.email.notmuch_config` | notmuch's default | Passed to notmuch as `NOTMUCH_CONFIG`. |
| `sources.email.stale_minutes` | 60 | The mail index counts as stale when its newest message is older than this. |
| `t3.db` | `~/.t3/userdata/statev2.sqlite` | T3 Code's database, opened read-only. |

## Bot catch-up and media configuration

These keys belong in `~/.config/toucan-bot/config.toml`, not the router config. See
[Teams setup](setup-teams.md) for per-chat app upgrades, consent and the tenant chat-RSC
setting `Get-MgBetaChatRscConfiguration`.

| Key | Default | Meaning |
|---|---|---|
| `owner_teams_id` | empty | Owner's human Entra ID; person aliases require a confirmed roster with exactly the owner and that person. Falls back to the router's `settings.owner_teams_id`. |
| `catchup_interval_seconds` | 120 | Run Graph catch-up at startup, then wait this many seconds between passes. |
| `catchup_lookback_days` | 7 | First read's modified-time lookback for each non-personal chat. Existing cursors survive config changes. |
| `roster_interval_seconds` | 3600 | Refresh Connector chat rosters periodically, as well as when added or membership changes. |
| `media_max_bytes` | 10485760 | Maximum downloaded inline-image size, including streamed responses without Content-Length. |
| `media_retention_days` | 30 | Delete older cached screenshots in local maintenance, including when credentials have been removed. |

All numeric keys must be positive integers. `TOUCAN_BOT_STATE_DIR` overrides the bot state
directory. Screenshots are stored in its private `media/` directory. Shared-file attachments
retain names and links only and require the owner's own Microsoft login. Graph message-list
403s record missing per-chat RSC consent or an unsupported chat type; hosted-image 403s can
occur even with successful message reads. `toucan-bot status --json` and `toucan doctor` expose
catch-up health and observed live-delivery gaps independently of the heartbeat.
Per-chat roster failures appear in status `roster.errors` and doctor's `teams.roster` item.
Page reads are paced per chat; retry/page state survives restarts. Pending screenshots retry
with backoff independently of the message cursor. Permanent image failures such as 403/404
remain unavailable. Image 401s retain their job and obtain a fresh token before the next due
retry. Person aliases are withdrawn during roster refresh; superseded responses are discarded.
Readers use one snapshot for roster confirmation and members. Media workers claim jobs
atomically, with a five-minute crash lease, and reject superseded completions. These internal
safety rules require no extra configuration.
The bot's schema-4 store assigns a durable change sequence on inserts and content or
visibility changes, including restoration and screenshot metadata updates. The router saves
the store generation and last sequence in its own database. It rescans after a replacement,
rebuild or lower source sequence, including restoring a bot.db backup, deduplicates by
message key and applies the original eligibility cutoffs.
Discovery is independent of creation-time lookback.
Bot maintenance retries failed iterations after one to 60 seconds and expires screenshots
off the event loop in batches of at most 32. These limits also require no configuration.
Expiry, catch-up, Connector and Graph image retries, and roster refresh have separate supervised loops so
a persistent failure in one does not stop the others.
Image jobs commit with message writes. Filesystem errors stay pending with backoff.
See [operations](operations.md) for retry and deferred-request behavior.

## `[people.KEY]`

One table per person who may ask for work. `KEY` is how routes refer to them.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Display label only. Never used to identify anyone. |
| `role` | no | Free text for your own reference (`pm`, `qa`). |
| `whatsapp` | no | List of JIDs: the phone JID and any `@lid` alias. |
| `teams_id` | no | Entra object ID (`sender_aad_id` in `toucan-bot messages --json`). |
| `email` | no | List of email addresses. |

Each identity may belong to only one person.

## `[threads.KEY]`

One table per T3 owner thread. See [setup-t3.md](setup-t3.md).

| Key | Required | Meaning |
|---|---|---|
| `hook_file` | yes | Mode-600 file holding the thread's T3 webhook URL. |
| `t3_thread` | no | T3 thread ID, used only when Toucan can't find the live thread from the hook's task. |
| `project` | no | Free text for your own reference. |
| `fallback` | no | Another thread key that takes requests while this one is rate-limited. |
| `rotate_tokens` | no | Overrides `settings.rotate_tokens` for this thread. |

## `[[routes]]`

One entry per group of chats that belongs to one thread.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Unique route name, used in commands (`--route NAME`). |
| `label` | no | Human name for the route, used in the drafts Toucan sends you. |
| `source` | yes | `whatsapp`, `teams` or `email`. |
| `chats` | yes | WhatsApp JIDs, Teams chat IDs, or (for email) your receiving addresses. A chat may appear in only one route. |
| `thread` | yes | Owner thread key. |
| `mode` | yes | `act`: replies go out directly. `draft`: replies come to you on WhatsApp. Email routes must be `draft`. |
| `requesters` | yes | Person keys whose messages become requests. Everyone else's messages are context only. |
| `active_from` | yes | ISO 8601 time with a timezone. Earlier messages never become requests. |
| `post_as_bot` | no | Teams only. Post `act` replies in the chat as the Teams bot. |
| `owner_reply_closes` | no | Teams only. If you answer in the chat before Toucan sends anything, the request closes as `owner replied`. |
| `reply_via_whatsapp` | no | Teams `act` routes without `post_as_bot`. Send the reply to the asker's WhatsApp instead of drafting it to you. |
| `reply_to` | no | WhatsApp `act` routes. Send every reply to this person's WhatsApp instead of the chat (for groups with clients in them). |
| `reply_prefix` | no | Text added at the top of every reply and send on this route. |

## Where Teams replies go

| Route | Reply goes to |
|---|---|
| `mode = "draft"` | you, as a WhatsApp draft |
| `act` with `post_as_bot = true` | the Teams chat, posted by the bot |
| `act` with `reply_via_whatsapp = true` | the asker's WhatsApp (draft to you if they have no phone JID) |
| `act` with neither | you, as a WhatsApp draft |

## Moving existing state to a new install

If `TOUCAN_DB` points at a database from an earlier install, its WhatsApp messages carry that
install's account label. When the labels don't match, `toucan sync` refuses to run (exit 2)
and `toucan doctor` reports `whatsapp.account` as a todo. Set `sources.whatsapp.account` to the
label it names. Otherwise every message in the lookback window would look new and closed
requests would come back.

## Environment variables

| Variable | Meaning |
|---|---|
| `TOUCAN_CONFIG` | Config file path. |
| `TOUCAN_DB` | State database path. |
| `TOUCAN_LOOM_CLIENT_ID` | OAuth client ID for `toucan-upload-loom` (toucan-tools). |
| `TOUCAN_SHEETS_DIR` | Folder holding `toucan-sheets`' OAuth client and token (toucan-tools; default `~/.config/toucan/sheets`). |
| `TOUCAN_BOT_STATE_DIR` | Bot state directory, default `~/.local/state/toucan-bot`. |
| `TOUCAN_BOT_CONFIG` | The bot's config file path. |
| `TOUCAN_BOT_TENANT_ID`, `TOUCAN_BOT_PUBLIC_URL`, `TOUCAN_BOT_PORT` | Override the bot's config file. |
