# Setting up Google Sheets (optional)

Threads sometimes need to hand someone a spreadsheet: a test report, a list of open issues, an
estimate. `toucan-sheets` creates and edits Google Sheets from the command line and prints
JSON, so an agent can create a sheet and put its link in a reply.

It comes in the optional `toucan-tools` package (`uv tool install toucan-tools`). The router
doesn't use it; it's a tool for agents. It uses only the `drive.file` scope. With that scope it can create
spreadsheets, and edit the ones it created or that you opened with it. It can't see the rest
of your Drive.

`toucan-sheets status` tells you whether it's signed in.

## 1. Create a Google Cloud OAuth client

Toucan can't ship a Google client for everyone, so you create one. It's free. In a Google
Workspace organisation one client can serve the whole team: set its audience to Internal, share
`client.json`, and have each person sign in with their own account. Never share `token.json`.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project.
2. Enable the **Google Sheets API** and the **Google Drive API** for it.
3. Set up the OAuth consent screen (Google Auth Platform, Branding and Audience):
   - Audience: Internal if you use Google Workspace and only your organisation needs it,
     otherwise External.
   - For External apps, publish the app ("In production"). Apps left in testing get refresh
     tokens that expire after seven days. Google asks for a homepage and a privacy policy URL
     to publish; a short page on GitHub Pages is enough.
   - Data access: add only `https://www.googleapis.com/auth/drive.file`.
4. Create an OAuth client ID of type **Desktop app** and download its JSON.
5. Save it where `toucan-sheets` looks for it:

```sh
mkdir -p ~/.config/toucan/sheets && chmod 700 ~/.config/toucan/sheets
install -m 600 ~/Downloads/client_secret_*.json ~/.config/toucan/sheets/client.json
```

`TOUCAN_SHEETS_DIR` moves this folder somewhere else.

## 2. Sign in

```sh
toucan-sheets auth --account you@example.com
```

It prints a Google sign-in URL and waits up to 15 minutes for Google to redirect to
`http://127.0.0.1:8765`. `--account` makes sure you sign in with the right Google account; it
refuses to save a token for any other.

On a headless server, forward the callback port from the computer that has the browser:

```sh
ssh -L 127.0.0.1:8765:127.0.0.1:8765 your-server toucan-sheets auth --account you@example.com
```

Open the URL on your computer. The redirect travels back through the SSH tunnel, and the token
is stored only on the server, in `~/.config/toucan/sheets/token.json` with mode 600.
`toucan-sheets` refreshes it on its own before each command.

`auth` refuses to replace an existing token. Run `toucan-sheets status` first; use
`auth --force` only if status fails and you mean to sign in again.

## 3. Use it

```sh
toucan-sheets status
toucan-sheets create 'Release checklist'
toucan-sheets create 'Release checklist' --values rows.json
toucan-sheets read SPREADSHEET_ID 'Sheet1!A1:C20'
toucan-sheets write SPREADSHEET_ID 'Sheet1!A1' rows.json
toucan-sheets write SPREADSHEET_ID 'Sheet1!C2' formulas.json --input-option USER_ENTERED
toucan-sheets batch-update SPREADSHEET_ID formatting.json
```

- `rows.json` is an array of rows, such as `[["Task", "Status"], ["Draft", "Ready"]]`. Use `-`
  to read it from stdin.
- `batch-update` takes a complete Sheets `spreadsheets.batchUpdate` body, for formatting, tabs,
  charts and other edits.
- `create` returns the spreadsheet ID and URL. If writing the initial values fails, the error
  output still includes the new spreadsheet's ID, so you can fix it without creating a duplicate.
- New spreadsheets are private to your account. Share them from Google Sheets as usual.

You can revoke access at any time from your Google account's third-party connections page.
