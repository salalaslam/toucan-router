# Contributing to Toucan Router

Bug reports, fixes and small improvements are welcome. For a larger change, open an issue
first so we can agree on the approach before you write the code.

Security problems go through GitHub's "Report a vulnerability" button, not a public issue.
See [SECURITY.md](SECURITY.md).

## Set up

You need Linux, Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/).

```sh
uv venv && uv pip install -e . pytest
.venv/bin/python -m pytest -q
```

The repo holds three packages, and each has its own tests:

| Folder | Package | Tests |
|---|---|---|
| `.` | `toucan-router`, the router | `.venv/bin/python -m pytest -q` |
| `bot/` | `toucan-teams-bot`, the Teams bot | `uv venv && uv pip install -e '.[test]' && .venv/bin/python -m pytest -q` |
| `tools/` | `toucan-router-tools`, Loom and Sheets helpers | same as `bot/` |

CI runs all three on Python 3.11, 3.12 and 3.13.

Install the pre-commit hooks so the leak checks run before every commit:

```sh
pip install pre-commit && pre-commit install
```

## Rules for code changes

- The router and `toucan-router-tools` use only the Python standard library. The bot is the
  only package with dependencies.
- The router never imports the bot or the tools. It reads the bot's SQLite store as a file and
  runs uploaders as commands.
- Every CLI command supports `--json`. Exit codes are 0 for success, 1 for an operational
  failure, 2 for a validation or usage error, and 3 for a refusal.
- People are identified by WhatsApp JID, Entra object ID or email address, never by display name.
- Add or update tests with every behaviour change.

[AGENTS.md](AGENTS.md#working-on-the-code) has the full layout and the bot's storage rules.

## Test data

Use fictional data only, in tests, examples, docs and issues:

- people `alice`, `bob` and `carol`
- threads `project-a` and `project-b`
- the `example.com` domain
- phone numbers starting with `1555`
- all-zero GUIDs

CI fails a pull request that contains real-looking personal data or secrets
(`scripts/check-generic-pii` and gitleaks). Never paste hook URLs, bot secrets, tokens or real
chat messages into code, issues or pull requests.

## Pull requests

- Keep each pull request to one change, and say in the description why it's needed.
- Link the issue it fixes with `Closes #N`.
- Make sure the tests pass for every package you touched.
- Update the README, `docs/` or `toucan guide` text when you change how something is set up or used.
