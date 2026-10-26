from __future__ import annotations

import json
import os
import secrets
import re
import tomllib
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULTS = dict(tenant_id="", public_url="", port=3978, org_name="Example Org", contact="",
                app_name="Toucan", developer_name="Example Org", website="https://example.com",
                owner_teams_id="", catchup_interval_seconds=120, catchup_lookback_days=7,
                roster_interval_seconds=3600, media_max_bytes=10485760, media_retention_days=30)


def config_path() -> Path:
    return Path(os.environ.get("TOUCAN_BOT_CONFIG", str(config_dir() / "config.toml"))).expanduser()


def config_dir() -> Path:
    if "TOUCAN_BOT_CONFIG" in os.environ:
        return Path(os.environ["TOUCAN_BOT_CONFIG"]).expanduser().parent
    return Path(os.environ.get("TOUCAN_BOT_CONFIG_DIR", "~/.config/toucan-bot")).expanduser()


def load() -> dict:
    try:
        with config_path().open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, tomllib.TOMLDecodeError):
        raise ValueError("cannot load bot config; check its path and TOML syntax") from None
    settings = DEFAULTS | data
    for key in ("tenant_id", "public_url", "port"):
        if (value := os.environ.get("TOUCAN_BOT_" + key.upper())) is not None:
            settings[key] = value
    try:
        if isinstance(settings["port"], (bool, float)):
            raise ValueError
        settings["port"] = int(settings["port"])
        if not 0 < settings["port"] < 65536:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("bot port must be an integer from 1 to 65535") from None
    numeric = ('catchup_interval_seconds', 'catchup_lookback_days', 'roster_interval_seconds',
               'media_max_bytes', 'media_retention_days')
    for key in numeric:
        if type(settings[key]) is not int or settings[key] <= 0:
            raise ValueError(f'bot {key} must be a positive integer')
    for key in DEFAULTS.keys() - {"port", *numeric}:
        if not isinstance(settings[key], str):
            raise ValueError(f"bot {key} must be text")
    if settings['owner_teams_id'] and not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', settings['owner_teams_id']):
        raise ValueError('bot owner_teams_id must be a GUID')
    settings['owner_teams_id'] = settings['owner_teams_id'].lower()
    if settings["tenant_id"] and not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", settings["tenant_id"]):
        raise ValueError("bot tenant_id must be a GUID")
    settings["tenant_id"] = settings["tenant_id"].lower()
    if settings["public_url"]:
        url = urlsplit(settings["public_url"])
        if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError("bot public_url must be a public HTTPS URL without credentials or query")
    settings["public_url"] = settings["public_url"].rstrip("/")
    return settings


def require_setup() -> dict:
    settings = load()
    missing = [key for key in ("tenant_id", "public_url") if not settings[key]]
    if missing:
        raise ValueError("bot config missing " + ", ".join(missing) + "; set them in config.toml or TOUCAN_BOT_* environment variables")
    return settings


def state_dir() -> Path:
    return Path(os.environ.get("TOUCAN_BOT_STATE_DIR", "~/.local/state/toucan-bot")).expanduser()


def db_path() -> Path:
    return state_dir() / "bot.db"


def package_path() -> Path:
    return state_dir() / "toucan-teams-app.zip"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    tenant_id: str = field(default_factory=lambda: load()["tenant_id"])
    # How the bot is registered with the Bot Framework, not the Entra app's account types.
    # New registrations can only be SingleTenant; MultiTenant is for older ones.
    app_type: str = "SingleTenant"


def load_credentials() -> Credentials | None:
    path = config_dir() / "credentials.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return Credentials(data["client_id"], data["client_secret"], data.get("tenant_id", load()["tenant_id"]).lower(),
                       data.get("app_type", "SingleTenant"))


def credential_warning(creds: Credentials | None, settings: dict) -> str | None:
    if creds is None:
        return None
    if not creds.client_id or not creds.client_secret or creds.app_type not in APP_TYPES:
        return "stored bot credentials are incomplete; reset them and finish setup again"
    if creds.tenant_id.lower() != settings["tenant_id"].lower():
        return "stored bot credentials belong to a different tenant; reset them and finish setup again"
    return None


def save_credentials(creds: Credentials) -> None:
    data = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "tenant_id": creds.tenant_id.lower(),
        "app_type": creds.app_type,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write_private(config_dir() / "credentials.json", json.dumps(data, indent=2) + "\n")


APP_TYPES = ("SingleTenant", "MultiTenant")


def app_tenant(creds: Credentials) -> str | None:
    """Tenant to request bot tokens from: None means the SDK's multi-tenant default (botframework.com).

    Entra will issue tokens from either authority, so only the Bot Framework registration says which one
    the Bot Connector accepts.
    """
    return None if creds.app_type == "MultiTenant" else creds.tenant_id


def clear_credentials() -> bool:
    path = config_dir() / "credentials.json"
    if path.exists():
        path.unlink()
        return True
    return False


def setup_token() -> str:
    """The secret part of the setup link. Created on first use."""
    path = config_dir() / "setup-token"
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    _write_private(path, token + "\n")
    return token


def setup_url() -> str:
    return f"{require_setup()['public_url']}/setup/{setup_token()}"
