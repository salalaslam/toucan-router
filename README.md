<h1 align="center">Toucan Router</h1>

<p align="center">Let colleagues message your coding agents on WhatsApp, Teams and email.</p>

<p align="center">
  <a href="#quick-start"><strong>Quick start</strong></a> ·
  <a href="docs/">Docs</a> ·
  <a href="docs/faq.md">FAQ</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
  <a href="https://pypi.org/project/toucan-router/">PyPI</a>
</p>

Toucan Router connects WhatsApp, Microsoft Teams and email to the Claude Code and Codex threads
you already run in [T3 Code](https://github.com/pingdotgg/t3code). A colleague asks in a chat,
the thread that owns that chat wakes up and does the work, and Toucan tracks the request until
the thread replies, promises a follow-up, escalates to you or closes it.

```
WhatsApp (wacli) ─┐                       ┌─> T3 thread "project-a" ─┐
Teams (bot)     ──┼─> toucan sync ─ wake ─┤                          ├─> toucan reply / promise / escalate
Email (notmuch) ──┘    (allowlist)        └─> T3 thread "project-b" ─┘        │
                                                                              v
                                    WhatsApp, Teams (as the bot), or a draft to you
```

## What you get

- **Your agents, your subscriptions.** Toucan runs no agent of its own. It wakes the T3 thread
  you already run, and the agent there answers with its own tools.
- **Only the people you approve.** Toucan reads the chats you route and matches people by
  WhatsApp JID, Entra ID or email address, never by display name. Everyone else stays context.
- **Every request tracked.** `claim`, `reply --final`, `reply --promise WHAT --due 3h`, `done`,
  `close`, `escalate`. An overdue promise or an unclaimed request sends you an alert.
- **Decisions only you can make, as one short card.** Threads can escalate a production deploy,
  a client commitment, scope, money or access. You answer "T435 B" from your phone.
- **Replies where the question came from.** WhatsApp, Teams as a bot, or a draft to you.
  Email is read-only: Toucan never sends email.
- **Safe sends.** Each reply is written to an outbox before it goes out. An interrupted send is
  marked uncertain and never resent on its own.
- **Threads that keep going.** Toucan hands a thread over to a fresh one when its context gets
  large, and to a fallback thread on the other provider while one is rate-limited.
- **Small and open.** The router is standard-library Python with no dependencies. MIT licensed.

## Quick start

You need Linux with systemd user services, Python 3.11 or newer, [uv](https://docs.astral.sh/uv/)
or pipx, and [T3 Code](https://github.com/pingdotgg/t3code) running on the same machine with
Claude Code or Codex signed in.

An agent sets Toucan up, not you. Paste this into Claude Code or Codex on that machine:

```text
Install toucan-router with `uv tool install toucan-router`, then run `toucan guide` and follow
it to set Toucan up for me. Ask me before adding anyone or turning anything on.
```

The agent asks for your name, timezone and number, discovers your chats, adds the people and
routes you pick, wires each owner thread to T3, and turns on the timer. `toucan doctor` shows
what's configured and what's left at any point.

## How much you need to set up

Most people stop after the first or second tier.

| Tier | Adds | Outside accounts | Background services |
|---|---|---|---|
| 1 | T3 Code + WhatsApp | a dedicated phone number | `wacli sync`, `toucan-sync` timer |
| 2 | + email (read-only) | none new | your mail sync |
| 3 | + Teams | one Azure Bot, approved once by a Teams admin, and a public HTTPS URL | `toucan-bot` |

## What Toucan is not

Toucan is not an agent harness, a hosted service or a chatbot. It has no system prompt, no model
tools and no agent loop. It is a command-line tool and a timer on your own machine. The
[FAQ](docs/faq.md) explains the difference and compares Toucan with OpenClaw, NanoClaw and
others. There are longer comparisons with [Hermes Agent](docs/hermes-comparison.md) and
[opencode](docs/opencode-comparison.md).

## Documentation

Setup (also inside the package, `toucan guide --list`):

- [T3 owner threads, webhook tasks and hook files](docs/setup-t3.md)
- [WhatsApp through wacli](docs/setup-whatsapp.md)
- [Teams through the Toucan bot](docs/setup-teams.md)
- [Read-only email through notmuch](docs/setup-email.md)
- [Proof-video uploads, and Loom](docs/setup-upload.md)
- [Google Sheets](docs/setup-sheets.md)
- [Alerts through ntfy or any URL](docs/setup-alerts.md)

Running it:

- [Configuration: every key](docs/configuration.md)
- [Operations: the request lifecycle, rotation, escalations, recovery](docs/operations.md)
- [Telemetry: the one anonymous event a day, and how to turn it off](docs/telemetry.md)
- [Architecture: how the pieces fit](docs/architecture.md)

`toucan guide` prints [AGENTS.md](AGENTS.md), the instructions agents follow.

## Packages

| Package | Commands | What it's for |
|---|---|---|
| `toucan-router` | `toucan` | the router. Standard library only. |
| `toucan-teams-bot` | `toucan-bot` | Teams: receives chat messages and posts replies. Needed for any Teams route. |
| `toucan-router-tools` | `toucan-upload-loom`, `toucan-sheets` | optional tools for agents: Loom uploads ([docs/setup-upload.md](docs/setup-upload.md)) and Google Sheets ([docs/setup-sheets.md](docs/setup-sheets.md)). The router doesn't need them. |

## Status

Version 0.3. T3 Code is the only supported agent host. Toucan reads some of T3's internal
tables for optional features (rotation, rate-limit fallback, `resume`). When a T3 update
changes them, those features turn off, `toucan doctor` warns, and wakes keep working.

WhatsApp support uses wacli, an unofficial client. Use a dedicated number: WhatsApp can ban
numbers that use unofficial clients. The Loom uploader in `toucan-router-tools` also uses Loom's
unofficial API and is marked experimental. Read [SECURITY.md](SECURITY.md) before routing chats
from people you don't fully trust: their messages reach agents that can run commands.

Toucan sends one anonymous usage event a day (version and counts, never chat content). Turn it
off with `TOUCAN_TELEMETRY=0` or `toucan config set settings.telemetry false`; see
[docs/telemetry.md](docs/telemetry.md).

## Contributing

Bug reports, fixes and small improvements are welcome. Open an issue before a larger change.

- Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup, rules and test data.
- [docs/architecture.md](docs/architecture.md) explains how the router, the sources and T3 fit
  together, and which rules keep them safe.
- Security problems go through GitHub's "Report a vulnerability" button. See [SECURITY.md](SECURITY.md).

```sh
uv venv && uv pip install -e . pytest && .venv/bin/python -m pytest -q
(cd bot && uv venv && uv pip install -e '.[test]' && .venv/bin/python -m pytest -q)
(cd tools && uv venv && uv pip install -e . pytest && .venv/bin/python -m pytest -q)
```

## License

[MIT](LICENSE)
