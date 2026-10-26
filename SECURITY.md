# Security

Toucan Router lets people message an AI agent that can run commands on your machine. Read
this before you route anyone's chats to a thread.

## The trust model

- **The requester allowlist is the only boundary.** Only messages from people listed in a
  route's `requesters` become requests. Identities are WhatsApp JIDs, Entra object IDs and
  email addresses, never display names. Anyone you add can ask your agent to do things.
- **Chat content is untrusted.** The inbox wraps every message in an `UNTRUSTED CONTENT`
  block, and the task prompt tells the thread not to take instructions about permissions,
  credentials or other chats from it. That reduces prompt injection. It doesn't prevent it.
  Give owner threads the narrowest permissions that let them do the work, and escalate
  anything involving production, money or credentials.
- **Toucan is single-operator.** Claim IDs coordinate trusted agents on one machine. They
  are not authentication. Anyone who can run `toucan` as your user can reply as Toucan.
- **Email is read-only.** Forged sender addresses are easy, so an email route's replies always
  come to you as drafts. Don't give email requesters more trust than that.

## Secrets and private data

| Item | Where | Protection |
|---|---|---|
| T3 webhook URLs (contain a token) | `~/.config/toucan/hooks/*.hook` | mode 600; `doctor` warns otherwise |
| Alert URL (may be a secret ntfy topic) | `alert_url_file` | mode 600 |
| Loom token (toucan-router-tools) | `~/.config/toucan/loom/` | folder 700, files 600 |
| Google OAuth client and token (toucan-router-tools) | `~/.config/toucan/sheets/` | folder 700, files 600; `drive.file` scope only |
| Teams messages received by the bot | `~/.local/state/toucan-bot/bot.db` | private messages from every chat the bot is in |
| Teams inline screenshots | `~/.local/state/toucan-bot/media/` | folder 700, files 600; 10 MiB cap and 30-day retention by default |
| Teams bot app ID and secret | `~/.config/toucan-bot/credentials.json` | mode 600; entered once through the setup page |
| Routed messages, requests, log | `~/.local/state/toucan/` | folder 700, database 600 |

Toucan never puts message text in wakes or alerts. `check-config` and `doctor` hide IDs unless
you pass `--show-ids`.

## Third-party risks

- **WhatsApp:** wacli is an unofficial client. WhatsApp can restrict or ban numbers that use it.
  Use a dedicated number.
- **Loom:** the uploader in `toucan-router-tools` uses Loom's unofficial upload API. It can break, and Loom
  could object to it being used this way.
- **Teams:** the bot stores every message in the chats it's added to, not only the ones
  that mention it, including messages from people Toucan doesn't route, and their edits,
  deletions and restorations. It recovers missed history through Graph using its own application
  credentials and per-chat RSC consent. The first read covers messages modified in the last
  seven days by default, including older messages edited recently.
  Application senders stay context only. It stores inline screenshots privately and keeps
  shared-file names and links, but never downloads file attachments. Opening those links
  needs the owner's own Microsoft login. Downloaded images and file links are untrusted.
  Image downloads have a byte cap and reject redirects and encoded path traversal.
  Graph endpoints are rebuilt from validated chat, message and hosted-content IDs and must
  match the message being stored. Connector endpoints use validated opaque attachment IDs
  from signed activity on supported Microsoft image hosts, bound to the activity's message
  in cache and retry records. Regional service prefixes are validated before URL construction.
  An image 401 retains pending work; due retries renew the appropriate token using the stored
  bot credentials with bounded backoff. Retry records contain no tokens or secrets.
  Media jobs have atomic claims with five-minute crash recovery. Only the current claimant
  can publish staged bytes and metadata; stale results cannot overwrite available or expired
  screenshots or newer edits. Expiry and publication share a write lock. Message merges and
  queue reconciliation also use write transactions to preserve concurrent changes.
  Message writes and image jobs commit together. Image-directory and publication failures
  leave retryable jobs without blocking message recovery. Connector and Graph image retries
  run separately from catch-up. Local expiry continues without credentials while the server runs.
  Expiry runs in an owned worker with a separate database connection and batches of at most
  32 files or messages per transaction. It revalidates candidates under the publication lock.
  Background failures roll back and retry with asynchronous backoff; persisted health and
  `/healthz` report error classes without exception text or credentials.
  Person-name sends require a confirmed roster with exactly the owner and that person;
  group aliases cannot override their human 1:1. Per-chat generations discard superseded
  roster responses and withdraw person aliases until the current fetch succeeds. Name readers
  read members, confirmation and generation in one snapshot. Router message/quote reads and
  bot health counts also use consistent snapshots. Mutation timestamps never establish
  creation or request eligibility. Graph recovery can promote incomplete observations only
  after checking the original seed cutoff and active_from, once per message.
  Router-owned generation and change-sequence pairs discover inserts and content or visibility
  changes outside the creation-time lookback. Store replacement, rebuild or source sequence
  regression after restoring a backup forces a rescan;
  message-key deduplication and eligibility cutoffs still apply. Restoration advances the
  same sequence, including for tombstones the router has never stored.
  Expiry, catch-up, Connector and Graph image retries, and roster refresh run independently so a persistent
  failure in one does not block the others. The privacy page explains that the first Graph
  window filters modification time and can retain much older messages edited recently.
  A message-list 403 means a missing RSC grant or unsupported chat type; image access can
  return 403 independently. Upgrades need consent in each chat and tenant policy can block
  it; administrators check `Get-MgBetaChatRscConfiguration`. The bot's privacy page says so. Tell the people in those chats. The bot's setup page is reachable from the internet. Registration needs the
  private setup link that `toucan-bot setup-url` prints. The server accepts only
  Microsoft-signed activity from your configured tenant.
- **Model providers:** the content of routed messages goes to whichever model your T3 threads
  use. Make sure the people you route are fine with that.

## Reporting a vulnerability

Please report security problems privately through GitHub's "Report a vulnerability" button on
this repository rather than in a public issue.
