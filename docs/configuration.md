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
| `drafts_whatsapp` | `owner_whatsapp` | Where drafts for you to post go instead: a phone JID, or a group JID (`120363000000000000@g.us`) such as an alerts group with only you in it. Escalation cards and the daily list always go to `owner_whatsapp`. |
| `timezone` | `"UTC"` | IANA zone used for every time Toucan shows (`Europe/London`, `America/New_York`). |
| `telemetry` | `true` | `false` stops the one anonymous usage event a day. See [telemetry](telemetry.md). |
| `alert_url` | none | URL that receives alerts. See [setup-alerts.md](setup-alerts.md). |
| `alert_url_file` | none | Mode-600 file containing the alert URL, for secret ntfy topics. When set, it wins over `alert_url` and must contain a valid HTTP URL. |
| `upload_command` | `"toucan-upload-loom"` | Command used by `toucan upload`. The default comes from the optional `toucan-router-tools` package. See [setup-upload.md](setup-upload.md). |
| `transcribe` | none | Command that takes an audio file path and prints its transcript. Voice notes are transcribed before they reach a thread. |
| `rate_per_hour` | 20 | Most messages Toucan sends to one other person or chat per hour. Drafts and escalations to you are never limited. |
| `rewake_minutes` | 15 | Wake an owner thread again after this long while requests stay unclaimed. |
| `wake_gap_minutes` | 2 | Shortest gap between two wakes of the same thread. |
| `max_wakes` | 3 | Wakes per request before Toucan stops and alerts. |
| `ack_alert_minutes` | 5 | Alert when a woken thread hasn't claimed a request after this long. |
| `claimed_stale_hours` | 4 | Alert when a claimed request has no reply or promise after this long. |
| `done_unclosed_minutes` | 30 | Alert when a request marked done still has no reply. |
| `whatsapp_stale_minutes` | 120 | The WhatsApp store counts as stale after this long. wacli writes only when messages arrive, so raise it on quiet numbers. |
| `thread_rules` | none | Standing rules for every owner thread, printed by `toucan inbox` after the accuracy rule. Inbox prints them in full when a thread hasn't seen them (a rotated successor counts as new), when they change, or after `context_hours`; otherwise one line says how to print them again (`toucan inbox --thread T --rules`). Use a multi-line string for several paragraphs. |
| `context_hours` | 24 | How much earlier chat history the inbox shows as context. |
| `fallback_minutes` | 5 | Hand a rate-limited thread's unclaimed request to its fallback after this long. |
| `rotate_tokens` | 200000 | Ask an owner thread to hand over to a fresh thread at this context size. A busy thread is asked too; its handover starts after the running turn. |
| `pause_file` | none | A JSON file another tool writes to pause work, such as a quota guard. While it holds `"paused": true`, `toucan inbox` tells owner threads to keep answering people briefly and start no delegates or long work. Optional `since` and `until` (Unix seconds) and `reason` (text) are shown in the note. A missing or unreadable file means no pause. Routing carries on; to stop routing, use `toucan pause` (see operations). |
| `rotate_stall_minutes` | 60 | Alert when a handover hasn't finished after this long. |
| `router_general_thread` | none | Thread key that gets off-topic requests. Required once any route has `router` other than `"off"`. |
| `router_commands` | `[]` | Commands the router tries in order, each an array such as `["claude", "-p"]`. Each reads the prompt on stdin and must print one JSON object. Empty means requests the rules can't place are decided as `router unavailable` (on a live route, the person is asked). |
| `router_timeout_seconds` | 60 | Longest one router command may run. At most `router_budget_seconds`. |
| `router_budget_seconds` | 90 | Router command time per sync, at most 180. Requests left over are decided in a later sync. |
| `router_history` | 20 | How many of the person's earlier messages in the chat the router sees. |
| `router_hold_minutes` | 5 | Live routes: a request the router hasn't decided after this long goes to the route's thread, with an alert. |
| `router_ask_minutes` | 15 | Live routes: how long the person has to answer "Which project is this about?" before the request goes to the route's thread. |
| `max_bounces` | 2 | Live routes: after this many `toucan bounce`s a request is escalated to you instead of routed again. |
| `followup_minutes` | 120 | A message that names no project, sent this soon after the same person's last request in the chat, follows that request. |

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
| `github` | no | List of GitHub logins. Used only by a project's [ledger](#projectskeyledger), to tell whose issue or comment it is. |

