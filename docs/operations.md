# Operations

How Toucan behaves day to day, and how to recover when something goes wrong. Every command
supports `--json`. Exit codes are 0 for success, 1 for an operational failure, 2 for a
validation or usage error, and 3 for a refusal.

## Running on a timer

```sh
cp contrib/systemd/toucan-sync.* ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now toucan-sync.timer
journalctl --user -u toucan-sync.service -n 50
```

Each `toucan sync` reads every source, records new and edited messages, wakes threads,
sends alerts and checks promises. Use `toucan sync --dry-run` to see what it would do without
writing anything, and `--shadow` to record state without waking or sending.

## The request lifecycle

```sh
toucan inbox --thread project-a
toucan claim 42 43 --thread project-a                 # prints a claim id
toucan reply 42 --claim CLAIM --final --text 'Fixed and deployed to staging.'
toucan reply 43 --claim CLAIM --promise 'send the test results' --due 3h --text 'On it, results in about 3 hours.'
toucan reply 44 --claim CLAIM --keep-open --text 'Which environment?'
toucan reply 42 --claim CLAIM --final --file /tmp/report.pdf --caption 'Report attached.'
toucan react 42 --claim CLAIM --emoji eyes            # Teams routes with post_as_bot only
toucan done 43 --claim CLAIM --note 'Finished; waiting on a decision.'
toucan close 43 --claim CLAIM --reason 'No reply needed.'
toucan release 43 --claim CLAIM                       # back to the inbox
toucan forward 43 --to project-b --thread project-a --reason 'Billing, not the portal'
toucan escalate 43 --claim CLAIM --note 'Needs a production deploy.'
toucan send --route team-chat --thread project-a --text 'The test results are ready.'
toucan upload /tmp/proof.mp4 --title 'Fix walkthrough'
toucan status
toucan log --thread project-a --since 24h
```

Every reply says what it does. `--final` closes the request. `--promise WHAT --due WHEN`
(`30m`, `2h`, `1d` or an ISO time) keeps it open and records the commitment. An open promise
shows in `inbox` and `status` and sends one alert once it is overdue. `--keep-open` keeps it
open for a question or a partial answer.

`send` starts a message with no request, for example to deliver a promise in a new message.
It needs `--route` and the route's owner `--thread`.

When a Teams request @mentions only other people (not the bot, the owner or the whole chat),
its `inbox` header says it is for them and should be closed without a reply unless the text
also asks Toucan. Mentions are classified only when `settings.owner_teams_id` is set. A later
change in who a request tags (for example Graph identifying a pushed mention) counts as a
change to an open request, like an edit. Screenshot download progress is not an edit: when an
image becomes available, `toucan wait` and the inbox report the request once, without a fresh
wake budget.

The first successful read of each route seeds earlier history as context. Later messages
become requests only if they arrive after both that seed and `active_from`. To pick up older
unanswered messages on purpose, run `toucan import-pending --route NAME --since ISO`.

## Waiting for requests

T3 queues a webhook run behind the thread's current turn, so a busy thread sees no wake until
that turn ends. Owner threads therefore keep `toucan wait --thread KEY` running as a
background command. It exits as soon as the thread has a new or changed request, which
reaches the agent mid-turn, and the agent starts it again. It times out after 110 minutes
(`--timeout`). `inbox` warns when no waiter is running and `status` lists waiters.

## Escalations

`escalate` sends you an excerpt of the request and the thread's note on WhatsApp, and leaves the
request open with no claim. Reply on WhatsApp, quoting the escalation, and the answer goes
back to the thread as `OWNER ANSWER`. The request returns to `new` and the thread is woken. An
unquoted reply answers the escalation sent just before it, or the only open one. If several
are open, Toucan alerts and asks for a quoted reply.

If you answer in the T3 thread instead, the thread runs `toucan resume REQ --thread KEY`. It
finds what you typed in that thread after the escalation, claims the request and prints your
messages.

## Rate limits and the fallback thread

