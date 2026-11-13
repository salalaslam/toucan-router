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

## Pausing routing

```sh
toucan pause --reason 'maintenance'          # until `toucan unpause`
toucan pause --until 2h                      # or an ISO time; the first sync after it carries on
toucan unpause
```

While paused, `toucan sync` reads no source, creates no requests, sends no wake-ups,
acknowledgements or alerts, and exits 0 with a `paused` object. `toucan digest` sends nothing;
open cards wait for the first list after the pause. Owner threads can still `reply`, `send`
and `escalate` on requests they already hold, and `toucan wait` keeps running. `toucan status`
shows the pause, and `toucan log` has `routing_paused` and `routing_unpaused` events. Pausing
again replaces the end time and reason and keeps the original start.

The pause lives in Toucan's database, so it holds however sync is run: the systemd timer,
another scheduler or by hand. The first sync after `toucan unpause` (or after `--until`) catches
up the way it does after downtime: messages that arrived during the pause become requests.
WhatsApp and email are read back only `--lookback-hours` (48 by default), so after a longer pause
`toucan unpause` warns that older messages from those sources will not become requests. Teams
catch-up uses the bot's change sequence and has no such limit.

This is not the same as `settings.pause_file`, which leaves routing on and tells owner threads
to keep replies short and start no long work, or `toucan disable`, which stops the systemd
units that `toucan enable` wrote.

## The request lifecycle

