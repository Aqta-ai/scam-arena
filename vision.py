"""Transcribe a screenshot with a configured NVIDIA vision model on Nebius.

Images are validated in memory and are never saved by the server.
"""
import base64
import binascii
from io import BytesIO
import os
import re
import warnings

import httpx
from PIL import Image, UnidentifiedImageError

import arena


VL_MODEL = os.environ.get("SCAM_ARENA_VL_MODEL", "nvidia/Nemotron-3.5-Super-VL")
MAX_BYTES = 2_500_000
MAX_SIDE = 1600
MAX_PIXELS = MAX_SIDE * MAX_SIDE
KINDS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
MAX_BASE64 = 4 * ((MAX_BYTES + 2) // 3)


class VisionResponseError(ValueError):
    """The provider did not return a complete usable transcription."""


class ImageInputError(ValueError):
    """A safe, actionable image-validation explanation."""


def check(data_url):
    if not isinstance(data_url, str):
        raise ValueError("Upload a PNG, JPEG or WebP screenshot.")
    if len(data_url) > MAX_BASE64 + 64:
        raise ValueError("That image is over 2.5 MB. Crop it to the message.")
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)", data_url)
    if not match:
        raise ValueError("Upload a PNG, JPEG or WebP screenshot.")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("That screenshot could not be decoded.") from exc
    if len(raw) > MAX_BYTES:
        raise ValueError("That image is over 2.5 MB. Crop it to the message.")
    mime = match.group(1)
    valid_magic = ((mime == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n"))
                   or (mime == "image/jpeg" and raw.startswith(b"\xff\xd8\xff"))
                   or (mime == "image/webp" and len(raw) >= 12
                       and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))
    if not valid_magic:
        raise ValueError("That file is not the image type it claims to be.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=("PNG", "JPEG", "WEBP")) as image:
                if image.format != KINDS[mime]:
                    raise ImageInputError("That file is not the image type it claims to be.")
                width, height = image.size
                if min(width, height) < 1 or max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
                    raise ImageInputError("Keep the screenshot to 1,600 pixels per side. Crop it to the message.")
                if getattr(image, "n_frames", 1) != 1:
                    raise ImageInputError("Upload a still screenshot, not an animated image.")
                image.verify()
            # verify() checks PNG structure; a separate load() also catches
            # truncated JPEG data before any image is sent to the provider.
            with Image.open(BytesIO(raw), formats=("PNG", "JPEG", "WEBP")) as image:
                image.load()
    except ImageInputError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("That screenshot could not be read. Try a complete PNG, JPEG or WebP image.") from exc
    return data_url


def read(data_url):
    body = {"model": VL_MODEL, "temperature": 0.0, "max_tokens": 1200, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "This is a screenshot of a message someone received. Transcribe the message text exactly as written, including the sender name if shown. Reply with the message text only, no commentary."},
        {"type": "image_url", "image_url": {"url": check(data_url)}},
    ]}]}
    response = httpx.post(f"{arena.BASE_URL}/chat/completions",
                          headers={"Authorization": f"Bearer {os.environ['NEBIUS_API_KEY']}"}, json=body, timeout=90)
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") if isinstance(data, dict) else None
    if (not isinstance(choices, list) or not choices or not isinstance(choices[0], dict)
            or choices[0].get("finish_reason") != "stop"):
        raise VisionResponseError("The screenshot transcription did not finish. Try a tighter crop.")
    message = choices[0].get("message")
    if (not isinstance(message, dict) or message.get("tool_calls") or message.get("function_call")
            or message.get("refusal") or not isinstance(message.get("content"), str)):
        raise VisionResponseError("The screenshot did not return text. Try a clearer crop.")
    text = arena.clean(message["content"])
    if not text or len(text) > 4000:
        raise VisionResponseError("The screenshot text is empty or too long. Try a tighter crop.")
    return {"text": text, "model": VL_MODEL, "provider": "Nebius Token Factory"}
