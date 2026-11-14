# Architecture

Toucan is a command-line tool and a timer. Every few minutes `toucan sync` reads approved
chats, records new messages as requests, and wakes the T3 thread that owns each one. The agent
in that thread then runs `toucan` commands through its shell to claim the request and reply.
There is no daemon in the router and no agent loop. The only long-running process is the
optional Teams bot.

```
 wacli store ─┐                                         ┌─ T3 webhook task ─> owner thread
 bot store  ──┼─ read-only ─> toucan sync ─> toucan.db ─┤
 notmuch    ──┘                                         └─ alerts, cards, acknowledgements
                                                                   ^
 owner thread ─ toucan claim / reply / escalate ─> outbox ─> wacli or toucan-bot
```

## Ownership boundaries

Toucan owns one SQLite database, `~/.local/state/toucan/toucan.db`, mode 600 in a mode-700
folder. Everything else it touches belongs to another program, and Toucan opens it read-only:

| Store | Owner | What Toucan reads |
|---|---|---|
| wacli's database | `wacli sync` | WhatsApp messages and the phone-to-`@lid` identity map |
| the bot store (`bot.db`) | `toucan-bot` | Teams messages, mentions, rosters and downloaded images |
| the notmuch index | your mail sync | email from routed senders |
| T3's database | T3 Code | the live thread behind a hook, context size, rate-limit stops |

The router never imports the bot or `toucan-router-tools`. It reads the bot's store as a file
and runs uploaders and senders as commands. The bot alone talks to Microsoft Graph and the Bot
Framework. This keeps the router on the Python standard library and lets each package release
on its own.

T3 internals are optional. [`t3.probe`](../src/toucan/t3.py) checks the columns Toucan needs.
When a T3 update changes them, rotation, rate-limit fallback and `resume` turn off,
`toucan doctor` warns, and wakes keep working, because a wake is only an HTTP POST to the
thread's webhook task.

A wake carries the thread key, a count and the time of the newest request. It never carries
message text. The agent reads the text with `toucan inbox`, so the text stays in Toucan's
database and the agent's own context.

## People and requests

A route ties chats to one owner thread and lists its requesters. Toucan matches a sender by
WhatsApp JID or `@lid`, Entra object ID or email address, never by display name. A message
becomes a request only when its sender is a requester, it is newer than both the route's
`active_from` and the route's first read (the seed), and it isn't the owner's own message.
Every other message is stored as context, so the thread can see the conversation around a
request.

A request moves through `new`, `held`, `claimed`, `done`, `escalated` and `closed`. `held`
only appears on a live router route, while the router decides where the request goes. A
claim returns a claim ID, and every later command on the request needs it, so two threads
can't answer the same request.

An edit or deletion of a request's message marks the request changed and gives it a fresh wake
budget. A screenshot that finishes downloading is reported once, without counting as an edit.

## One sync pass

[`App.sync`](../src/toucan/core.py) runs in this order:

1. If routing is paused, stop. A paused Toucan reads no source, so a broken source can't fail
   the run.
2. Transcribe new voice notes, outside the transaction, because transcription is slow.
3. In one transaction: check source health, read Teams catch-up state, load the WhatsApp
   identity map, read the owner's answers to decision cards, read every route, create
   requests, expire held requests, wake threads, ask oversized threads to rotate, check
   promises and claims, expire stuck deliveries, and reconcile alerts.
4. After the commit: send queued notices and arrival reactions.
5. Run the router stage for requests it holds, then wake the threads it placed them in.
6. Read GitHub for ledger projects. This step is slow and optional, so a failure is logged and
   never fails the sync.

Each source is read on its own. An unreadable source is skipped and reported, and the others
still sync. Alerts fire once per problem and clear when it recovers.

Teams progress is the bot store's change sequence and store identity, read in one snapshot and
committed with the messages it covers. If the identity changes, or the sequence goes backwards
after a `bot.db` restore, Toucan rescans. WhatsApp and email have no such sequence and are read
back `--lookback-hours`, 48 by default, and deduplicated by message key.

