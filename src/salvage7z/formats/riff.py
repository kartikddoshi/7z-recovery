"""RIFF containers (WAV, AVI, WebP, CorelDRAW): total size is in the header."""

from __future__ import annotations

import struct

from .base import Found

EXT = "riff"
_KINDS = {b"WAVE": ("WAV audio", "wav"), b"AVI ": ("AVI video", "avi"), b"WEBP": ("WebP image", "webp")}
SCAN_PATTERNS = (rb"RIFF.{4}(?:WAVE|AVI |WEBP|CDR[0-9A-Zv])",)


def probe(buf, pos, end):
    if buf[pos:pos + 4] != b"RIFF" or pos + 12 > end:
        return None
    size = struct.unpack_from("<I", buf, pos + 4)[0]
    form = bytes(buf[pos + 8:pos + 12])
    if form in _KINDS:
        kind, ext = _KINDS[form]
    elif form[:3] == b"CDR":
        kind, ext = "CorelDRAW drawing", "cdr"
    else:
        return None
    length = 8 + size + (size & 1)
    if size < 4 or pos + 8 + size > end:
        return None
    return Found(kind, ext, min(length, end - pos))
