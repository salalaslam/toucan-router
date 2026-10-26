"""Build the Teams app package (manifest.json and two icons, zipped)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from . import config

ICON = Path(__file__).parent / "assets" / "toucan.png"
ACCENT = "#1B3BF0"
# Teams app version is independent of the Python package. It must exceed the installed 1.0.1
# so Teams accepts the upgrade and offers per-chat RSC consent.
VERSION = "1.1.0"


def manifest(bot_id: str) -> dict:
    cfg = config.load()
    app = cfg["app_name"]
    return {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.19/MicrosoftTeams.schema.json",
        "manifestVersion": "1.19",
        "version": VERSION,
        "id": bot_id,
        "developer": {
            "name": cfg["developer_name"],
            "websiteUrl": cfg["website"],
            "privacyUrl": f"{cfg['public_url']}/privacy",
            "termsOfUseUrl": f"{cfg['public_url']}/terms",
        },
        "name": {"short": app, "full": f"{app} project assistant"},
        "description": {
            "short": f"Project requests and updates from {app}.",
            "full": (f"{app} posts replies and updates from an AI project assistant for {cfg['org_name']}. "
                     "It receives messages in chats where it is installed. It posts as the bot."),
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": ACCENT,
        "webApplicationInfo": {"id": bot_id, "resource": "https://RscBasedStoreApp"},
        "authorization": {"permissions": {"resourceSpecific": [
            {"name": "ChatMessage.Read.Chat", "type": "Application"},
        ]}},
        "bots": [
            {
                "botId": bot_id,
                "scopes": ["personal", "groupChat"],
                "supportsFiles": False,
                "isNotificationOnly": False,
            }
        ],
    }


def color_icon() -> bytes:
    return _png(Image.open(ICON).convert("RGBA").resize((192, 192), Image.LANCZOS))


def outline_icon() -> bytes:
    img = Image.new("RGBA", (32, 32), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle((6, 5, 25, 9), fill="white")
    draw.rectangle((13, 9, 18, 27), fill="white")
    return _png(img)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def build(bot_id: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest(bot_id), indent=2))
        z.writestr("color.png", color_icon())
        z.writestr("outline.png", outline_icon())
    dest.write_bytes(buf.getvalue())
    return dest
