# Setting up alerts

Toucan sends a short alert when something needs you: a request nobody claimed within
`ack_alert_minutes`, an overdue promise, a failed or uncertain send, a stale source, a thread
stopped by a rate limit, or a stuck thread rotation. Alerts never include message text.

Alerts are optional. Without them, the same problems show in `toucan status`.

## ntfy

[ntfy](https://ntfy.sh) is the simplest option. Pick a topic name nobody can guess, subscribe
to it in the ntfy app, and give Toucan its URL:

```toml
[settings]
alert_url = "https://ntfy.sh/your-unguessable-topic"
```

Anyone who knows a public ntfy topic can read it, so keep the topic name secret. To keep it
out of the config file, put the URL in a mode-600 file instead:

```sh
umask 077 && printf '%s\n' 'https://ntfy.sh/your-unguessable-topic' > ~/.config/toucan/alert-url
```

```toml
[settings]
alert_url_file = "~/.config/toucan/alert-url"
```

A self-hosted ntfy server works the same way (`alert_url = "http://127.0.0.1:8090/toucan"`).

## Other services

Toucan POSTs the alert as a plain-text body to `alert_url`, with a `Title` header. Any
endpoint that accepts that works. That includes ntfy, a Gotify or Apprise bridge, or your own
small webhook that forwards to Slack or Telegram.

`settings.ntfy` from earlier versions still works as an alias for `alert_url`, and
`toucan check-config` warns about it.

`toucan doctor` reports `alerts` as skipped when neither key is set.
