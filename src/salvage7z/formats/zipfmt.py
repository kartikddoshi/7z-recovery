"""ZIP and ZIP-based documents (docx, xlsx, pptx, odt, ...).

The end is found from the "end of central directory" record: it is the right one when
its central-directory offset and size add up to its own position. ZIP64 is not handled.
"""

from __future__ import annotations

import struct

from .base import Found

EXT = "zip"
SCAN_PATTERNS = (rb"PK\x03\x04[\x0a\x14\x2d\x3f]\x00",)
_METHODS = {0, 1, 6, 8, 9, 12, 14, 93, 95, 98, 99}


def _document_type(names):
    s = set(names)
    if "[Content_Types].xml" in s:
        for prefix, kind, ext in (("word/", "Word document", "docx"), ("xl/", "Excel workbook", "xlsx"),
                                  ("ppt/", "PowerPoint presentation", "pptx")):
            if any(n.startswith(prefix) for n in s):
                return kind, ext
    if "mimetype" in s and "content.xml" in s:
        return "OpenDocument file", "odf"
    if "META-INF/MANIFEST.MF" in s:
        return "Java archive", "jar"
    return "ZIP archive", EXT


def probe(buf, pos, end):
    if buf[pos:pos + 4] != b"PK\x03\x04" or pos + 30 > end:
        return None
    _ver, _flags, method = struct.unpack_from("<HHH", buf, pos + 4)
    name_len = struct.unpack_from("<H", buf, pos + 26)[0]
    if method not in _METHODS or not (0 < name_len <= 1024):
        return None
    q = pos
    while True:
        e = buf.find(b"PK\x05\x06", q, end)
        if e < 0 or e + 22 > end:
            return None
        cd_size, cd_offset, comment_len = struct.unpack_from("<IIH", buf, e + 12)
        if pos + cd_offset + cd_size == e and buf[pos + cd_offset:pos + cd_offset + 4] == b"PK\x01\x02":
            names = []
            c = pos + cd_offset
            while c + 46 <= e and buf[c:c + 4] == b"PK\x01\x02" and len(names) < 5000:
                fl, el, cl = struct.unpack_from("<HHH", buf, c + 28)
                names.append(bytes(buf[c + 46:c + 46 + fl]).decode("utf-8", "replace"))
                c += 46 + fl + el + cl
            kind, ext = _document_type(names)
            length = e + 22 + comment_len - pos
            if pos + length > end:
                return None
            return Found(kind, ext, length, info={"entries": len(names), "first_entries": names[:5]})
        q = e + 4
