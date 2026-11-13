"""BMP: the file size is in the header. The 2-byte signature is too weak to scan for."""

from __future__ import annotations

import struct

from .base import Found

EXT = "bmp"
SCAN_PATTERNS = ()


def probe(buf, pos, end):
    if buf[pos:pos + 2] != b"BM" or pos + 30 > end:
        return None
    size, reserved, offset, dib = struct.unpack_from("<IIII", buf, pos + 2)
    if reserved != 0 or dib not in (12, 40, 52, 56, 64, 108, 124):
        return None
    if offset < 14 + dib or size < offset or pos + size > end:
        return None
    return Found("BMP image", EXT, size)
