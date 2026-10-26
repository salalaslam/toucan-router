import base64
import json
import os
import stat
import time

import pytest

from toucan_router_tools import upload_loom


@pytest.fixture
def loom_home(tmp_path, monkeypatch):
    config = tmp_path / 'toucan' / 'loom'
    monkeypatch.setattr(upload_loom, 'CONFIG', config)
    monkeypatch.setattr(upload_loom, 'CREDENTIALS', config / 'oauth.json')
    return config


def test_logged_out_status_is_json(loom_home, capsys):
    assert upload_loom.main(['status']) == 1
    assert json.loads(capsys.readouterr().out) == {'logged_in': False}
    assert not loom_home.exists()


def test_login_saves_private_credentials(loom_home, monkeypatch, capsys):
    site = '00000000-0000-0000-0000-000000000001'
    payload = base64.urlsafe_b64encode(json.dumps({'aud': [f'ari:cloud:loom::site/{site}']}).encode()).decode().rstrip('=')
    token = 'header.' + payload + '.signature'
    responses = iter([
        {'device_code': 'device', 'verification_uri': 'https://auth.atlassian.com/authorize', 'expires_in': 60},
        {'access_token': token, 'refresh_token': 'saved-refresh-token', 'expires_in': 3600, 'token_type': 'Bearer'},
    ])
    monkeypatch.setattr(upload_loom, 'request', lambda path, fields: next(responses))
    monkeypatch.setattr(upload_loom.time, 'sleep', lambda seconds: None)
    previous = os.umask(0)
    try:
        assert upload_loom.main(['login']) == 0
    finally:
        os.umask(previous)
    saved = json.loads((loom_home / 'oauth.json').read_text())
    assert saved['cloud_id'] == site
    assert saved['access_token'] == token
    assert stat.S_IMODE(loom_home.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in loom_home.iterdir())
    output = capsys.readouterr().out
    assert token not in output and 'saved-refresh-token' not in output
    assert upload_loom.main(['login']) == 1
    assert json.loads((loom_home / 'oauth.json').read_text()) == saved


def test_upload_and_status_json_with_mocked_network(loom_home, tmp_path, monkeypatch, capsys):
    record = {'access_token': 'access', 'refresh_token': 'refresh', 'expires_at': time.time() + 3600,
              'cloud_id': '00000000-0000-0000-0000-000000000001'}
    upload_loom.save(record)
    movie = tmp_path / 'clip.mp4'
    movie.write_bytes(b'video-content')
    sent, completion = [], []

    def network(url, method='GET', headers=None, data=None, timeout=60):
        if url == upload_loom.GRAPHQL:
            message = json.loads(data)
            query = message['query']
            if 'loom_spacesSearch' in query:
                result = {'loom_spacesSearch': [{'id': 'ari:cloud:loom:site:space/activation/workspace/space'}]}
            elif 'loom_createFileUploadCredentials' in query:
                result = {'loom_createFileUploadCredentials': {'success': True, 'credentials': {
                    '__typename': 'LoomS3UploadInfo', 'endpoint': 'https://storage.example.com',
                    'path': 'uploads/workspace', 'region': 'us-east-1', 'accessKeyId': 'key',
                    'secretAccessKey': 'secret', 'sessionToken': 'session'}}}
            else:
                completion.append(message['variables']['input'])
                result = {'loom_completeVideoFileUpload': {'success': True,
                          'video': {'id': 'video-id', 'url': 'https://www.loom.com/share/video-id'}}}
            return json.dumps({'data': result}).encode()
        assert method == 'PUT'
        sent.append(data.read())
        return b''

    monkeypatch.setattr(upload_loom, 'fetch', network)
    assert upload_loom.main(['status']) == 0
    assert json.loads(capsys.readouterr().out) == {'logged_in': True, 'workspace': 'workspace'}
    assert upload_loom.main(['upload', str(movie), '--title', 'Review']) == 0
    assert json.loads(capsys.readouterr().out) == {'url': 'https://www.loom.com/share/video-id', 'id': 'video-id'}
    assert sent == [b'video-content']
    assert completion[0]['fileName'] == 'Review.mp4'