Each sync reads the latest run and message of every owner thread from T3. When the latest
run failed at a usage or rate limit (Codex records this without writing a message), or the
latest message is a model rate-limit error from the latest run, the turn has ended and nothing
will restart it. A newer running or finished run clears the state. Toucan alerts once,
then wakes the thread every `rewake_minutes` so it carries on when the limit clears. If the
thread has `fallback = "OTHER"`, its unclaimed requests move to that thread after
`fallback_minutes`, unless the fallback is rate-limited too.

## Forwarding a request

`toucan forward REQ --to THREAD --thread KEY [--reason TEXT]` moves a request that thread KEY
holds to another configured thread. A new request moves as it is. A claimed request needs its
current `--claim`; otherwise the command refuses (exit 3) and says to pass the claim or
release it first. Either way the request arrives at the target as new and unclaimed, with a
fresh wake budget, and keeps any promise already made. Done, escalated and closed requests,
and requests with a delivery in progress, can't be forwarded.

The `forwarded` event records the old thread, the new one and the reason. The target's
`toucan wait` reports it at once and the next sync wakes the target. Its inbox then shows that
chat, and it can claim, reply and react into the original chat as usual. The original thread
still sees the chat, but its inbox labels the message as belonging to the other thread, and
it can't claim it. Requests handed to a fallback thread show the same way.

## The router

Each route normally sends every request to its one `thread`. The router is being built so
that Toucan can pick the thread for each request instead. For now it runs only in shadow
mode: set `router = "shadow"` on a route and it decides and logs where each new request would
go. Requests still go to the route's thread, and wakes and replies don't change.

It needs `[projects.KEY]` tables, `settings.router_general_thread`, and usually
`settings.router_commands` (see [configuration](configuration.md)). Requests created before a
route's router was switched on are not decided.

For each new request it tries these steps in order and stops at the first that answers:

1. **Reply or quote.** The message replies to or quotes a request's message, or a reply
   Toucan sent for one: that request's thread. WhatsApp uses the quoted message ID wacli
   stores. Teams uses the bot store's `reply_to_id`, quoted-reply entities and message
   references. Email uses `In-Reply-To`, read from the message file. Without that linkage
   the step is skipped.
2. **Project named.** Project keys, names and aliases found in the message's own text, not in
   quoted text (for email, lines starting with `>` and everything after an `On ... wrote:`
   line). Exactly one project: its thread. Two or more go to step 4.
3. **Follow-up.** No project named, and the same person had a request in the same chat whose
   message was at most `followup_minutes` earlier: that request's thread. In a group this
   follows the person, not the chat. A WhatsApp 1:1's phone and `@lid` chats count as one chat.
4. **Router command.** One stateless call per request. The prompt lists the projects, the
   person's last `router_history` messages in the chat, their open requests and threads, and
   the new message, all marked as untrusted data. Each command in `router_commands` runs in
   turn with the prompt on stdin. The first that exits 0 and prints a valid answer wins.
   Anything else (non-zero exit, timeout, rate-limit text, invalid JSON, unknown project keys,
   unexpected fields or kinds) moves on to the next command. A command's output is only ever
   read as a label; nothing in it is run or followed.
5. **Unavailable.** No commands configured, or all failed: the decision is `ask`, with reason
   `router unavailable`.

The answer must be one JSON object, optionally inside one Markdown code fence:

```json
{"kind": "project", "projects": ["project-a"], "parts": [], "reason": "names the portal"}
```

| `kind` | Meaning | Decided thread |
|---|---|---|
| `project` | exactly one project | that project's thread |
| `split` | independent asks for two or more projects, one `parts` entry each | each part's project thread, with its text |
| `depends` | one task across projects; the first listed is the one whose code changes | the first project's thread |
| `offtopic` | not about any project | `router_general_thread` |
| `unsure` | can't tell; `projects` may list candidates | none: the decision is `ask` |

When a step uses an earlier request's thread, it uses that request's own decision while
routing is shadowed, unless a thread has forwarded the request, in which case it uses where it
went.

Example commands, each with tools turned off:

```toml
router_commands = [
  ["claude", "-p", "--model", "claude-haiku-5-5", "--tools", "", "--no-session-persistence"],
  ["codex", "exec", "--model", "gpt-6-luna", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "-"],
]
```

### Timing and failures