Each identity may belong to only one person.

## `[threads.KEY]`

One table per T3 owner thread. See [setup-t3.md](setup-t3.md).

| Key | Required | Meaning |
|---|---|---|
| `hook_file` | yes | Mode-600 file holding the thread's T3 webhook URL. |
| `t3_thread` | no | T3 thread ID, used only when Toucan can't find the live thread from the hook's task. |
| `project` | no | Project key used to resolve escalation delegation. Other free text is allowed. |
| `fallback` | no | Another thread key that takes requests while this one is rate-limited. |
| `rotate_tokens` | no | Overrides `settings.rotate_tokens` for this thread. |
| `waiter` | no | Default `true`. Set `false` for a thread whose turns stay short because it hands work to delegates (a desk). `toucan inbox` then stops asking it to start `toucan wait`, and `toucan setup-prompt` leaves that step out. `toucan add thread KEY --no-waiter` sets it. See [operations](operations.md#waiting-for-requests). |

## `[projects.KEY]`

One table per project the router can pick. `KEY` is how the router names it. See
[operations](operations.md#the-router).

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | The project's name. |
| `aliases` | no | Other names people use for it, such as a repo or product name. |
| `description` | no | One line the router's AI step reads. |
| `thread` | yes | Owner thread key that handles the project. |
| `rules` | no | Standing rules for this project, shown to the owner thread that handles it after `settings.thread_rules`, on the same schedule. Multi-line text is fine. |

The key, the name and each alias match whole words in a message, in any case, so `portal`
doesn't match `portals`. Two projects may not share a name or alias. `toucan remove thread`
refuses while a project or `router_general_thread` uses that thread.

```sh
toucan add project project-a --name 'Project A' --alias portal --description 'The customer portal' --thread project-a --json
toucan remove project project-a --json
```

### Project escalation delegates

| Key | Required | Meaning |
|---|---|---|
| `escalate_to` | no | Person key for the project engineering lead. The person must have a phone WhatsApp JID. |
| `escalate_from` | no | ISO datetime with an offset. Before this time, cards go to the owner. Omit to start immediately. |
| `escalate_wait_minutes` | no | Positive integer, default 10. Unanswered cards go to the owner once this many minutes after creation. |

Set or clear the policy atomically. Each set replaces all three escalation settings;
omitting `--from` starts immediately and omitting `--wait-minutes` uses 10.

```sh
toucan config escalation project-a --to bob --from '2026-11-06T09:00:00+00:00' --wait-minutes 10 --json
toucan config escalation project-a --clear --json
```

The command validates the whole config before writing. The existing `config set` and
`config unset` commands also accept these keys.

Toucan uses the saved live router decision, including split-message parts, to find the
project. Otherwise it uses the thread's `project` key, the project's thread, or fallback
ancestry. An original route can disambiguate a shared fallback. If the stored data identifies
several projects, delivery stays with the owner.

Sent cards remember the delegate's phone JID, name and wait time. Config changes apply to
new cards. The delegate can answer by card ID, letter or quote, and request `more`, in their
own WhatsApp chat. `T435 pass` forwards that card to the owner immediately; bare `pass`
works when only one card is open to that delegate. Passing sends a receipt and cancels the
timed forward. Both people may still answer. Receipts go to the person answering. The owner can also answer; the first
person's answer wins and the other receives an already-answered notice.

Periodic sync checks answers before forwarding overdue cards. Forwarding commits one outbox
attempt before sending, so restarts do not duplicate it. Failed or uncertain sends require
operator inspection, as with other deliveries. Closed, replaced and answered cards do not
forward. Held cards, same-question updates, the 09:00 owner list and Toucan's own routing
escalations keep their existing behavior.

### `[projects.KEY.ledger]`

Turns on the ledger and Preflight for one project. Both are experimental. The ledger is a
timeline of what the project's people sent and what Toucan sent them, on WhatsApp, Teams and
GitHub. Preflight checks each `toucan reply` and `toucan send` from the project's thread
against that timeline before the message is queued. See
[operations](operations.md#ledger-and-preflight).

```toml
[projects.project-a.ledger]
mode = "shadow"
people = ["alice", "bob"]
pel = "bob"
review_hosts = ["excalidraw.com"]
github = ["example/project-a"]
self_github = ["example-owner"]
```

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"shadow"` | `off`, `shadow` (record what Preflight would hold, send anyway) or `enforce` (refuse held sends unless the thread passes `--override`). |
| `enforce_rules` | all four rules | In `enforce` mode, the rules that refuse a send. The others are recorded as in `shadow` mode, so a rule can keep collecting verdicts while the accurate ones block. |
| `people` | `[]` | Person keys the ledger follows. Sends to anyone else are not checked. |
| `pel` | the project's `escalate_to`, if it is in `people` | The person who reviews material before everyone else. Set it to `""` to turn off the PEL-first check. |
| `review_hosts` | `[]` | Hosts whose links count as review material, such as a whiteboard or a question form. Subdomains match. |
| `review_words` | wireframe, mock-up, mockup, mock up | Words that make a message with links review material. |
| `topics` | `sprint` and `issue` | Name to regular expression. The first group (or the whole match) is the topic key, stored as `name:key`, such as `sprint:2.1` or `issue:12`. |
| `supersede` | "these replace", "instead of the … I sent" | Regular expressions for a message that replaces something the person already has. |
| `burst_per_hour` | 4 | Sends to one person within an hour, without them writing back, before the next is held. |
| `pel_wait_minutes` | 30 | How long other people wait for the PEL to reply before review material can go to them anyway. |
| `github` | `[]` | Repositories (`owner/name`) whose issues and comments go into the ledger. |
| `self_github` | `[]` | Logins whose GitHub posts count as sent by us. |
| `github_minutes` | 10 | How often `toucan sync` reads GitHub. |

Unknown keys are refused, so a typo can't silently turn a check off.

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
| `owner_reply_closes` | no | Teams only. If you answer in the chat before Toucan sends anything, the request closes as `owner replied`. A request with a promise, or whose message already had a reply sent for another part, stays open. |
| `reply_via_whatsapp` | no | Teams `act` routes without `post_as_bot`. Send the reply to the asker's WhatsApp instead of drafting it to you. |
| `reply_to` | no | WhatsApp `act` routes. Send every reply to this person's WhatsApp instead of the chat (for groups with clients in them). |
| `reply_prefix` | no | Text added at the top of every reply and send on this route. |
| `send_threads` | no | Other thread keys that may also use `toucan send` on this route, for messages that aren't replies (for example a project thread copying a status to a person whose chat another thread owns). Requests still go only to `thread`. |
| `router` | no | `"off"` (default); `"shadow"`: log which thread the router would pick for each request, without changing where it goes; `"live"`: send each request to the thread the router picks. See [operations](operations.md#the-router). |

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
| `TOUCAN_TELEMETRY` | `0` turns off the daily usage event, as does `DO_NOT_TRACK=1`. See [telemetry](telemetry.md). |
| `TOUCAN_LOOM_CLIENT_ID` | OAuth client ID for `toucan-upload-loom` (toucan-router-tools). |
| `TOUCAN_SHEETS_DIR` | Folder holding `toucan-sheets`' OAuth client and token (toucan-router-tools; default `~/.config/toucan/sheets`). |
| `TOUCAN_BOT_STATE_DIR` | Bot state directory, default `~/.local/state/toucan-bot`. |
| `TOUCAN_BOT_CONFIG` | The bot's config file path. |
| `TOUCAN_BOT_TENANT_ID`, `TOUCAN_BOT_PUBLIC_URL`, `TOUCAN_BOT_PORT` | Override the bot's config file. |
