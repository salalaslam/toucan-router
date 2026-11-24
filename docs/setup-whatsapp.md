# Setting up WhatsApp

Toucan reads WhatsApp through [wacli](https://wacli.sh), a command-line WhatsApp client that
keeps a local copy of your chats. Toucan reads wacli's store read-only and sends replies with
`wacli send`.

wacli is an unofficial client (it links to WhatsApp as a companion device, like WhatsApp Web).
WhatsApp can restrict or ban numbers that use unofficial clients. Use a dedicated number for
Toucan, not your personal one, and keep `rate_per_hour` low.

`toucan doctor` checks `whatsapp.command` (wacli on PATH) and `whatsapp.store` (store readable)
whenever Toucan may send on WhatsApp. That includes drafts and escalations to you, so Teams-only
and email-only setups need wacli too. `whatsapp.fresh` (synced recently) runs only when a
route uses `source = "whatsapp"`.

## 1. Install and pair

Install wacli following its docs, then pair it with the dedicated number:

```sh
wacli auth            # shows a QR code; scan it from WhatsApp > Linked devices on the phone
wacli auth status
```

The phone must stay online now and then for the link to keep working.

## 2. Keep it syncing

Run `wacli sync` as a user service so new messages reach the store:

```ini
# ~/.config/systemd/user/wacli-sync.service
[Unit]
Description=wacli sync
After=network-online.target

[Service]
ExecStart=%h/.local/bin/wacli sync --follow --presence-mode quiet --max-reconnect 0 --refresh-contacts --refresh-groups
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload && systemctl --user enable --now wacli-sync.service
```

Tell Toucan where things are:

```toml
[sources.whatsapp]
store = "~/.local/state/wacli"       # wacli's store folder (default)
account = "wa"                       # label stored with each message
service_unit = "wacli-sync.service"  # optional; doctor checks it is active
```

`settings.whatsapp_stale_minutes` sets when the store counts as stale. wacli only writes when
messages arrive, so on a quiet number set it to several hours.

## 3. Find JIDs

People and chats are identified by JID, never by name:

- a person: `15550000001@s.whatsapp.net` (their number) and, in newer chats, a `...@lid` alias.
  List both in `whatsapp = [...]`.
- a group: `120363000000000000@g.us`.

```sh
wacli --read-only --json chats list
wacli --read-only --json messages list --chat JID --limit 5
```

## 4. Route a chat

```sh
toucan discover whatsapp --json
toucan add person alice --name 'Alice Example' --whatsapp 15550000001@s.whatsapp.net --whatsapp 100000000000001@lid
toucan add route alice-whatsapp --source whatsapp --chat 15550000001@s.whatsapp.net \
  --chat 100000000000001@lid --thread project-a --requester alice --mode draft
```

That writes this config:

```toml
[people.alice]
name = "Alice Example"
whatsapp = ["15550000001@s.whatsapp.net", "100000000000001@lid"]

[[routes]]
name = "alice-whatsapp"
source = "whatsapp"
chats = ["15550000001@s.whatsapp.net", "100000000000001@lid"]
thread = "project-a"
mode = "draft"              # "act" sends replies straight to the chat
requesters = ["alice"]
active_from = "2026-01-01T00:00:00+00:00"
```

For a group that includes a client, `reply_to = "alice"` sends every reply to Alice's own
WhatsApp instead of the group, and `reply_prefix` adds a line at the top of each reply.

## Quoted messages

When someone replies to an earlier message, the inbox shows that message below theirs as
`Quote (from NAME):` lines, with its text and any attachment's local path, however old it is.
A quoted message wacli never stored is named by its ID instead. Quoted text is context: the
router reads only the sender's own words for project names.

`quote_replies = true` on a WhatsApp route sends each reply to a request as a WhatsApp reply
that quotes the request's message (`wacli send --reply-to`), so the person sees which message
it answers. It applies only when the reply goes to the chat the message came from, and only
when wacli has the message's content to quote. For a text reply a 1:1 chat's phone and `@lid`
JIDs count as one chat; a file reply quotes only when it goes to the exact JID the message is
stored under. If wacli refuses the quote anyway, it does so before sending (for a file, after
uploading it), and Toucan sends the reply unquoted and records a `quote_dropped` event. It is
off by default.

```sh
toucan config set routes.alice-whatsapp.quote_replies true --json
```

## Photo bursts

People often send a few photos right after a message ("add this one", then three photos). By
default each photo is its own request. Set `photo_burst_seconds` on the route to fold them
into one:

```sh
toucan config set routes.alice-whatsapp.photo_burst_seconds 30
```

(`toucan add route` takes `--photo-burst-seconds 30` for a new route.)

A photo then joins the request of the same person's message just before it in the chat when:

- it has no caption and doesn't quote another message (any text makes its own request),
- it came at most `photo_burst_seconds` after that message (each photo of an album counts from
  the one before it),
- that request is still `new`: not claimed, not held by the live router, and not split across
  threads, and
- that message isn't a voice note still waiting for its transcript or an answer to a decision
  card.

Otherwise the photo becomes its own request, as before. Every message is kept. The inbox lists
the photos under the request, after its message, marked as part of it. A photo that joins a
request the thread has already read shows up in `toucan wait` like an edit; it doesn't add a
wake. `photo_burst_seconds` must be shorter than `settings.wake_gap_minutes`; 30 suits most
chats. `toucan sync --dry-run --json` lists the photos it would merge under each route's
`merged`.

## Stage reactions

`stage_reactions = true` on a WhatsApp `act` route makes Toucan react to each request message
so the person sees it arrived and when it is done, the same way it does on Teams routes with
`post_as_bot`. It is off by default.

```sh
toucan config set routes.alice-whatsapp.stage_reactions true --json
toucan config set settings.done_emoji check --json   # 👀 when it arrives, ✅ after the final reply
```

The stages come from the `ack_emoji`, `work_emoji` and `done_emoji` settings, shared with
Teams. The default `ack_emoji = "eyes"` shows 👀 on arrival. Leave `work_emoji` empty to keep
👀 while a thread works on it, so the person sees only received and done. Set it (for example
to `eye`) for a separate working stage. With `done_emoji` empty, 👀 stays after the reply. A
request closed without a reply loses its reaction.

- When someone sends several messages before Toucan answers, only the newest carries the
  reaction, at the stage of the least advanced one. With `photo_burst_seconds`, a photo that
  joined a request is one of its messages, so an album's newest photo carries it.
- WhatsApp keeps one reaction per sender on a message, so a new stage replaces the old one.
  Toucan sends the reaction with `wacli send react --sender`, naming who wrote the message (without it
  WhatsApp takes the target for one of Toucan's own messages and shows nothing), and removes
  it with an empty reaction.
- Reactions go out during `toucan sync`, at most four calls a sync, never in a dry run. A
  failed or timed-out call is retried on the next syncs, three times per stage, then `toucan
  status` shows an `ack:` problem. Replies never wait for a reaction.
- Messages older than two hours when Toucan first sees them get no reaction (a backlog after
  downtime).
- Not with `reply_to`: a reaction would show in the group that `reply_to` keeps Toucan out of.

## Your own number

`settings.owner_whatsapp` is your personal number's JID. Drafts and escalations go there, and
your answers to escalations come back from that chat. To give threads work yourself, add
yourself to `[people]` and route your 1:1 with the Toucan number to a thread.

## Voice notes

Set `settings.transcribe` to a command that takes an audio file path and prints the
transcript, for example a Whisper wrapper. Toucan downloads voice notes with
`wacli --read-only media download` and puts the transcript in the request.