```sh
toucan inbox --thread project-a
toucan claim 42 43 --thread project-a                 # prints a claim id
toucan reply 42 --claim CLAIM --final --text 'Fixed and deployed to staging.'
toucan reply 43 --claim CLAIM --promise 'send the test results' --due 3h --text 'On it, results in about 3 hours.'
toucan reply 44 --claim CLAIM --keep-open --text 'Which environment?'
toucan reply 42 --claim CLAIM --final --file /tmp/report.pdf --caption 'Report attached.'
toucan react 42 --claim CLAIM --emoji check           # Teams routes with post_as_bot only; sync already adds settings.ack_emoji (eyes) on arrival
toucan done 43 --claim CLAIM --note 'Finished; waiting on a decision.'
toucan close 43 --claim CLAIM --reason 'No reply needed.'
toucan release 43 --claim CLAIM                       # back to the inbox
toucan forward 43 --to project-b --thread project-a --reason 'Billing, not the portal'
toucan escalate 43 --claim CLAIM --category deploy --want 'Ship the billing fix' \
  --question 'Release PR 109 to production now?' --option 'Yes, release now' --option 'Wait for PR 110' \
  --pick A --note 'Full context, sent only if the owner replies more.'
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

A thread whose turns stay short doesn't need a waiter: a desk that claims each request,
acknowledges it and hands the work to a delegate is free again within a minute or two, so
webhook wakes reach it without waiting long. Set `waiter = false` on that thread
([configuration](configuration.md#threadskey)) and `inbox` stops asking for one. If such a
thread does long work itself, new requests wait until that turn ends; the unclaimed-request
alert still fires.

## Escalations

An escalation reaches you only as a decision card, and only for what you alone can decide:
`deploy` (a production deploy, release or merge), `client` (a commitment to a client), `scope`
(scope, an estimate or priorities between projects), `money`, `access` (access or credentials),
or `asked` (the requester asked for you personally). `toucan escalate` refuses anything else,
and anything without one question (under 160 characters), two to four options (under 80 each)
and the thread's pick. Routine waiting and dependency tracking belong to the handling thread.
It records who or what is needed and waits without reminders, rather than asking whether to
keep the request open. Tracking questions about blocked or pending work are refused even
when labelled as scope or access decisions. A new decision about spending, access or a
client commitment can still be escalated while work is blocked. Toucan writes the card itself:

```text
*T43* · project-a · Alice
Wants: Ship the billing fix
Decide: Release PR 109 to production now?
A) Yes, release now  ← suggested
B) Wait for PR 110
Reply "T43 A", quote this, or "more"
```

The request stays open with no claim. Answer on WhatsApp in any of these ways:

- `T43 B` (or `T43 B, but tell Alice first`) answers card T43 from anywhere in the chat. A bare
  letter reaches the thread spelled out, as `B) Wait for PR 110`.
- Quote the card. An unquoted reply answers the card sent just before it, or the only open one that
  hasn't been answered (a card answered in the last 10 minutes still takes follow-ups).
  With several open, Toucan asks once, in one line, which card it is for.
- `more`, quoting the card, or `more T43`, sends the thread's full note and the requester's
  message. Quoting that detail message answers the card too.

To check that Toucan is up, send `ping` on its own to the assistant number. Toucan replies
`pong` within about a minute with the time, the number of open requests and the cards waiting
on you. A ping older than 10 minutes when Toucan first sees it gets no reply.

The answer goes back to the thread as `OWNER ANSWER`; the request returns to `new` and the
thread is woken. If the thread escalates the same request again with the same question, the
new note is added to the card without a message (`more` shows it) and the card waits for the
daily list. A different question is a new card.

`toucan digest` (run it once a day from a timer) sends one message listing every card still
waiting on you, with its options, and how many requests threads settled without you the day
before. It sends nothing when nothing is waiting. `--dry-run` prints it.

`toucan retriage` hands every request escalated before cards existed back to its thread, which
the inbox tells to settle it or escalate it again as a card. Those cards are held for the next
daily list instead of being sent one by one.

If you answer in the T3 thread instead, the thread runs `toucan resume REQ --thread KEY`. It
finds what you typed in that thread after the escalation, claims the request and prints your
messages.

To answer from a separate personal T3 thread, add its ID to `settings.owner_t3_threads`
(default `[]`), then run:

```sh
toucan answer T435 B --thread T3_THREAD_ID --note 'Owner chose to wait'
```

The card must be open and the option must be one of its letters. Toucan reads T3 history
read-only and requires evidence after the card was created or last reopened: a message with
role `user` and `createdBy = "user"` containing the card ID or chosen option text, or a
resolved `user_input` picker response whose answer contains the chosen option text. A picker
answer letter also counts when its question names the card. Agent relays and the command's
note never count as evidence. Missing history or an unsupported schema fails closed.
The answer reaches the owning thread's inbox, returns the request to `new`, and removes the
card from the digest. `toucan log` records the owner, source `t3:<thread>`, option, note and
evidence record ID. This trusts T3's user provenance and the local configuration; it does
not authenticate against agents that can rewrite those files.

## Rate limits and the fallback thread

Each sync reads the latest run and message of every owner thread from T3. When the latest
run failed at a usage or rate limit (Codex records this without writing a message), or the
latest message is a model rate-limit error from the latest run, the turn has ended and nothing
will restart it. A newer running or finished run clears the state. Toucan alerts once,
then wakes the thread every `rewake_minutes` so it carries on when the limit clears. When the
error says when the limit ends (Codex's "try again at Nov 11th, 2026 8:52 PM", read in
`timezone`; the proxy's "reset after 2h12m20s"; Claude Code's "resets 7pm (UTC)"), those wakes
wait until then, since earlier ones fail at once. If the thread has `fallback = "OTHER"`, its
unclaimed requests move to that thread after `fallback_minutes`, unless the fallback is
rate-limited too. A request is handed over once: if the fallback forwards it back, it stays
with the owner thread until that thread picks it up. If the fallback hits a limit before it
claims a handed-over request, the request goes back to its owner as soon as the owner is no
longer limited.

## Forwarding a request

`toucan forward REQ --to THREAD --thread KEY [--reason TEXT]` moves a request that thread KEY
holds to another configured thread. A new request moves as it is. A claimed request needs its
current `--claim`; otherwise the command refuses (exit 3) and says to pass the claim or
release it first. Either way the request arrives at the target as new and unclaimed, with a
fresh wake budget, and keeps any promise already made. Done, escalated and closed requests,
and requests with a delivery in progress, can't be forwarded, nor can a request the router
is still holding. On a live-routed chat, a thread that doesn't know the right thread uses
[`toucan bounce`](#bouncing-a-request) instead.

The `forwarded` event records the old thread, the new one and the reason. The target's
`toucan wait` reports it at once and the next sync wakes the target. Its inbox then shows that
chat, and it can claim, reply and react into the original chat as usual. The original thread
still sees the chat, but its inbox labels the message as belonging to the other thread, and
it can't claim it. Requests handed to a fallback thread show the same way.

## The router

Each route normally sends every request to its one `thread`. The router lets Toucan pick the
thread for each request instead. A route's `router` setting has three values:

- `"off"` (default): every request goes to the route's thread.
- `"shadow"`: the router decides and logs where each new request would go, and changes
  nothing. Requests still go to the route's thread, and wakes and replies don't change.
- `"live"`: requests go where the router decides (see [Live routing](#live-routing)). The
  route's thread becomes its default: where requests go when the router can't decide.

It needs `[projects.KEY]` tables, `settings.router_general_thread`, and usually
`settings.router_commands` (see [configuration](configuration.md)). On a shadow route,
requests created before the router was switched on are not decided; on a live route, only
requests created while it is live are routed.

The router is experimental. Run a route in `"shadow"` for a few days first, mark its
decisions with `toucan router mark`, and check `toucan router stats` before you switch it
to `"live"`. Follow-ups are the weak spot: a no-project message from the same person within
`followup_minutes` joins their last request, even when it starts a new topic.

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
went. On a live route it uses the thread the request is in now. Steps 1 and 3 skip a message
that was split across threads, since it has no single thread to follow.

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
(`router_failed` once any route is live) and `route_marked`. Live routes add the events listed
under [Live routing](#live-routing). `toucan status` has a `router` object (last run, last
command success, failure count over 24 hours, pending decisions, and on live routes the number
held and waiting for an answer) and a `held` list, and `toucan doctor` adds `router.commands`
and `router.health`. `router.health` warns when a held request is more than two minutes past
`router_hold_minutes`, which means syncs aren't running.

### Live routing

With `router = "live"`, sync applies each decision:

1. **Rules first, no delay.** Steps 1 to 3 run inside sync as the request is created, so a
   request they place is in its thread before that sync's wakes go out. The inbox tells the
   thread why ("routed here because names Project B").
2. **Held for the router command.** A request the rules can't place is held (state `held`): it
   is not woken, not in any thread's inbox as a request, can't be claimed or forwarded, and isn't
   handed to a fallback thread. The router stage after the sync decides it and wakes the thread in the same
   sync, using the usual wake gap and wake budget. A request whose earlier request (the one it
   replies to or follows) is itself held waits for that one first.
3. **Never stranded.** A request not decided within `router_hold_minutes` (default 5), for
   example because every command timed out or another sync held the router lease, goes to the
   route's thread with reason `router timed out`, and raises a `router:timeout:REQ` alert.
   Switching a route to `"shadow"` or `"off"`, or removing it, hands its held requests to the
   route's thread at the next sync (decision `released`). A router stage failure doesn't fail
   the sync; held requests then time out as above.
4. **After assignment it is an ordinary request.** Rate-limit handover to a thread's fallback,
   `toucan forward`, `owner_reply_closes`, owner answers to escalations and `toucan resume`
   all work as before, and the request's state is `new` again.
5. **A late decision never wins.** Each request has a routing generation that changes whenever
   it is held, asked about, assigned (by a decision, a timeout, a release or an answer) or
   bounced. A router command's decision applies only if the generation it started from is
   still current when it is written, checked in the same transaction as the assignment or the
   question. Otherwise it is dropped (event `route_discarded`) and the request waits for the
   next decision, so a slow command can't undo a timeout or send a request back to a thread
   that bounced it in the meantime.

Every live decision is a `router_decisions` row with mode `live`, so `toucan router review`,
`mark` and `stats` cover live routing too. A request decided again after a bounce has several
rows; `mark` marks the latest. Extra decision kinds: `answer` and `no_answer` (step 6, a
question to the person), `fallback` (step 6, a question that couldn't be asked), `timeout`
(step 5) and `released` (step 0).

Events: `route_held`, `routed` (a request got its thread, by any step), `route_live` (the
router stage's decision, with each failed attempt, like `route_shadow`), `route_live_deferred`,
`route_split`, `router_asked`, `router_answered`, `route_discarded` (a decision that arrived
after the request had moved on: timed out, released, answered or bounced), `bounced`, `bounce_escalation_failed`,
`promise_not_sent` and `reaction_kept`.

`toucan sync --dry-run` places requests with the rules in its in-memory copy, reports which
held requests would need a command, and sends nothing. `toucan sync --shadow` never asks
anyone a question; held requests it can't place fall back to the route's thread.

Versions before live routing don't know the `held` state, so they leave held requests alone:
they neither wake nor hand them out, and nothing releases them either. To go back to such a
version:

1. Switch every live route to `router = "shadow"` (or `"off"`).
2. Run `toucan sync` once with the current version. It releases every held request to its
   route's thread (decision `released`) and wakes it; `toucan status` then shows an empty
   `held` list.
3. Install the older version.

Installing the older version first leaves held requests stranded until a live-routing version
runs a sync again.

### Bouncing a request

```sh
toucan bounce 42 --thread project-b --reason 'This is about the portal' --json   # add --claim ID if claimed
```

A thread that gets a request that isn't its runs `toucan bounce`. Claim rules are the same as
`toucan forward`: a new request, or a claimed one with its current claim. The request goes
back to the router, held as above, and is decided again without any thread that has bounced
it: a rule that would pick such a thread falls through to the next step, the router command's
prompt lists those threads' projects as excluded, and an answer that picks one anyway becomes
a question. The general thread can be excluded too. When the router can't place it, it goes to
the route's thread, or the general thread if the route's thread bounced it.

After `max_bounces` (default 2) bounces the request is escalated to the owner on WhatsApp
instead, through the usual escalation path, from the route's thread, which gets the owner's
answer back. If that escalation can't be sent, the request is a new request in the route's
thread and the command exits 1. A bounce on a route that isn't live is refused (exit 3) with a
pointer to `toucan forward`.

### Split requests

When the router command answers `split` on a live route, the message becomes one request per
thread. The first keeps the original request id; the others are new requests on the same
message. Each has the part of the message it covers (`part`) and a label made from its project
names (`part_label`). Parts whose projects share a thread become one request. A part that is
bounced and then split again is asked about instead.

- The inbox shows the part first: "This request is only this part of the message: ...", then
  the whole message, and names the thread that has another part.
- `toucan reply` starts every reply and draft for a part with its label, as in
  `Project B: rerun done`, unless the text already starts that way.
- One acknowledgement per message. On Teams, the bot's reaction is shared: a second `toucan
  react` with the same emoji on the same message does nothing, and removing it does nothing
  while another part is still open. On every route, a `--promise` reply is recorded (promise
  and due time) but not sent when another part of the same message already had a reply sent;
  `--final` and `--keep-open` replies are always sent.
- An edit or deletion of the message flags every part.
- `owner_reply_closes` treats the message as one: once any part has had a reply sent, an owner
  message in the chat stands no part down, and a part holding a promise is never stood down.
  An owner message before any reply closes every part.

### Asking the person

When the decision is `ask` on a live route (the command is unsure, every command failed, or the
router hit an error), Toucan sends the person one question the way a reply would go, through
the outbox (with no request id), counted against `rate_per_hour`:

- `Which project is this about: Project A or Project B?` when the command named two to four
  candidate projects;
- otherwise `Which project is this about? ` followed by the projects the person's recent
  requests went to, or every project name if there are none.

A message from them in the same chat within `router_ask_minutes` (default 15) answers it when it
names exactly one project and either

- replies to or quotes the question (it answers that question, however long it is), or
- quotes nothing and is essentially just the project name: once the project names and the
  words a, an, the, it, it's, its, is, was, for, about, this, that, one, project, app, please,
  thanks, thank, you, yes, yeah, yep, ok, okay, sorry, oh, i, mean and meant are taken out,
  nothing is left ("Project A please", "the billing one", "it's billing"). It answers the latest
  open question. "fix billing" has a word left over, so it becomes a request of its own.

The request goes to that project's thread and is woken in that sync, and the answer message is
context, not a new request. Anything else they send in that window, such as "Separately, fix
the billing export crash" or a short message quoting something else, is handled as a new
message as usual and the question stays open. With no answer in time the request
goes to the route's thread with reason `no answer to the question`. A request is never asked
about twice.

A question can only go where the answer will come from, so Toucan asks only on WhatsApp `act`
routes without `reply_to`, and on Teams `act` routes with `post_as_bot`. On other routes, when
the hourly limit is reached, or when the question fails to send, the request goes to the
route's thread straight away (decision `fallback`). A question whose delivery is uncertain is
treated as asked.

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

When an owner thread's context reaches `rotate_tokens`, sync asks it to hand over. It waits
only for deliveries in flight, not for the thread to go idle or for new requests: T3 queues the
rotation wake behind a running turn, so the handover starts at the next gap between turns.
Requests the old thread never claimed stay new, and get a fresh wake budget for the successor
once the hook points at it. Delegates the old thread started still report back to it; it
forwards their results to the successor. The old thread runs
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

## Ledger and Preflight

Experimental. A project with a [`[projects.KEY.ledger]`](configuration.md#projectskeyledger)
table gets a timeline of what its people sent Toucan and what Toucan sent them, and a check
before each send.

**The timeline** comes from the router's own state: routed messages from the project's people
and the thread's sends (`toucan reply` and `toucan send`). A send counts for a project only when
its thread owns the project, so someone who works on two projects has two separate timelines.
GitHub issues, pull request comments and review comments from the listed repositories are added
by `toucan sync` every `github_minutes`, through `gh api`; `gh` must be signed in. Each page and
its cursor commit together and items are keyed by GitHub ID, so an interrupted read repeats at
most one page. Each read takes at most about a minute; what's left continues on the next run. A
failed read is logged (`ledger_github_error`) and never fails a sync.

**Preflight** runs on each send from the project's thread to one of its people. It holds a send
when:

| Rule | Holds when |
|---|---|
| `pel_first` | Review material (a link to a `review_hosts` host, or a `review_words` message with links) goes to someone other than the PEL, and the PEL hasn't had the same topic or link yet, or got it less than `pel_wait_minutes` ago and hasn't replied. |
| `review_link` | A message about wireframes or mock-ups links to a host that isn't in `review_hosts` (GitHub links are ignored). |
| `supersede` | The message replaces something a person other than the PEL already has. |
| `burst` | The person has had `burst_per_hour` sends in the last hour without writing back. |

In `shadow` mode the send goes out and the verdict is recorded as a `preflight` event. In
`enforce` mode a held send is refused (exit 3) with the reasons, and recorded as a `preflight`
event with `refused: true`. The thread fixes the message or sends it with
`--override "why it must go now"`, which is recorded too. Who each send was for is recorded when it
is queued, since WhatsApp replaces `@[person]` tags with names. Drafts,
escalations and the router's own questions are never checked. If the check itself fails, the
error is recorded and the send goes out.

The owner thread's `toucan inbox` lists who it has messaged in the last two hours and about which
topics.

```sh
toucan ledger show --project project-a --since 2d [--person alice] [--topic 2.1] [--json]
toucan ledger replay --project project-a --since 14d --json   # Preflight on past sends, as they stood then
toucan ledger status --json                                   # modes, GitHub reads, verdicts this week
toucan ledger sync --json                                     # read GitHub now
```

Use `replay` before you switch a project to `enforce`: it shows which past sends each rule
would have held.

## Data and retention

The state database holds copies of routed messages, requests, deliveries and the event log.
Toucan creates its state folder with mode 700 and the database with mode 600. It doesn't
change the mode of a folder that already exists; `toucan doctor` warns (`state.permissions`)
if the folder or database is readable by other users. Source databases
are always opened read-only. Media downloaded for transcription is kept in
`~/.local/state/toucan/media/`. Ledger projects also store copies of their GitHub issues and
comments in the database. The router doesn't delete old data on its own; the bot expires its screenshot cache as described below. Back up or prune
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
which retries transient failures with backoff. A Connector 404 means the stored conversation
is unavailable. Its aliases remain invalid, its history stays intact, and it is checked
again at the normal roster interval without failing the entire sweep. Logs identify the
conversation, HTTP status and exception class without printing credentials or response bodies.

The bot pins the Teams API and common HTTP packages to the tested 2.1.1 SDK family.
Bot send and reaction commands have a 25-second deadline covering initialization, token
acquisition, delivery and retries. A send succeeds only with a real Connector activity ID;
missing IDs and the SDK placeholder are ambiguous. Reactions succeed on the Connector's
successful response, which supplies no new activity ID. Exit 4 means the request may have
landed, so inspect the chat before retrying. Setup timeouts and timeouts while waiting to
retry a definitive rejection fail with exit 1. The receipt confirms Connector acceptance;
it does not independently confirm that Graph or a Teams client can display the message.

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
