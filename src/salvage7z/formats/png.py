"""PNG: 8-byte signature, then length-prefixed chunks up to IEND."""

from __future__ import annotations

import re
import struct

from .base import Found

EXT = "png"
MAGIC = b"\x89PNG\r\n\x1a\n"
SCAN_PATTERNS = (re.escape(MAGIC),)


def probe(buf, pos, end):
    if buf[pos:pos + 8] != MAGIC:
        return None
    q = pos + 8
    first = True
    while q + 12 <= end:
        length, kind = struct.unpack_from(">I4s", buf, q)
        if length > 0x7FFFFFFF or not kind.isalpha() or (first and kind != b"IHDR"):
            return None
        first = False
        if q + 12 + length > end:
            break
        q += 12 + length
        if kind == b"IEND":
            return Found("PNG image", EXT, q - pos)
    if first:
        return None
    return Found("PNG image", EXT, end - pos, complete=False, note="cut off before the IEND chunk")
