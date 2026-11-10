import pytest

from toucan_bot.media import download_url

KEY = '19:chat/1700000000000'
TENANT = '00000000-0000-0000-0000-000000000000'
BASE = 'https://smba.trafficmanager.net'
TAIL = 'v3/attachments/0-abc-d1-0123456789abcdef/views/original'


@pytest.mark.parametrize('prefix', ['', 'apac/', f'apac/{TENANT}/'])
def test_connector_url_keeps_region_and_tenant(prefix):
    url = f'{BASE}/{prefix}{TAIL}'
    assert download_url(url, 'live', KEY) == url


@pytest.mark.parametrize('prefix', ['apac/not-a-tenant/', 'apac/%2e%2e/', f'{TENANT}/apac/', f'apac/{TENANT}/extra/'])
def test_connector_url_rejects_other_prefixes(prefix):
    with pytest.raises(ValueError):
        download_url(f'{BASE}/{prefix}{TAIL}', 'live', KEY)


def test_tenant_prefix_does_not_widen_hosts():
    with pytest.raises(ValueError):
        download_url(f'https://example.com/apac/{TENANT}/{TAIL}', 'live', KEY)
