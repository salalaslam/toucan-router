import base64
import fcntl
import hashlib
import io
import json
import os
import stat
import time
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from toucan_tools import sheets


@pytest.fixture
def sheets_home(tmp_path, monkeypatch):
    path = tmp_path / 'sheets'
    monkeypatch.setenv('TOUCAN_SHEETS_DIR', str(path))

    def offline(*args, **kwargs):
        pytest.fail('Unexpected network request')

    monkeypatch.setattr(sheets.HTTP, 'open', offline)
    return path


def install_client(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    (path / 'client.json').write_text(json.dumps({'installed': {
        'client_id': 'desktop.example.com', 'client_secret': 'fictional-client-secret',
    }}))
    (path / 'client.json').chmod(0o600)


def install_token(path, expires_at=None):
    record = dict(access_token='fictional-access-token', refresh_token='fictional-refresh-token',
                  expires_at=expires_at if expires_at is not None else time.time() + 3600,
                  scopes=['https://www.googleapis.com/auth/drive.file'])
    (path / 'token.json').write_text(json.dumps(record))
    (path / 'token.json').chmod(0o600)
    return record


def response(data):
    return io.BytesIO(json.dumps(data).encode())


class CallbackSocket:
    def __init__(self, target):
        self.input = io.BytesIO(f'GET {target} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n'.encode())
        self.output = bytearray()

    def makefile(self, mode, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


class AuthorizationServer:
    def __init__(self, address, handler):
        assert address == ('127.0.0.1', 8765)
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def handle_request(self):
        connection = CallbackSocket('/?code=fictional-code&state=' + self.oauth_state)
        self.handler(connection, ('127.0.0.1', 1555), self)
        assert b'200 OK' in connection.output


@pytest.mark.parametrize('has_client', [False, True])
def test_status_without_setup_is_json(sheets_home, has_client, capsys):
    if has_client:
        install_client(sheets_home)
    assert sheets.main(['status']) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result['authenticated'] is False
    assert ('Not authenticated' if has_client else 'No desktop OAuth client') in result['reason']
    assert not output.err


def test_auth_pkce_and_private_atomic_token(sheets_home, monkeypatch, capsys):
    install_client(sheets_home)
    sheets_home.chmod(0o755)
    (sheets_home / 'client.json').chmod(0o644)
    monkeypatch.setattr(sheets, 'CallbackServer', AuthorizationServer)
    replace = sheets.os.replace
    replacements = []

    def atomic(source, target):
        assert source != target
        assert stat.S_IMODE(os.stat(source).st_mode) == 0o600
        assert not target.exists()
        replacements.append((source, target))
        replace(source, target)

    def network(req, timeout):
        assert timeout == 60
        if req.full_url == sheets.TOKEN:
            fields = parse_qs(req.data.decode())
            authorization = json.loads(capsys.readouterr().err)['authorization_url']
            query = parse_qs(urlsplit(authorization).query)
            assert urlsplit(authorization).netloc == 'accounts.google.com'
            assert query['scope'] == ['https://www.googleapis.com/auth/drive.file']
            assert query['code_challenge_method'] == ['S256']
            assert query['code_challenge'] == [base64.urlsafe_b64encode(hashlib.sha256(
                fields['code_verifier'][0].encode()).digest()).decode().rstrip('=')]
            assert 43 <= len(fields['code_verifier'][0]) <= 128
            assert query['access_type'] == ['offline'] and query['prompt'] == ['consent']
            assert query['login_hint'] == ['alice@example.com']
            assert fields['redirect_uri'] == query['redirect_uri'] == ['http://127.0.0.1:8765/']
            assert fields['code'] == ['fictional-code']
            assert fields['grant_type'] == ['authorization_code']
            return response(dict(access_token='new-access-token', refresh_token='new-refresh-token',
                                 token_type='Bearer', expires_in=3600))
        assert req.full_url == sheets.IDENTITY
        assert req.get_header('Authorization') == 'Bearer new-access-token'
        return response({'user': {'emailAddress': 'ALICE@example.com'}})

    monkeypatch.setattr(sheets.HTTP, 'open', network)
    monkeypatch.setattr(sheets.os, 'replace', atomic)
    previous = os.umask(0)
    try:
        assert sheets.main(['auth', '--account', 'alice@example.com']) == 0
    finally:
        os.umask(previous)
    assert json.loads(capsys.readouterr().out) == dict(authenticated=True, email='ALICE@example.com', scopes=sheets.SCOPES)
    saved = json.loads((sheets_home / 'token.json').read_text())
    assert saved['access_token'] == 'new-access-token'
    assert saved['refresh_token'] == 'new-refresh-token'
    assert len(replacements) == 1
    assert stat.S_IMODE(sheets_home.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in sheets_home.iterdir())
    assert not list(sheets_home.glob('.token-*'))


@pytest.mark.parametrize('existing,force,account,email,exit_code', [
    (True, False, None, 'alice@example.com', 3),
    (True, True, 'alice@example.com', 'bob@example.com', 3),
    (False, False, 'alice@example.com', 'bob@example.com', 3),
    (True, True, None, 'bob@example.com', 0),
])
def test_existing_authorization_and_account_guard(sheets_home, monkeypatch, capsys, existing, force, account, email, exit_code):
    install_client(sheets_home)
    original = install_token(sheets_home) if existing else None
    monkeypatch.setattr(sheets, 'CallbackServer', AuthorizationServer)
    calls = []

    def network(req, timeout):
        calls.append(req.full_url)
        if req.full_url == sheets.TOKEN:
            return response(dict(access_token='replacement-access', refresh_token='replacement-refresh',
                                 token_type='Bearer', expires_in=3600))
        return response({'user': {'emailAddress': email}})

    monkeypatch.setattr(sheets.HTTP, 'open', network)
    args = ['auth'] + (['--force'] if force else []) + (['--account', account] if account else [])
    assert sheets.main(args) == exit_code
    saved = json.loads((sheets_home / 'token.json').read_text()) if (sheets_home / 'token.json').exists() else None
    output = capsys.readouterr()
    if exit_code:
        assert saved == original
        assert ('already exists' if existing and not force else 'does not match') in output.err
    else:
        assert saved['access_token'] == 'replacement-access'
        assert json.loads(output.out)['email'] == 'bob@example.com'
    if existing and not force:
        assert not calls
    assert all(secret not in output.out + output.err for secret in (
        'fictional-access-token', 'fictional-refresh-token', 'replacement-access', 'replacement-refresh', 'fictional-client-secret'))


@pytest.mark.parametrize('target,accepted,denied', [
    ('/?state=expected&code=code', True, False),
    ('/?state=wrong&code=code', False, False),
    ('/?code=code', False, False),
    ('/?state=expected&state=wrong&code=code', False, False),
    ('/?state=expected&code=one&code=two', False, False),
    ('/other?state=expected&code=code', False, False),
    ('/?state=expected&error=access_denied', True, True),
    ('/?state=expected&error=access_denied&code=code', False, False),
])
def test_callback_state_validation(target, accepted, denied, capsys):
    server = AuthorizationServer(('127.0.0.1', 8765), sheets.CallbackHandler)
    server.oauth_state, server.code, server.denied = 'expected', None, False
    connection = CallbackSocket(target)
    sheets.CallbackHandler(connection, ('127.0.0.1', 1555), server)
    assert (b'200 OK' if accepted else b'400 Bad Request') in connection.output
    assert server.code == ('code' if accepted and not denied else None)
    assert server.denied is denied
    assert capsys.readouterr() == ('', '')


def test_auth_timeout_does_not_save(sheets_home, monkeypatch, capsys):
    install_client(sheets_home)
    monkeypatch.setattr(sheets, 'CallbackServer', AuthorizationServer)
    times = iter([0, 901])
    monkeypatch.setattr(sheets.time, 'monotonic', lambda: next(times))
    assert sheets.main(['auth']) == 1
    assert '15 minutes' in json.loads(capsys.readouterr().err.splitlines()[-1])['error']
    assert not (sheets_home / 'token.json').exists()


@pytest.mark.parametrize('token_response', [
    dict(access_token='new-access-token', token_type='Bearer', expires_in=3600),
    dict(access_token='new-access-token', refresh_token='new-refresh-token', token_type='Bearer',
         expires_in=3600, scope='https://www.googleapis.com/auth/drive'),
])
def test_auth_rejects_missing_offline_access_or_extra_scopes(sheets_home, monkeypatch, capsys, token_response):
    install_client(sheets_home)
    monkeypatch.setattr(sheets, 'CallbackServer', AuthorizationServer)
    monkeypatch.setattr(sheets.HTTP, 'open', lambda req, timeout: response(token_response))
    assert sheets.main(['auth']) == 1
    output = capsys.readouterr()
    assert json.loads(output.err.splitlines()[-1])['error']
    assert 'new-access-token' not in output.out + output.err
    assert not (sheets_home / 'token.json').exists()


def test_invalid_client_does_not_start_authorization(sheets_home, capsys):
    install_client(sheets_home)
    (sheets_home / 'client.json').write_text('{"web":{"client_secret":"fictional-client-secret"}}')
    assert sheets.main(['auth']) == 2
    output = capsys.readouterr()
    assert 'installed desktop OAuth client' in json.loads(output.err)['error']
    assert 'fictional-client-secret' not in output.err and not output.out


def test_refresh_holds_lock_and_preserves_refresh_token(sheets_home, monkeypatch, capsys):
    install_client(sheets_home)
    install_token(sheets_home, expires_at=0)
    calls = []

    def network(req, timeout):
        calls.append(req.full_url)
        if req.full_url == sheets.TOKEN:
            with (sheets_home / 'token.lock').open() as other:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fields = parse_qs(req.data.decode())
            assert fields['grant_type'] == ['refresh_token']
            assert fields['refresh_token'] == ['fictional-refresh-token']
            return response(dict(access_token='refreshed-access', token_type='Bearer', expires_in=3600))
        assert req.full_url == sheets.IDENTITY
        assert req.get_header('Authorization') == 'Bearer refreshed-access'
        return response({'user': {'emailAddress': 'alice@example.com'}})

    monkeypatch.setattr(sheets.HTTP, 'open', network)
    for attempt in range(2):
        assert sheets.main(['status', '--json']) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['authenticated'] is True
        assert result['email'] == 'alice@example.com'
        assert result['scopes'] == sheets.SCOPES and result['has_refresh_token'] is True
        assert result['expiry'].endswith('+00:00')
    assert calls.count(sheets.TOKEN) == 1
    saved = json.loads((sheets_home / 'token.json').read_text())
    assert saved['access_token'] == 'refreshed-access' and saved['refresh_token'] == 'fictional-refresh-token'


def test_atomic_replace_failure_keeps_old_token(sheets_home, monkeypatch, capsys):
    install_client(sheets_home)
    original = install_token(sheets_home, expires_at=0)
    monkeypatch.setattr(sheets.HTTP, 'open', lambda req, timeout: response(dict(
        access_token='new-access-token', token_type='Bearer', expires_in=3600)))

    def fail(source, target):
        raise OSError('fictional-access-token must not leak from exceptions')

    monkeypatch.setattr(sheets.os, 'replace', fail)
    assert sheets.main(['status']) == 1
    assert json.loads((sheets_home / 'token.json').read_text()) == original
    assert not list(sheets_home.glob('.token-*'))
    assert 'fictional-access-token' not in capsys.readouterr().out


@pytest.mark.parametrize('operation', ['read', 'refresh'])
def test_google_error_messages_redact_credentials(sheets_home, monkeypatch, capsys, operation):
    install_client(sheets_home)
    install_token(sheets_home, expires_at=0 if operation == 'refresh' else None)

    def network(req, timeout):
        message = 'Denied fictional-access-token fictional-refresh-token'
        if operation == 'refresh':
            message += ' fictional-client-secret'
        body = {'error': {'message': message}}
        raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden', {}, response(body))

    monkeypatch.setattr(sheets.HTTP, 'open', network)
    assert sheets.main(['read', 'sheet', 'A1']) == 1
    output = capsys.readouterr()
    result = json.loads(output.err)
    assert 'Google API HTTP 403: Denied' in result['error']
    assert all(value not in output.err + output.out for value in (
        'fictional-access-token', 'fictional-refresh-token', 'fictional-client-secret', 'Authorization', 'Bearer'))


@pytest.mark.parametrize('command,method,suffix,payload', [
    (['read', 'sheet/id', "'Sales / 2026'!A1:B2"], 'GET',
     '/sheet%2Fid/values/%27Sales%20%2F%202026%27%21A1%3AB2', None),
    (['write', 'sheet/id', 'A1:B2', '-', '--input-option', 'USER_ENTERED'], 'PUT',
     '/sheet%2Fid/values/A1%3AB2?valueInputOption=USER_ENTERED', {'values': [[1, 2]]}),
    (['write', 'sheet', 'A1', '-'], 'PUT', '/sheet/values/A1?valueInputOption=RAW', {'values': [[1, 2]]}),
    (['batch-update', 'sheet/id', '-'], 'POST', '/sheet%2Fid:batchUpdate',
     {'requests': [{'addSheet': {'properties': {'title': 'Example'}}}]}),
])
def test_sheets_api_commands(sheets_home, monkeypatch, capsys, command, method, suffix, payload):
    install_client(sheets_home)
    install_token(sheets_home)
    input_body = payload if command[0] == 'batch-update' else [[1, 2]]
    monkeypatch.setattr(sheets.sys, 'stdin', io.StringIO(json.dumps(input_body)))

    def network(req, timeout):
        assert req.full_url == sheets.SHEETS + suffix
        assert req.method == method
        assert (json.loads(req.data) if req.data else None) == payload
        assert req.get_header('Authorization') == 'Bearer fictional-access-token'
        return response({'result': 'done'})

    monkeypatch.setattr(sheets.HTTP, 'open', network)
    assert sheets.main(command) == 0
    assert json.loads(capsys.readouterr().out) == {'result': 'done'}


@pytest.mark.parametrize('failure', [None, 'api', 'input'])
def test_create_initial_values_and_recovery_receipt(sheets_home, tmp_path, monkeypatch, capsys, failure):
    install_client(sheets_home)
    install_token(sheets_home)
    values = tmp_path / 'values.json'
    values.write_text('invalid JSON' if failure == 'input' else '[["alice", 1]]')
    calls = []

    def network(req, timeout):
        calls.append(req.full_url)
        if req.full_url == sheets.SHEETS:
            assert json.loads(req.data) == {'properties': {'title': 'Example'}}
            return response({'spreadsheetId': 'created/id', 'spreadsheetUrl': 'https://example.com/sheet'})
        assert req.full_url == sheets.SHEETS + '/created%2Fid/values/Sheet1%21A1?valueInputOption=RAW'
        assert json.loads(req.data) == {'values': [['alice', 1]]}
        if failure == 'api':
            raise urllib.error.HTTPError(req.full_url, 400, 'Bad Request', {}, response({'error': {'message': 'Bad range'}}))
        return response({'updatedCells': 2})

    monkeypatch.setattr(sheets.HTTP, 'open', network)
    assert sheets.main(['create', 'Example', '--values', str(values)]) == (2 if failure == 'input' else 1 if failure else 0)
    output = capsys.readouterr()
    assert len(calls) == (1 if failure == 'input' else 2)
    if failure:
        receipt, error = map(json.loads, output.err.splitlines())
        assert receipt == dict(createdSpreadsheetId='created/id', spreadsheetUrl='https://example.com/sheet', initialValuesWritten=False)
        assert error['error'] and not output.out
    else:
        assert json.loads(output.out)['initialValuesUpdate'] == {'updatedCells': 2}

