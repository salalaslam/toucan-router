# Usage data

Toucan Router can send one anonymous event a day so its maintainers can count how many installs
are in use. It sends counts and version numbers, never anything from your chats or your config.
It sends nothing until you say yes.

## Asking

Toucan Router is new, and this count is the only way its maintainers learn whether anyone uses
it. Until you answer, Toucan asks in two places:

- `toucan doctor` lists a `telemetry` item as left to do. In JSON the item carries an `ask` field
  with the question and the command for each answer. The agent setting Toucan up puts the
  question to you and runs the command for your answer. It must not answer for you.
- The daily list on WhatsApp ends with one line asking the same question. This reaches installs
  upgraded from an earlier version, where nobody runs `doctor`.

The question, word for word:

> Toucan Router is new, and its maintainers can't tell whether anyone uses it. May it send one anonymous event a day so they can count active installs? The event holds the Toucan, Python and OS versions and counts of routes, threads, people and requests. It never holds messages, names, phone numbers, IDs or config values. The count decides whether the maintainers keep working on Toucan Router and what they build next.

| Answer | Command |
|---|---|
| Yes, share counts | `toucan telemetry on` |
| No | `toucan telemetry off` |

Either command records your answer (`settings.telemetry` and `settings.telemetry_asked`), and
Toucan stops asking. `toucan init --telemetry on` (or `off`) records it during setup.
A `telemetry = false` set before Toucan started asking doesn't count as an answer, because
installing agents often set it without asking you, so Toucan asks once more after the upgrade.

## What is sent

After you say yes, once every 24 hours the first `toucan` command that opens the state database (usually the
`toucan-sync` timer) sends one `router.active` event to PostHog with these fields:

| Field | Example | Meaning |
|---|---|---|
| `distinct_id` | 64 hex characters | SHA-256 of a random install ID |
| `version` | `0.3.0` | the `toucan-router` package version |
| `python` | `3.13.1` | Python version |
| `os` | `linux` | operating system name |
| `routes`, `threads`, `people` | `4`, `2`, `5` | how many of each the config has |
| `router_off`, `router_shadow`, `router_live` | `2`, `1`, `1` | routes in each router mode |
| `sources` | `["teams", "whatsapp"]` | which sources the routes use |
| `requests_24h` | `12` | requests created in the last 24 hours |

The install ID is random. Toucan creates it the first time it sends, stores it in
`telemetry.json` next to the state database (mode 600), and prints a one-line notice to stderr.
No account, phone number, Teams ID, email address, chat name, JID, thread ID, project name,
message text or config value is sent, hashed or not.

The event asks PostHog not to create a person profile and not to look up a location from the IP
address. PostHog still receives the IP address of the request, as any web service does.

A failed send never fails a command. Sends time out after 3 seconds, and after a failure Toucan
waits an hour before it tries again. `--dry-run` never sends.

## Turning it off

Any of these turns it off at any time:

```sh
toucan telemetry off
```

- `TOUCAN_TELEMETRY=0` in the environment of the `toucan-sync` service and your shell.
- `DO_NOT_TRACK=1`.

With either variable set, Toucan also stops asking. `toucan doctor` shows whether it is on.
