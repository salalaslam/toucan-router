# Usage data

Toucan Router sends one anonymous event a day so the project can count how many installs are in
use. It sends counts and version numbers, never anything from your chats or your config.

## What is sent

Once every 24 hours, the first `toucan` command that opens the state database (usually the
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

Any of these turns it off:

```sh
toucan config set settings.telemetry false
```

- `TOUCAN_TELEMETRY=0` in the environment of the `toucan-sync` service and your shell.
- `DO_NOT_TRACK=1`.

`toucan doctor` shows whether it is on.
