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

## Your own number

`settings.owner_whatsapp` is your personal number's JID. Drafts and escalations go there, and
your answers to escalations come back from that chat. To give threads work yourself, add
yourself to `[people]` and route your 1:1 with the Toucan number to a thread.

## Voice notes

Set `settings.transcribe` to a command that takes an audio file path and prints the
transcript, for example a Whisper wrapper. Toucan downloads voice notes with
`wacli --read-only media download` and puts the transcript in the request.
