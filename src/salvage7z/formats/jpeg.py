"""JPEG: walk the marker segments, then the entropy-coded scans, up to the EOI marker."""

from __future__ import annotations

import struct

from .base import Found

EXT = "jpg"
SCAN_PATTERNS = (rb"\xff\xd8\xff[\xdb\xe0\xe1\xe2\xed\xee\xfe]",)


def probe(buf, pos, end):
    if buf[pos:pos + 3] != b"\xff\xd8\xff":
        return None
    q = pos + 2
    while q + 2 <= end:
        if buf[q] != 0xFF:
            return None
        marker = buf[q + 1]
        if marker == 0xFF:                      # fill byte
            q += 1
            continue
        if marker == 0xD9:                      # EOI
            return Found("JPEG image", EXT, q + 2 - pos)
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            q += 2
            continue
        if q + 4 > end:
            return None
        seg = struct.unpack_from(">H", buf, q + 2)[0]
        if seg < 2:
            return None
        q += 2 + seg
        if marker == 0xDA:                      # start of scan: skip entropy-coded data
            while True:
                q = buf.find(b"\xff", q, end)
                if q < 0 or q + 1 >= end:
                    return None
                nxt = buf[q + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    q += 2
                elif nxt == 0xFF:
                    q += 1
                else:
                    break
    return None