The router runs at the end of `toucan sync`, after the sync transaction has committed and its
wakes and alerts have gone out. It holds no database lock while a command runs, so replies and
`toucan wait` carry on. It starts a command only if the command's full `router_timeout_seconds`
still fits in `router_budget_seconds` for this sync. Requests it doesn't reach are decided,
oldest first, in a later sync, and a command that already failed for a request isn't tried
again for it. A timed-out command is killed with its whole process group. The timer unit's
syncs never overlap, and a lease in the state database stops a manual `toucan sync` from
deciding the same requests at the same time. A sync can take up to `router_budget_seconds`
longer, which delays the next sync's wakes by as much, so keep the budget small if wake
latency matters.

Any router error is recorded as a `route_shadow_failed` event and a `router:failed` problem
without failing the rest of the sync. A request whose own data breaks the rules is decided as
`error` so it isn't retried every minute. Three requests in a row on which every command
failed raise a `router:unavailable` alert. `toucan sync --dry-run` reports which requests
the rules would decide and which would need a command, and calls no command and writes
nothing.

Events: `route_shadow` (one per decision, with step, kind, decided threads, reason, command
index, latency and each failed attempt's outcome), `route_shadow_deferred`, `route_shadow_failed`
and `route_marked`. `toucan status` has a `router` object (last run, last command success,
failure count over 24 hours, pending decisions), and `toucan doctor` adds `router.commands` and
`router.health`.

### Reviewing decisions

```sh
toucan router review --since 24h --json            # also --route NAME, --undecided-only
toucan router mark 42 wrong --note 'Billing is project-b' --json
toucan router stats --since 2026-10-29T00:00:00+00:00 --json
```

`review` lists each decision with the request, time, route, person key, message text,
the thread the request really went to, the decided threads or parts, step, kind, reason and
verdict. `--undecided-only` lists only decisions not yet marked right or wrong. `mark` records
a verdict (exit 3 if the request has no decision). `stats` counts decisions by step, kind,
decision and verdict, and gives the share of marked decisions that are wrong.

## Thread rotation

When an idle owner thread's context reaches `rotate_tokens`, sync asks it to hand over. It
waits until no requests or deliveries are pending. The old thread runs
`toucan rotate --thread KEY`, which prints step-by-step instructions. The old thread writes a
handoff brief and launches a successor with the same model and modes. The successor creates
its own webhook task and rewrites the hook file. Claims and promises carry over, and the
config doesn't change.

`toucan rotate --thread KEY --check` reports the live thread and its context size, and exits
with code 3 while a handover is unfinished. `--reset` clears a stuck handover. Sync alerts if a
handover hasn't finished after `rotate_stall_minutes`.

## Deliveries and recovery

Toucan writes each delivery to its outbox before calling wacli or the bot. If a send is
interrupted while the sender runs, or the bot exits with code 4 (Teams may have accepted the
message but the response never arrived), the delivery becomes `uncertain` and Toucan never resends
it automatically. Further replies on that request are refused until a person has checked
whether the message arrived. A delivery still `pending` after five minutes never reached the
sender, so Toucan marks it failed, alerts, and allows a retry. Failed sends alert once each,
stay visible in `status`, and are retried explicitly. The `rate_per_hour` limit applies to
messages for other people and counts pending, sent, sending and uncertain attempts to that
recipient from the last hour. Drafts and escalations to you are never held back.

An unavailable source is skipped and reported, and the others still sync. Alerts fire once
per problem until it recovers.

## Data and retention

The state database holds copies of routed messages, requests, deliveries and the event log.
Toucan creates its state folder with mode 700 and the database with mode 600. It doesn't
change the mode of a folder that already exists; `toucan doctor` warns (`state.permissions`)
if the folder or database is readable by other users. Source databases
are always opened read-only. Media downloaded for transcription is kept in
`~/.local/state/toucan/media/`. The router doesn't delete old data on its own; the bot expires its screenshot cache as described below. Back up or prune
the state folder the way you would any other private data.


## Teams catch-up and delivery health

`toucan-bot status --json` reads the local store without contacting Microsoft. Its `catchup`
object reports the interval and each non-personal chat's cursor, last success age, last
error, HTTP status and stale flag. Its `delivery` object counts messages from the last 24
hours first seen by Graph with no live delivery after a five-minute grace period. Historical
first-run recovery can contribute to this count. Application posts count in `graph_only`
but not `ordinary_graph_only`; they are context only in the router.

`toucan doctor` adds `teams.catchup` and `teams.delivery` in the normal doctor item format.
`teams.catchup` covers routed chats only and names the route. It warns after an HTTP 401 or
403, after other failures or no success for longer than the greater of five minutes and three
configured intervals, for never-synced chats, and when the bot has not seen a routed chat.
An unavailable schema or store is a todo. `toucan sync` raises the same problems as alerts,
one per route (`teams:catchup:ROUTE`), once until they recover. A 403 there means only
@mentions reach Toucan in that chat; the alert says to upgrade the app and accept its
permissions in the chat. Catch-up retries there within 15 minutes of consent, or at once
when an ordinary message is pushed in the chat. A chat that has never synced is reported after a catch-up pass that
ended after the bot first saw it, or at the latest three intervals (at least five minutes)
after the later of bot start and first sight, with the catch-up task's error if it keeps
failing. A restart therefore raises no false alert, and a failing catch-up still shows.
Conversations no route uses never raise router alerts. Missing ordinary push delivery warns that Toucan depends on catch-up
latency. Zero observed gaps does not prove delivery, and a fresh heartbeat proves only that
the process is running. Inspect these items separately from `teams.fresh`.

The status `background` object reports each task's health, including last
success age, current error class, last failure time and failure count. `teams.background`
warns for failed or stale tasks; missing records require restarting the upgraded bot.
Heartbeat, expiry, catch-up, Connector and Graph media retries, and roster refresh have separate supervised
loops, so a persistent expiry error cannot block the other operations. Each retries failed
iterations with asynchronous exponential backoff from one to 60
seconds. A SQLite lock causes rollback and retry, rather than stopping maintenance.
The HTTP loop uses a zero SQLite busy timeout. Failures log only their class, not exception
text. If another writer also blocks health persistence, `/healthz` reports the failure from
memory; status and doctor retain the previous success until its age becomes stale. The
failure count and time persist when the writer releases its lock and the task recovers.

For message-list HTTP 403, check that app version 1.1.0 was upgraded or reinstalled and
consented in that exact chat. Have an administrator check `Get-MgBetaChatRscConfiguration`
and effective app exceptions. A missing RSC grant or unsupported chat type can return 403.
The app's catalog update alone does not grant per-chat access. A Graph hosted-image 403 is
recorded on the attachment as unavailable and does not fail message catch-up.

The bot migrates older stores to schema version 4 on startup, preserving messages, sent
receipts and conversations. The router probes that version read-only. Back up the bot store
before an upgrade and restart the upgraded bot before running the new router. Internal media
claim columns migrate additively, preserving queued work without changing the router's
version-4 read contract. Version 4 adds a store generation and durable change sequence.
New stores, upgrades from older schemas and message-table rebuilds receive a new random
generation and assign sequences to retained messages. Ordinary restarts preserve both.
Each chat's
cursor advances only after every page completes. Page requests are spaced at least one second
apart per chat. HTTP 429/503 and transport failures retry the same page up to three times,
with exponential backoff and Retry-After support, including HTTP-date values. A wait over
30 seconds defers the chat to a later pass. Page position, window and retry time survive a
restart, so recovery resumes at the failed page while other chats can continue.

Mutation activities never supply creation times. Graph's createdDateTime repairs creation
independently of content revision ordering. The router persists deferred eligibility for
incomplete observations first seen after route seeding. Once creation and requester identity
are known, it creates one request only if the message meets both its original cutoff and
the current active_from cutoff. Initial seed context stays context. Deferred rows remain
discoverable through later changes beyond the normal lookback. Deleted messages stay deleted under stale edits; undelete
activity or a newer Graph non-deleted snapshot restores visibility. An undelete notification
without a body preserves stored text, HTML and attachments.

The bot has one durable change sequence across all chats. Inserts and changes to message
content, sender identity, creation time, visibility or attachment metadata advance it in
the same transaction. Duplicate deliveries that change no content do not advance it.
Replies also receive their parent's change sequence so derived quotes stay current without
tracked-row reads, including when the quoted parent arrives after its reply.
The router keeps a per-route, per-chat pair of store generation and last change sequence
in its own database. It reads that pair, changed messages and quotes in one read-only source
snapshot and commits progress with ingestion. It rescans when the generation changes or
the source sequence falls below saved progress, including after restoring a bot.db backup,
deduplicating by chat and message ID. This replaces the rowid watermark and Teams sync's
lookback and tracked-row exceptions. The first pass after an upgrade rescans existing rows.
Creation time still controls seed and active_from eligibility; old context stays context.
An unseen tombstone can be skipped safely because restoration advances the sequence and
becomes discoverable even after an extended outage, creating an eligible request once.

Pending screenshots have durable retry work separate from message cursors. Timeouts, 401,
429 and server errors stay pending with exponential backoff and Retry-After support. Maintenance
retries both live and Graph images even when messages are unchanged. Permanent failures,
such as 403/404, become unavailable. After an image 401, the next due retry requests a fresh
Graph or Connector token with the bot's stored credentials, bypassing the SDK's cached token.
Only one token renewal runs per media batch; repeated 401s and renewal failures retain the
job with bounded exponential backoff. Pending work contains no tokens.
Message writes and image queuing commit in one transaction for both delivery paths,
including retained Connector attachments when Graph enriches a message. An interruption
cannot commit a message while losing its image jobs. Image-directory creation, permission
and publication errors stay pending with backoff. Neither image worker runs inside message
catch-up, so a persistent media failure cannot block message listing or cursor progress.

Each worker atomically claims a due image job before HTTP. A claim expires after five
minutes so another worker can recover after a crash. Cancellation releases its own claim.
No database transaction spans network waits. Downloads stay in private temporary files
until the worker checks that it still owns the job and the current attachment is pending.
Late successes and failures cannot replace available or expired attachments, overwrite
newer bytes, or consume replacement work. Completion preserves current attachment names
and other message metadata. Expiry and publication share a write lock; a cache entry
removed before completion stays pending for retry instead of publishing a missing path.

Message merges, queue reconciliation and expiry use short write transactions so they cannot
overwrite a concurrent delete or attachment edit. Router message and quote reads, bot health
counts and roster resolution each use a consistent SQLite snapshot.
Expiry runs in an owned worker thread with its own zero-wait SQLite connection. It scans
files and selects message batches outside the writer lock, then rechecks current files and
attachments under the lock. Each transaction handles at most 32 file candidates or 32
messages. Cancellation stops between batches and joins the worker before shutdown returns.

Roster confirmation uses a per-chat generation stored in the bot database. Each fetch and
membership change invalidates the prior confirmation. Superseded responses and failures
are discarded, including an older fetch that finishes after a newer one. Person aliases
remain unavailable until the current fetch succeeds. Topics and route aliases still work
subject to the person-name precedence rules. Name resolution reads members, confirmation
and generation in one transaction; confirmation must match the current generation.
Older confirmations paired with a different generation remain unavailable until refreshed.
Status reports per-chat failures in `roster.errors`; doctor warns in `teams.roster`.
The refresh pass processes every chat before reporting any failure to its supervisor,
which retries with backoff rather than reporting success and waiting the full roster interval.

Screenshot files expire after `media_retention_days`, default 30, during bot maintenance.
Local expiry also runs after credentials are removed, while the bot server is running.
Expiry removes the cached bytes and clears their stored paths; message text, file links
and other metadata have no automatic expiry. The router can retain an older path until it
next syncs that message, so an owner agent must check that a screenshot path still exists.
A shared file has only its name and link; the bot cannot download it and the owner's own
Microsoft login is needed.

Before a live cutover, use operator-controlled chats to check ordinary no-mention delivery,
Graph recovery after downtime, matching push/Graph IDs, paging, edits/deletes/restoration,
application cards, human 1:1 name resolution and desktop/mobile screenshots. Check image
bytes independently from message-list permission. Offline tests cannot prove tenant grants,
Microsoft delivery or hosted-content access.
