# Setting up email (read-only)

Toucan can turn emails from approved senders into requests. It reads mail through
[notmuch](https://notmuchmail.org/) and never sends, moves or tags email. A reply to an email
request always goes to you as a draft on WhatsApp, and you send it from your own mail client.

`toucan doctor` checks `email.command` (notmuch on PATH) and `email.fresh` (the index has mail
newer than `stale_minutes`) when a route uses `source = "email"`.

## 1. Get mail into a notmuch index

Toucan doesn't fetch mail. Use whatever you like to sync a mailbox to a local Maildir, then
index it with notmuch. Common setups:

- Gmail: [lieer](https://github.com/gauteh/lieer) (`gmi sync`) syncs Gmail with labels.
- IMAP: [mbsync](https://isync.sourceforge.io/) or offlineimap.
- Microsoft 365 with OAuth only: put [email-oauth2-proxy](https://github.com/simonrob/email-oauth2-proxy)
  in front of mbsync.

Run the sync and `notmuch new` on a timer (every five minutes is plenty), then check:

```sh
notmuch count '*'
notmuch search --limit 3 date:1d..
```

## 2. Configure

```sh
toucan discover email --json
toucan add person carol --name 'Carol Example' --email carol@example.com
toucan add route support-email --source email --chat you@example.com --thread project-a \
  --requester carol --mode draft
```

That writes this config:

```toml
[sources.email]
# notmuch_config = "~/.notmuch-config"   # only if notmuch needs a non-default config
stale_minutes = 60

[people.carol]
name = "Carol Example"
email = ["carol@example.com"]

[[routes]]
name = "support-email"
source = "email"
chats = ["you@example.com"]      # your address(es) the mail was sent to (To, Cc or Delivered-To)
thread = "project-a"
mode = "draft"                   # required: email routes are read-only
requesters = ["carol"]           # only mail From these people becomes a request
active_from = "2026-01-01T00:00:00+00:00"
```

Rules for email routes:

- `mode` must be `draft`. Validation fails with `act`.
- `post_as_bot`, `owner_reply_closes`, `reply_to` and `reply_via_whatsapp` are not allowed.
- Mail from anyone not listed in `requesters` is ignored. Addresses compare case-insensitively.
- Each sync re-reads the last 48 hours of mail (`toucan sync --lookback-hours`), so mail
  that reaches the index late still becomes a request. Toucan tracks Message-IDs, so the same
  email sent to two of your addresses becomes one request.
- The request text is the subject and the plain-text body (or the HTML body converted to
  text). Attachments are not passed on.

## 3. Check

```sh
toucan doctor
toucan sync --dry-run
```
