"""Optional Google Sheets companion using a user's desktop OAuth client."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SCOPES = ['https://www.googleapis.com/auth/drive.file']
AUTHORIZE = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN = 'https://oauth2.googleapis.com/token'
SHEETS = 'https://sheets.googleapis.com/v4/spreadsheets'
IDENTITY = 'https://www.googleapis.com/drive/v3/about?fields=user'


class SheetsError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def directory():
    return Path(os.environ.get('TOUCAN_SHEETS_DIR') or '~/.config/toucan/sheets').expanduser()


@contextmanager
def lock():
    path = directory()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    fd = os.open(path / 'token.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        yield


def load_private(name):
    path = directory() / name
    if not path.is_file():
        message = 'No desktop OAuth client. Add client.json before running toucan-sheets auth.' if name == 'client.json' else 'Not authenticated. Run toucan-sheets auth.'
        raise SheetsError(message)
    path.chmod(0o600)
    try:
        data = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise SheetsError(f'Invalid {name}; expected a JSON object.', 2) from None
    if not isinstance(data, dict):
        raise SheetsError(f'Invalid {name}; expected a JSON object.', 2)
    return data


def client():
    data = load_private('client.json').get('installed')
    if not isinstance(data, dict) or not all(isinstance(data.get(key), str) and data[key] for key in ('client_id', 'client_secret')):
        raise SheetsError('client.json must contain an installed desktop OAuth client.', 2)
    return data


def save(record):
    # Callers hold token.lock across the read, refresh/authorization and replace.
    fd, temporary = tempfile.mkstemp(prefix='.token-', dir=directory())
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory() / 'token.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(NoRedirect)


def request(url, method='GET', record=None, body=None, fields=None, sensitive=()):
    headers = {'Accept': 'application/json'}
    hidden = list(sensitive)
    data = None
    if record:
        headers['Authorization'] = 'Bearer ' + record['access_token']
        hidden.extend(record.get(key) for key in ('access_token', 'refresh_token'))
    if fields is not None:
        data = urllib.parse.urlencode(fields).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        hidden.extend(fields.get(key) for key in ('client_secret', 'refresh_token', 'code', 'code_verifier'))
    elif body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with HTTP.open(req, timeout=60) as response:
            raw = response.read()
            result = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        message = 'Google API request failed'
        try:
            result = json.loads(error.read())
            if isinstance(result, dict):
                hidden.extend(result.get(key) for key in ('access_token', 'refresh_token', 'client_secret'))
                detail = result.get('error', {})
                if isinstance(detail, dict):
                    message = detail.get('message', message)
                elif isinstance(detail, str):
                    message = result.get('error_description') or detail
        except (ValueError, OSError):
            pass
        if not isinstance(message, str):
            message = 'Google API request failed'
        for value in sorted((v for v in hidden if isinstance(v, str) and v), key=len, reverse=True):
            for variant in (value, urllib.parse.quote(value, safe=''), urllib.parse.quote_plus(value)):
                message = message.replace(variant, '[redacted]')
        message = re.sub(r'(?i)\b(?:authorization\s*[:=]\s*|bearer\s+)\S+', '[redacted]', message)
        raise SheetsError(f'Google API HTTP {error.code}: {message}') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SheetsError('Google request failed due to a network error.') from None
    except (ValueError, UnicodeError):
        raise SheetsError('Google returned invalid JSON.') from None
    if not isinstance(result, dict):
        raise SheetsError('Google returned an invalid response.')
    return result


def token_record(response, previous=None):
    access = response.get('access_token')
    refresh = response.get('refresh_token') or (previous or {}).get('refresh_token')
    lifetime = response.get('expires_in')
    scopes = response.get('scope', ' '.join(SCOPES))
    if not isinstance(scopes, str) or set(scopes.split()) != set(SCOPES):
        raise SheetsError('Unexpected OAuth scopes; authenticate again with drive.file only.')
    if not isinstance(access, str) or not access or isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or not math.isfinite(lifetime) or lifetime <= 0:
        raise SheetsError('Google returned incomplete credentials.')
    if str(response.get('token_type', '')).lower() != 'bearer':
        raise SheetsError('Google returned an unsupported token type.')
    if not isinstance(refresh, str) or not refresh:
        raise SheetsError('Google did not return an offline refresh token. Run toucan-sheets auth again.')
    return dict(access_token=access, refresh_token=refresh, expires_at=time.time() + lifetime, scopes=SCOPES)


def credentials():
    with lock():
        app = client()
        record = load_private('token.json')
        if record.get('scopes') != SCOPES:
            raise SheetsError('Unexpected OAuth scopes; authenticate again with drive.file only.')
        expiry = record.get('expires_at')
        if not isinstance(record.get('access_token'), str) or not record['access_token'] or isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
            raise SheetsError('Invalid token.json. Run toucan-sheets auth --force.')
        if expiry <= time.time() + 120:
            if not isinstance(record.get('refresh_token'), str) or not record['refresh_token']:
                raise SheetsError('No refresh token. Run toucan-sheets auth --force.')
            response = request(TOKEN, 'POST', fields={
                'grant_type': 'refresh_token', 'client_id': app['client_id'],
                'client_secret': app['client_secret'], 'refresh_token': record['refresh_token'],
            }, sensitive=[record['access_token']])
            record = token_record(response, record)
            save(record)
        return record


def identity(record, sensitive=()):
    data = request(IDENTITY, record=record, sensitive=sensitive)
    user = data.get('user')
    if not isinstance(user, dict) or not isinstance(user.get('emailAddress'), str) or not user['emailAddress']:
        raise SheetsError('Google did not return an account email.')
    return user['emailAddress']


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # The request URL contains the authorization code and must never be logged.
        pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        states = query.get('state', [])
        valid = parsed.path == '/' and len(states) == 1 and hmac.compare_digest(states[0].encode(), self.server.oauth_state.encode())
        codes = query.get('code', [])
        errors = query.get('error', [])
        if not valid or not ((len(codes) == 1 and codes[0] and not errors) or (len(errors) == 1 and errors[0] and not codes)):
            self.send_response(400)
            message = b'Invalid OAuth callback.'
        else:
            self.server.code = codes[0] if codes else None
            self.server.denied = bool(errors)
            self.send_response(200)
            message = b'Toucan authorization received. You may close this tab.'
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(message)))
        self.end_headers()
        self.wfile.write(message)


class CallbackServer(HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(1)
        return connection, address


def auth(port=8765, force=False, account=None):
    if not 1 <= port <= 65535:
        raise SheetsError('Port must be between 1 and 65535.', 2)
    with lock():
        if (directory() / 'token.json').exists() and not force:
            raise SheetsError('A token already exists. Check toucan-sheets status or use auth --force.', 3)
        app = client()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        state = secrets.token_urlsafe(32)
        redirect = f'http://127.0.0.1:{port}/'
        fields = dict(client_id=app['client_id'], redirect_uri=redirect, response_type='code',
                      scope=' '.join(SCOPES), state=state, code_challenge=challenge,
                      code_challenge_method='S256', access_type='offline', prompt='consent')
        if account:
            fields['login_hint'] = account
        with CallbackServer(('127.0.0.1', port), CallbackHandler) as server:
            server.oauth_state, server.code, server.denied = state, None, False
            print(json.dumps({'authorization_url': AUTHORIZE + '?' + urllib.parse.urlencode(fields)}), file=sys.stderr, flush=True)
            deadline = time.monotonic() + 900
            while not server.code and not server.denied:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SheetsError('Authorization timed out after 15 minutes. Run toucan-sheets auth again.')
                server.timeout = min(1, remaining)
                server.handle_request()
            if server.denied:
                raise SheetsError('Google authorization was denied.')
            code = server.code
        response = request(TOKEN, 'POST', fields=dict(
            grant_type='authorization_code', client_id=app['client_id'], client_secret=app['client_secret'],
            code=code, code_verifier=verifier, redirect_uri=redirect))
        record = token_record(response)
        email = identity(record, sensitive=[app['client_secret'], code, verifier])
        if account and email.casefold() != account.casefold():
            raise SheetsError('Authenticated account does not match --account; no token was saved.', 3)
        save(record)
        return dict(authenticated=True, email=email, scopes=SCOPES)


def json_input(path):
    try:
        if path == '-':
            return json.load(sys.stdin)
        with open(path) as source:
            return json.load(source)
    except (OSError, ValueError, UnicodeError):
        raise SheetsError('Could not read input; expected a JSON file or - for stdin.', 2) from None


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse may include an invalid argument verbatim in its error text.
        raise SheetsError('Invalid command or arguments. Use toucan-sheets --help.', 2)


def main(argv=None):
    parser = Parser(prog='toucan-sheets', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    login = sub.add_parser('auth')
    login.add_argument('--port', type=int, default=8765)
    login.add_argument('--force', action='store_true')
    login.add_argument('--account')
    sub.add_parser('status')
    create = sub.add_parser('create')
    create.add_argument('title')
    create.add_argument('--values')
    create.add_argument('--range', default='Sheet1!A1')
    read = sub.add_parser('read')
    read.add_argument('spreadsheet_id')
    read.add_argument('range')
    write = sub.add_parser('write')
    write.add_argument('spreadsheet_id')
    write.add_argument('range')
    write.add_argument('values')
    write.add_argument('--input-option', choices=['RAW', 'USER_ENTERED'], default='RAW')
    batch = sub.add_parser('batch-update')
    batch.add_argument('spreadsheet_id')
    batch.add_argument('body')
    for command in sub.choices.values():
        command.add_argument('--json', action='store_true', help='Output JSON (the default)')
    options = None
    try:
        options = parser.parse_args(argv)
        if options.command == 'auth':
            result = auth(options.port, options.force, options.account)
        else:
            record = credentials()
            if options.command == 'status':
                result = dict(authenticated=True, email=identity(record), scopes=record['scopes'],
                              has_refresh_token=bool(record.get('refresh_token')),
                              expiry=datetime.fromtimestamp(record['expires_at'], timezone.utc).isoformat())
            elif options.command == 'create':
                result = request(SHEETS, 'POST', record, body={'properties': {'title': options.title}})
                if options.values:
                    try:
                        url = SHEETS + '/' + urllib.parse.quote(result['spreadsheetId'], safe='') + '/values/' + urllib.parse.quote(options.range, safe='')
                        result['initialValuesUpdate'] = request(url + '?valueInputOption=RAW', 'PUT', record,
                                                                body={'values': json_input(options.values)})
                    except Exception:
                        print(json.dumps(dict(createdSpreadsheetId=result.get('spreadsheetId'),
                                              spreadsheetUrl=result.get('spreadsheetUrl'), initialValuesWritten=False)), file=sys.stderr)
                        raise
            else:
                url = SHEETS + '/' + urllib.parse.quote(options.spreadsheet_id, safe='')
                if options.command == 'batch-update':
                    result = request(url + ':batchUpdate', 'POST', record, body=json_input(options.body))
                else:
                    url += '/values/' + urllib.parse.quote(options.range, safe='')
                    if options.command == 'read':
                        result = request(url, record=record)
                    else:
                        result = request(url + '?valueInputOption=' + options.input_option, 'PUT', record,
                                         body={'values': json_input(options.values)})
        print(json.dumps(result, indent=2))
        return 0
    except (SheetsError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        reason = str(error) if isinstance(error, SheetsError) else 'Could not complete the Sheets operation; check local files and configuration.'
        if options and options.command == 'status':
            print(json.dumps(dict(authenticated=False, reason=reason)))
            return 1
        print(json.dumps({'error': reason}), file=sys.stderr)
        return error.code if isinstance(error, SheetsError) else 1


if __name__ == '__main__':
    sys.exit(main())
