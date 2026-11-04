"""Prepare a local image for an inline Teams message from the bot.

Teams shows a base64 data URI attachment inline in personal and group chats. It accepts
static PNG, JPEG and GIF up to 1024x1024 and does not play animations, so the image is
downscaled, re-encoded and size-capped here, before anything is sent.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SIDE = 1024
# Raw bytes; base64 adds a third. Kept under the largest data URI Teams has accepted.
MAX_BYTES = 700_000
MAX_INPUT_BYTES = 30_000_000
MAX_INPUT_PIXELS = 50_000_000


class ImageError(ValueError):
    """The file cannot be sent as an inline Teams image."""


@dataclass(frozen=True)
class Prepared:
    name: str
    content_type: str
    data: bytes
    width: int
    height: int

    def data_uri(self) -> str:
        return f"data:{self.content_type};base64,{base64.b64encode(self.data).decode('ascii')}"


def prepare(path, *, max_side: int = MAX_SIDE, max_bytes: int = MAX_BYTES) -> Prepared:
    path = Path(path)
    if not path.is_file():
        raise ImageError("image file does not exist")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ImageError("image file is too large to read")
    try:
        with Image.open(path) as img:
            if img.width * img.height > MAX_INPUT_PIXELS:
                raise ImageError("image has too many pixels")
            if getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1:
                raise ImageError("animated images do not play in Teams; send a still image")
            img.load()
            img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ImageError("file is not a readable image") from error
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    stem = path.stem or "image"
    png = _encode_png(img.convert("RGBA" if alpha else "RGB"))
    if len(png) <= max_bytes:
        return Prepared(stem + ".png", "image/png", png, img.width, img.height)
    # Too big as PNG (usually a photo): JPEG on white, lowering quality, then size.
    flat = img.convert("RGBA")
    background = Image.new("RGB", flat.size, "white")
    background.paste(flat, mask=flat.getchannel("A"))
    while True:
        for quality in (85, 75, 65, 55):
            data = _encode_jpeg(background, quality)
            if len(data) <= max_bytes:
                return Prepared(stem + ".jpg", "image/jpeg", data, background.width, background.height)
        if min(background.size) <= 64:
            raise ImageError("image cannot be made small enough for Teams")
        background = background.resize((max(1, background.width * 3 // 4), max(1, background.height * 3 // 4)), Image.LANCZOS)


def _encode_png(img) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _encode_jpeg(img, quality) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()
