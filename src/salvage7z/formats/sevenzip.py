"""A .7z archive stored inside the damaged one: its own start header gives its length."""

from __future__ import annotations

import re
import struct
import zlib

from ..container import HEADER_SIZE, MAGIC
from .base import Found

EXT = "7z"
SCAN_PATTERNS = (re.escape(MAGIC),)


def probe(buf, pos, end):
    if buf[pos:pos + 6] != MAGIC or pos + HEADER_SIZE > end:
        return None
    if zlib.crc32(bytes(buf[pos + 12:pos + 32])) != struct.unpack_from("<I", buf, pos + 8)[0]:
        return None
    offset, size = struct.unpack_from("<QQ", buf, pos + 12)
    length = HEADER_SIZE + offset + size
    if pos + length > end:
        return Found("7z archive", EXT, end - pos, complete=False,
                     note=f"cut off: {end - pos:,} of {length:,} bytes")
    return Found("7z archive", EXT, length)
