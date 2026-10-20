# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Images and documents in chats and tool results, in every app's shape (D-046).

A screenshot pasted into a chat, or one a browser tool returned, is evidence the next
model may need. It is stored whole (the operator's decision) as a session blob and
shown in the brief as a placeholder (``[image #12: image/png 1280x800]``); a multimodal
model opens it on demand (``coding_view_image``). Nothing reads or describes the image:
there is no model inside Continuum.

Recognised parts:

* Anthropic / Claude Code: ``{"type": "image" | "document", "source": {"type": "base64",
  "media_type", "data"}}``
* MCP tool results: ``{"type": "image", "data", "mimeType"}``
* OpenAI / Codex: ``{"type": "input_image", "image_url": "data:...;base64,..."}`` and
  ``{"type": "image_url", "image_url": {"url": "data:..."}}``
* Gemini: ``{"inlineData": {"mimeType", "data"}}``

Standard library only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
from typing import Any

MAX_MEDIA_BYTES = 10 << 20        # larger images are recorded, not stored
MAX_PARTS = 20                    # per message or tool result
_DATA_URL = re.compile(r"^data:([\w./+-]+);base64,(.*)$", re.S)


def is_media(part: Any) -> bool:
    return isinstance(part, dict) and _one(part) is not None


def _one(part: dict[str, Any]) -> dict[str, str] | None:
    kind = part.get("type")
    source = part.get("source")
    if kind in ("image", "document") and isinstance(source, dict) \
            and source.get("type") == "base64" and isinstance(source.get("data"), str):
        return {"media_type": str(source.get("media_type") or "application/octet-stream"),
                "data": source["data"]}
    if kind == "image" and isinstance(part.get("data"), str):                  # MCP
        return {"media_type": str(part.get("mimeType") or "image/png"), "data": part["data"]}
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if kind in ("input_image", "image_url") and isinstance(url, str):
        m = _DATA_URL.match(url)
        if m:
            return {"media_type": m.group(1), "data": m.group(2)}
    inline = part.get("inlineData") or part.get("inline_data")
    if isinstance(inline, dict) and isinstance(inline.get("data"), str):
        return {"media_type": str(inline.get("mimeType") or inline.get("mime_type")
                                  or "application/octet-stream"), "data": inline["data"]}
    return None


def find(value: Any, depth: int = 0) -> list[dict[str, str]]:
    """Every image/document part inside a message or tool result, in order."""
    if depth > 8:
        return []
    if isinstance(value, dict):
        found = _one(value)
        if found is not None:
            return [found]
        out: list[dict[str, str]] = []
        for v in value.values():
            if isinstance(v, dict | list):
                out += find(v, depth + 1)
        return out[:MAX_PARTS]
    if isinstance(value, list):
        out = []
        for v in value:
            out += find(v, depth + 1)
        return out[:MAX_PARTS]
    return []


def decode(part: dict[str, str]) -> bytes | None:
    """The raw bytes, or None when the data is not base64 or too large to store."""
    data = part.get("data", "")
    if len(data) > MAX_MEDIA_BYTES * 4 // 3 + 4:
        return None
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return None


def describe(part: dict[str, str]) -> dict[str, Any]:
    """Metadata that needs no model: type, size, SHA-256, pixel dimensions."""
    raw = decode(part)
    size = len(raw) if raw is not None else len(part.get("data", "")) * 3 // 4
    meta: dict[str, Any] = {"kind": "image" if part["media_type"].startswith("image/")
                            else "document", "media_type": part["media_type"][:80],
                            "bytes": size, "stored": raw is not None}
    if raw is not None:
        meta["sha256"] = hashlib.sha256(raw).hexdigest()
        dims = dimensions(raw)
        if dims:
            meta["width"], meta["height"] = dims
    return meta


def label(meta: dict[str, Any]) -> str:
    size = f" {meta['width']}x{meta['height']}" if meta.get("width") else ""
    where = f" #{meta['blob'][7:19]}" if meta.get("blob") else ""
    return f"[{meta['kind']}{where}: {meta['media_type']}{size}]"


def dimensions(raw: bytes) -> tuple[int, int] | None:
    """Width and height from the header of a PNG, GIF, JPEG or WebP file."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            w, h = struct.unpack(">II", raw[16:24])
            return int(w), int(h)
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", raw[6:10])
            return int(w), int(h)
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            chunk = raw[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(raw[24:27], "little") + 1
                h = int.from_bytes(raw[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", raw[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                bits = int.from_bytes(raw[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if raw[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(raw):
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                              0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    return w, h
                length = struct.unpack(">H", raw[i + 2:i + 4])[0]
                i += 2 + length
    except struct.error:
        return None
    return None