## Waking a busy thread

T3 queues a webhook run behind the thread's current turn. A thread doing long work would see
no wake until the turn ends, so owner threads keep `toucan wait` running as a background
command. It polls the database and exits as soon as the thread has a new or changed request.
Wakes are batched: a first wake waits at most `wake_gap_minutes`, repeats wait
`rewake_minutes`, and a request gets at most `max_wakes` before Toucan alerts that it is
unclaimed.

## Sends: intent before effect

Every outgoing message, whether a reply, a draft to the owner or a decision card, goes through
the `outbox` table:

1. Insert the row as `pending` and commit.
2. Change it to `sending` and commit, before calling `wacli` or `toucan-bot`.
3. Record `sent`, `failed` or `uncertain` with the sender's result.

A sender that times out or is killed after step 2 may have delivered the message, so the row
becomes `uncertain` and Toucan never resends it on its own. The bot exits with code 4 when
Teams may have accepted a message whose response never arrived. That is `uncertain` too.
Further replies on that request are refused until a person checks. A row still `pending` after
five minutes never reached a sender, so it is marked failed and is safe to retry.

The request state changes in the same transaction as the `sent` result. A reply with `--final`
closes the request only once the message has gone.

## The router

Most routes send every request to one thread. A route with `router = "live"` lets Toucan pick
the thread per request. The first three steps are pure functions over plain data and run inside
sync's transaction: reply or quote linkage, a project named in the message, then a follow-up
from the same person. A request they can't place is held.

The fourth step runs `settings.router_commands`, for example `claude -p`, with a prompt on
stdin. It runs after sync's transaction has committed and its wakes have gone out, so a slow
command never holds a database lock, and it stops once `router_budget_seconds` is used. Every
routing transition bumps `requests.route_gen`. A decision applies only if the generation it
started from is still current, so a slow decision can't undo a timeout or a bounce. This is
the only place Toucan calls a model, and only when you configure a command.

## Escalations

An owner thread can't send the owner free text. `toucan escalate` takes a category (`deploy`,
`client`, `scope`, `money`, `access` or `asked`), one question, two to four options and the
thread's pick, and [`cards.render`](../src/toucan/cards.py) builds a short WhatsApp card. The
owner answers "T435 B". Sync reads the answer from the owner's chat and hands the request back
to the thread. The thread's longer note goes out only if the owner replies "more".

## Effects and dry runs

The clock, subprocesses, HTTP and sleep are fields of one
[`Effects`](../src/toucan/effects.py) object passed into `App`. Tests replace them, so no test
touches the network, a real chat or a real clock.

`toucan sync --dry-run` copies the database read-only into memory and runs the same code
against the copy. It wakes nothing, sends nothing and writes no state. `--shadow` records state
but doesn't wake or send.

## Configuration

`toucan` commands write `toucan.toml`; agents never edit it by hand. Each command validates
the whole new config before it writes, so a bad change is refused and nothing
changes. Every command prints JSON with `--json` and uses the same exit codes: 0 success,
1 operational failure, 2 validation or usage error, 3 refusal.

## Where to look

| Module | What it does |
|---|---|
| `cli.py` | argument parsing and exit codes |
| `config.py`, `configedit.py` | loading, validating and editing `toucan.toml` |
| `core.py` | `App`: sync, the request lifecycle, the outbox, wakes, rotation, escalations |
| `sources.py` | read-only adapters for wacli, the bot store and notmuch |
| `router.py` | the live and shadow router |
| `cards.py` | decision cards |
| `ledger.py` | the experimental ledger and Preflight |
| `t3.py` | read-only T3 database access and the schema probe |
| `storage.py` | the state schema and migrations |
| `setup.py`, `services.py`, `agentsetup.py`, `discovery.py` | `init`, `doctor`, `enable`, `guide` and `discover` |

The bot's storage rules (message identity, tombstones, media claims, Graph back-off) are in
[AGENTS.md](../AGENTS.md#working-on-the-code). [Operations](operations.md) covers the same
behaviour from the operator's side.
