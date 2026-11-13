"""OLE2 / Compound File Binary: legacy .doc, .xls, .ppt, .msg, Thumbs.db ...

The file is a 512-byte header followed by fixed-size sectors. The FAT says which sectors
are in use; the file ends after the last used one.
"""

from __future__ import annotations

import re
import struct

from .base import Found

EXT = "ole"
MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
SCAN_PATTERNS = (re.escape(MAGIC),)
_FREE = 0xFFFFFFFF
_END_OF_CHAIN = 0xFFFFFFFA      # this and above are special values


def _utf16(s):
    return s.encode("utf-16le")


_STREAM_NAMES = (
    (_utf16("WordDocument"), "Word document (legacy)", "doc"),
    (_utf16("Workbook"), "Excel workbook (legacy)", "xls"),
    (_utf16("Book"), "Excel workbook (legacy)", "xls"),
    (_utf16("PowerPoint Document"), "PowerPoint presentation (legacy)", "ppt"),
    (_utf16("__substg1.0_"), "Outlook message", "msg"),
)


def probe(buf, pos, end):
    if buf[pos:pos + 8] != MAGIC or pos + 512 > end:
        return None
    shift = struct.unpack_from("<H", buf, pos + 30)[0]
    if shift not in (9, 12):
        return None
    ss = 1 << shift
    per = ss // 4
    n_fat = struct.unpack_from("<I", buf, pos + 44)[0]
    difat = list(struct.unpack_from("<109I", buf, pos + 76))
    nxt = struct.unpack_from("<I", buf, pos + 68)[0]
    guard = 0
    while nxt < _END_OF_CHAIN and guard < 100000:
        o = pos + (nxt + 1) * ss
        if o + ss > end:
            return None
        blk = struct.unpack_from(f"<{per}I", buf, o)
        difat += blk[:-1]
        nxt = blk[-1]
        guard += 1
    last = -1
    index = 0
    for fat_sector in difat[:n_fat]:
        if fat_sector >= _END_OF_CHAIN:
            break
        o = pos + (fat_sector + 1) * ss
        if o + ss > end:
            return None
        for j, v in enumerate(struct.unpack_from(f"<{per}I", buf, o)):
            if v != _FREE:
                last = index + j
        index += per
    if last < 0:
        return None
    length = (last + 2) * ss
    if pos + length > end:
        return None
    kind, ext = "OLE2 compound file", EXT
    body = bytes(buf[pos:pos + min(length, 32 << 20)])
    for name, k, e in _STREAM_NAMES:
        if name in body:
            kind, ext = k, e
            break
    return Found(kind, ext, length)
