import json
import sqlite3
import stat

import pytest

from toucan import telemetry
from toucan.effects import Effects

CFG = dict(settings={}, threads={'desk': {}, 'web': {}}, people={'ann': {}},
           routes=[dict(source='whatsapp', router='live'), dict(source='teams', router='shadow'), dict(source='teams')])


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0
        self.sent = []
        self.fail = False

    def post(self, url, data, headers, timeout=20):
        if self.fail:
            raise OSError('down')
        self.sent.append(dict(url=url, body=json.loads(data), timeout=timeout))
        return 200


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry, 'POSTHOG_KEY', 'phc_test')
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE requests (id INTEGER PRIMARY KEY, created_at REAL)')
    clock = Clock()
    db.executemany('INSERT INTO requests (created_at) VALUES (?)', [(clock.t - 60,), (clock.t - 100,), (clock.t - 2 * 86400,)])
    fx = Effects(now=lambda: clock.t, post=clock.post)
    return clock, db, fx, tmp_path / 'state' / 'telemetry.json'


def report(fake, environ=None, cfg=CFG, stderr=None):
    clock, db, fx, path = fake
    return telemetry.report(cfg, db, fx, path, environ or {}, stderr)


def test_sends_counts_only_and_once_a_day(fake, capsys):
    clock, db, fx, path = fake
    assert report(fake) == 'sent'
    assert 'TOUCAN_TELEMETRY=0' in capsys.readouterr().err
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    body = clock.sent[0]['body']
    assert clock.sent[0]['timeout'] == telemetry.TIMEOUT
    assert body['event'] == 'router.active' and body['api_key'] == 'phc_test'
    install = json.loads(path.read_text())['install_id']
    assert len(body['distinct_id']) == 64 and install not in json.dumps(body)
    props = body['properties']
    assert set(props) == {'version', 'python', 'os', 'routes', 'threads', 'people', 'router_off', 'router_shadow',
                          'router_live', 'sources', 'requests_24h', '$process_person_profile', '$geoip_disable', '$lib'}
    assert (props['routes'], props['threads'], props['people'], props['requests_24h']) == (3, 2, 1, 2)
    assert (props['router_off'], props['router_shadow'], props['router_live']) == (1, 1, 1)
    assert props['sources'] == ['teams', 'whatsapp'] and props['$process_person_profile'] is False

    clock.t += 86400 - 1
    assert report(fake) == 'skip'
    clock.t += 1
    assert report(fake) == 'sent'
    assert len(clock.sent) == 2 and clock.sent[1]['body']['distinct_id'] == body['distinct_id']
    assert capsys.readouterr().err == ''


def test_failure_waits_an_hour_and_never_raises(fake):
    clock, *_ = fake
    clock.fail = True
    assert report(fake) == 'failed'
    clock.fail = False
    clock.t += 3599
    assert report(fake) == 'skip'
    clock.t += 1
    assert report(fake) == 'sent'


@pytest.mark.parametrize('environ, settings', [
    ({'TOUCAN_TELEMETRY': '0'}, {}), ({'TOUCAN_TELEMETRY': 'off'}, {}), ({'DO_NOT_TRACK': '1'}, {}),
    ({}, {'telemetry': False}),
])
def test_opt_outs_send_nothing_and_write_nothing(fake, environ, settings):
    clock, _, _, path = fake
    assert report(fake, environ, CFG | {'settings': settings}) == 'off'
    assert clock.sent == [] and not path.exists()


def test_do_not_track_zero_keeps_it_on(fake):
    assert report(fake, {'DO_NOT_TRACK': '0'}) == 'sent'


def test_unpublished_key_is_off(fake, monkeypatch):
    monkeypatch.setattr(telemetry, 'POSTHOG_KEY', 'phc_REPLACE_ME')
    assert report(fake) == 'off'



def test_cli_sends_after_real_runs_only_and_honours_the_setting(request, monkeypatch, capsys):
    from toucan.cli import main
    from toucan.config import ValidationError, load
    w = request.getfixturevalue('world')
    monkeypatch.setattr(telemetry, 'POSTHOG_KEY', 'phc_test')
    monkeypatch.setenv('TOUCAN_TELEMETRY', '1')
    monkeypatch.setenv('TOUCAN_CONFIG', str(w.config_path))
    monkeypatch.setenv('TOUCAN_DB', str(w.db_path))
    sent = []
    w.fx.post = lambda url, data, headers, timeout=20: sent.append(json.loads(data)) or 200
    run = lambda *argv: main(list(argv), effects=w.fx, paths=w.paths)

    run('sync', '--dry-run', '--json')
    assert sent == [] and not (w.db_path.parent / 'telemetry.json').exists()
    run('status', '--json')
    assert [s['event'] for s in sent] == ['router.active']
    assert 'TOUCAN_TELEMETRY=0' in capsys.readouterr().err
    run('status', '--json')
    assert len(sent) == 1

    assert run('config', 'set', 'settings.telemetry', 'false', '--json') == 0
    assert load(w.config_path)['settings']['telemetry'] is False
    (w.db_path.parent / 'telemetry.json').unlink()
    run('status', '--json')
    assert len(sent) == 1
    w.config_path.write_text(w.config_path.read_text().replace('telemetry = false', 'telemetry = "no"'))
    with pytest.raises(ValidationError, match='telemetry must be true or false'):
        load(w.config_path)
