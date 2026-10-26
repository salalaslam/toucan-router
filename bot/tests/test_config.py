import io
import json
import stat
import zipfile

import pytest
from PIL import Image

from toucan_bot import cli, config, package


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "config" / "config.toml"
    path.parent.mkdir()
    for key in ("TENANT_ID", "PUBLIC_URL", "PORT"):
        monkeypatch.delenv("TOUCAN_BOT_" + key, raising=False)
    monkeypatch.setenv("TOUCAN_BOT_CONFIG", str(path))
    return path


def test_status_and_serve_report_missing_configuration(settings, capsys):
    assert cli.main(["status", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["credentials"] is False
    assert out["tenant_id"] == "" and out["public_url"] == ""
    assert cli.main(["serve"]) == 1
    assert "missing tenant_id, public_url" in capsys.readouterr().err


def test_file_config_env_overrides_and_status_omit_secrets(settings, monkeypatch, capsys):
    settings.write_text('tenant_id = "00000000-0000-0000-0000-000000000000"\n'
                        'public_url = "https://old.example.com"\nport = 3978\n'
                        'app_name = "Example Assistant"\norg_name = "Example Org"\n')
    monkeypatch.setenv("TOUCAN_BOT_PUBLIC_URL", "https://bot.example.com/")
    monkeypatch.setenv("TOUCAN_BOT_PORT", "4000")
    config.save_credentials(config.Credentials("11111111-1111-1111-1111-111111111111", "hidden-secret"))
    assert stat.S_IMODE(config.config_dir().stat().st_mode) == 0o700
    assert cli.main(["status", "--json"]) == 0
    raw = capsys.readouterr().out
    out = json.loads(raw)
    assert out["ok"] and out["credentials"]
    assert out["tenant_id"] == "…0000" and out["bot_id"] == "…1111"
    assert out["public_url"] == "https://bot.example.com" and out["local_port"] == 4000
    assert "hidden-secret" not in raw and "setup/" not in raw
    assert cli.main(["--json", "status", "--show-ids"]) == 0
    assert json.loads(capsys.readouterr().out)["tenant_id"] == "00000000-0000-0000-0000-000000000000"


def test_package_uses_config_and_valid_teams_icons(settings, tmp_path):
    settings.write_text('public_url = "https://bot.example.com"\napp_name = "Example Assistant"\n'
                        'developer_name = "Example Developer"\nwebsite = "https://example.com"\n')
    path = package.build("11111111-1111-1111-1111-111111111111", tmp_path / "app.zip")
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert manifest['manifestVersion'] == '1.19'
        assert manifest['version'] == '1.1.0'
        assert manifest['webApplicationInfo'] == {'id': manifest['id'], 'resource': 'https://RscBasedStoreApp'}
        assert manifest['authorization']['permissions']['resourceSpecific'] == [
            {'name': 'ChatMessage.Read.Chat', 'type': 'Application'}]
        assert manifest["name"]["short"] == "Example Assistant"
        assert manifest["developer"]["name"] == "Example Developer"
        assert manifest["developer"]["privacyUrl"] == "https://bot.example.com/privacy"
        assert Image.open(io.BytesIO(z.read("color.png"))).size == (192, 192)
        outline = Image.open(io.BytesIO(z.read("outline.png")))
        assert outline.size == (32, 32) and outline.mode == "RGBA"
        assert set(outline.get_flattened_data()) == {(255, 255, 255, 0), (255, 255, 255, 255)}


@pytest.mark.parametrize("text,reason", [
    ('tenant_id = "invalid"', "tenant_id must be a GUID"),
    ('public_url = "https://user:password@example.com"', "public_url must be"),
    ('port = 0', "port must be"),
    ('port = 3978.5', "port must be"),
    ('catchup_interval_seconds = 0', 'catchup_interval_seconds must be a positive integer'),
    ('catchup_lookback_days = -1', 'catchup_lookback_days must be a positive integer'),
    ('media_max_bytes = true', 'media_max_bytes must be a positive integer'),
    ('media_retention_days = 1.5', 'media_retention_days must be a positive integer'),
])
def test_invalid_bot_config_is_reported_without_echoing_values(settings, capsys, text, reason):
    settings.write_text(text)
    assert cli.main(["status", "--json"]) == 1
    captured = capsys.readouterr()
    assert reason in captured.err and "password" not in captured.err and not captured.out


def test_changed_tenant_requires_new_credentials(settings, capsys):
    settings.write_text('tenant_id = "00000000-0000-0000-0000-000000000000"\n'
                        'public_url = "https://bot.example.com"\n')
    config.save_credentials(config.Credentials("11111111-1111-1111-1111-111111111111", "hidden-secret"))
    settings.write_text('tenant_id = "11111111-1111-1111-1111-111111111111"\n'
                        'public_url = "https://bot.example.com"\n')
    assert cli.main(["status", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["registered"] and not out["ok"] and not out["credentials"]
    assert "different tenant" in out["warning"]
    assert cli.main(["serve"]) == 1
    assert "different tenant" in capsys.readouterr().err
