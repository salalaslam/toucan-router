# Setting up video uploads

Threads often need to share a proof video: a screen recording or a rendered walkthrough
showing that a fix works. `toucan upload` sends a file to a video host and prints a share
link the thread can put in its reply.

```sh
toucan upload /path/to/proof.mp4 --title 'Login fix, before and after'
toucan upload /path/to/proof.mp4 --title '...' --json    # {"url": "...", "id": "..."}
```

Toucan runs the command in `settings.upload_command`. The default is `toucan-upload-loom`,
from the optional `toucan-tools` package (`uv tool install toucan-tools`).

Uploads are optional. Until `toucan-tools` is installed, `toucan doctor` lists `upload.command`
as skipped. Once it is, `upload.login` is skipped until you sign in, and ok after. If you set your own
`upload_command` and it isn't on PATH, `upload.command` is a todo.

## Loom (default, experimental)

```sh
toucan-upload-loom login     # prints a URL; open it, sign in to Loom and approve access
toucan-upload-loom status    # {"logged_in": true, ...}
```

The login uses Atlassian's device sign-in. Toucan stores the token in `~/.config/toucan/loom/`
(folder mode 700, files mode 600) and refreshes it before each upload. Run
`toucan-upload-loom login --force` if the sign-in is revoked or stops refreshing.

Loom has no public upload API. This uploader uses the same sign-in client and upload calls as
Atlassian's own command-line tool. It may break when Loom changes them, and Loom could object
to it being used this way. If that happens, switch `upload_command` to another uploader.
Uploaded videos keep your workspace's default sharing settings. The uploader doesn't change
permissions or notify anyone. Loom may still be processing a video when the link comes back.

`TOUCAN_LOOM_CLIENT_ID` overrides the OAuth client ID if you have registered your own.

## Any other host

Set `upload_command` to any program that follows this contract:

```text
argv:    <upload_command...> upload FILE --title TITLE
stdout:  {"url": "https://...", "id": "..."}      (JSON, one object)
exit:    0 on success; anything else is a failure, and stderr explains it
```

```toml
[settings]
upload_command = "my-uploader --profile work"   # split like a shell would, but run without one
```

Toucan waits up to 30 minutes. Keep credentials in the uploader's own config, not in its
arguments.

Hosts people commonly use for this, for which you'd write or install your own uploader:
YouTube (unlisted), Vimeo, Wistia, Vidyard, Mux, Cloudflare Stream, Bunny Stream, Streamable,
Google Drive, Dropbox, OneDrive or SharePoint, Microsoft Stream, S3-compatible storage with a
presigned link (AWS S3, Cloudflare R2, Backblaze B2, MinIO), GitHub release assets,
Cap, Zight and Tella.
