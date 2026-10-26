# Toucan Router

Toucan Router connects WhatsApp, Microsoft Teams and email to coding agents running in
[T3 Code](https://github.com/pingdotgg/t3code). When an approved person messages a chat
you've routed, Toucan wakes the T3 thread that owns that chat. It then tracks the request
until the thread replies, promises a follow-up, escalates to you or closes it.

It is a small, single-operator tool for people who already run Claude Code or Codex
threads in T3 Code and want colleagues to be able to ask those threads for work.

```
WhatsApp (wacli) ─┐                       ┌─> T3 thread "project-a" ─┐
Teams (bot)     ──┼─> toucan sync ─ wake ─┤                          ├─> toucan reply / promise / escalate
Email (notmuch) ──┘    (allowlist)        └─> T3 thread "project-b" ─┘        │
                                                                              v
                                    WhatsApp, Teams (as the bot), or a draft to you
```

What it does:

- Reads approved chats only. People are matched by WhatsApp JID, Entra ID or email address,
  never by display name.
- Wakes a thread through its T3 webhook. The wake carries counts and times, never message text.
- Tracks every request: `claim`, `reply --final`, `reply --promise WHAT --due 3h`, `done`,
  `close`, `escalate`, `resume`. Overdue promises and unclaimed requests send an alert.
- Sends replies to WhatsApp, posts them in Teams as a bot, or drafts them to you.
  Email is read-only: Toucan never sends email.
- Uploads proof videos with `toucan upload`, through any uploader command you configure.
- Rotates T3 threads whose context gets large, and hands requests to a fallback thread on
  another provider while one is rate-limited.

## Requirements

- Linux with systemd user services, Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/)
  or pipx.
- [T3 Code](https://github.com/pingdotgg/t3code) running on the same machine.
- At least one source: [wacli](docs/setup-whatsapp.md) for WhatsApp,
  the [Toucan Teams bot](docs/setup-teams.md) for Teams, or [notmuch](docs/setup-email.md) for email.

## Quick start

Toucan is set up by an agent, not by hand. Paste this into Claude Code or Codex on the machine
that runs T3 Code:

```text
Install toucan-router with `uv tool install toucan-router`, then run `toucan guide` and follow
it to set Toucan up for me. Ask me before adding anyone or turning anything on.
```

The agent installs the package, asks for your name, timezone and number, discovers your chats,
adds the people and routes you pick, wires each owner thread to T3, and turns on the timer.
`toucan doctor` shows what's configured and what's left at any point.

The guides are in [docs/](docs/) and inside the package (`toucan guide --list`):

| Topic | Covers |
|---|---|
| [setup-t3](docs/setup-t3.md) | owner threads, webhook tasks, hook files |
| [setup-whatsapp](docs/setup-whatsapp.md) | wacli pairing and sync |
| [setup-teams](docs/setup-teams.md) | the Teams bot: registration, approval, routing |
| [setup-email](docs/setup-email.md) | read-only email through notmuch |
| [setup-upload](docs/setup-upload.md) | the upload hook, and Loom through toucan-router-tools |
| [setup-sheets](docs/setup-sheets.md) | Google Sheets through toucan-router-tools |
| [setup-alerts](docs/setup-alerts.md) | ntfy or any alert URL |
| [configuration](docs/configuration.md) | every config key |
| [operations](docs/operations.md) | the request lifecycle, rotation, escalations, recovery |

`toucan guide` prints [AGENTS.md](AGENTS.md), the instructions agents follow.

## How much you need to set up

Most people stop after the first or second tier.

| Tier | Adds | Outside accounts | Background services |
|---|---|---|---|
| 1 | T3 Code + WhatsApp | a dedicated phone number | `wacli sync`, `toucan-sync` timer |
| 2 | + email (read-only) | none new | your mail sync |
| 3 | + Teams | one Azure Bot, approved once by a Teams admin, and a public HTTPS URL | `toucan-bot` |

## Packages

| Package | Commands | What it's for |
|---|---|---|
| `toucan-router` | `toucan` | the router. Standard library only. |
| `toucan-teams-bot` | `toucan-bot` | Teams: receives chat messages and posts replies. Needed for any Teams route. |
| `toucan-router-tools` | `toucan-upload-loom`, `toucan-sheets` | optional tools for agents: Loom uploads ([docs/setup-upload.md](docs/setup-upload.md)) and Google Sheets ([docs/setup-sheets.md](docs/setup-sheets.md)). The router doesn't need them. |

## Status

Version 0.1. T3 Code is the only supported agent host. Toucan reads some of T3's internal
tables for optional features (rotation, rate-limit fallback, `resume`). When a T3 update
changes them, those features turn off, `toucan doctor` warns, and wakes keep working.

WhatsApp support uses wacli, an unofficial client. Use a dedicated number: WhatsApp can ban
numbers that use unofficial clients. The Loom uploader in `toucan-router-tools` also uses Loom's
unofficial API and is marked experimental. Read [SECURITY.md](SECURITY.md) before routing chats from people you
don't fully trust: their messages reach agents that can run commands.

## Development

```sh
uv venv && uv pip install -e . pytest && .venv/bin/python -m pytest -q
(cd bot && uv venv && uv pip install -e '.[test]' && .venv/bin/python -m pytest -q)
(cd tools && uv venv && uv pip install -e . pytest && .venv/bin/python -m pytest -q)
```

MIT licensed. See [LICENSE](LICENSE).
