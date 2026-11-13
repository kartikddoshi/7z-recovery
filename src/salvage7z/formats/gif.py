"""GIF: header, optional colour table, then blocks up to the 0x3B trailer."""

from __future__ import annotations

from .base import Found

EXT = "gif"
SCAN_PATTERNS = (rb"GIF8[79]a",)


def _skip_sub_blocks(buf, q, end):
    while q < end:
        n = buf[q]
        q += 1 + n
        if n == 0:
            return q
    return None


def probe(buf, pos, end):
    if buf[pos:pos + 6] not in (b"GIF87a", b"GIF89a") or pos + 13 > end:
        return None
    flags = buf[pos + 10]
    q = pos + 13
    if flags & 0x80:
        q += 3 * (2 << (flags & 7))
    while q is not None and q < end:
        b = buf[q]
        if b == 0x3B:
            return Found("GIF image", EXT, q + 1 - pos)
        if b == 0x21:                           # extension: label byte, then sub-blocks
            q = _skip_sub_blocks(buf, q + 2, end)
        elif b == 0x2C:                         # image descriptor
            if q + 10 > end:
                return None
            f = buf[q + 9]
            q += 10
            if f & 0x80:
                q += 3 * (2 << (f & 7))
            q = _skip_sub_blocks(buf, q + 1, end)   # +1: LZW minimum code size
        else:
            return None
    return None
