"""STL meshes.

Binary STL: 80-byte free-text header, uint32 triangle count, then 50 bytes per triangle
(normal + 3 vertices as float32, 2 attribute bytes). Length is exactly 84 + 50 * count,
but there is no signature, so headers written by known tools are treated as strong
evidence and everything else has to pass a plausibility check on the triangles.

ASCII STL: text from "solid" to "endsolid".
"""

from __future__ import annotations

import math
import re
import struct

from .base import Found

EXT = "stl"
KNOWN_HEADERS = (b"Rhinoceros Binary STL",)
SCAN_PATTERNS = tuple(re.escape(h) for h in KNOWN_HEADERS)


def _triangles_plausible(buf, pos, count):
    step = max(1, count // 300)
    tested = unit = 0
    for i in range(0, count, step):
        v = struct.unpack_from("<12f", buf, pos + 84 + 50 * i)
        tested += 1
        if any((not math.isfinite(x)) or abs(x) > 1e8 for x in v):
            return False
        n2 = v[0] * v[0] + v[1] * v[1] + v[2] * v[2]
        if n2 < 1e-6 or 0.98 < n2 < 1.02:       # zero or unit normal
            unit += 1
    return unit >= 0.9 * tested


def _binary(buf, pos, end, known_header):
    if pos + 84 > end:
        return None
    count = struct.unpack_from("<I", buf, pos + 80)[0]
    length = 84 + 50 * count
    if count == 0:
        return None
    if pos + length > end:
        if not known_header:
            return None
        have = (end - pos - 84) // 50
        if have <= 0 or not _triangles_plausible(buf, pos, have):
            return None
        return Found("STL mesh", EXT, 84 + 50 * have, complete=False,
                     info={"triangles": have, "triangles_expected": count},
                     note=f"cut off: {have:,} of {count:,} triangles ({100 * have // count}%); "
                          "triangle count in the header was corrected so the file opens",
                     patches=((80, struct.pack("<I", have)),),
                     discard_after=end - pos - (84 + 50 * have))
    if not _triangles_plausible(buf, pos, count):
        return None
    return Found("STL mesh", EXT, length, info={"triangles": count})


def _ascii(buf, pos, end):
    if buf[pos:pos + 6] != b"solid ":
        return None
    head = bytes(buf[pos:min(end, pos + 1024)])
    if b"facet" not in head and b"endsolid" not in head:
        return None
    e = buf.find(b"endsolid", pos, end)
    if e < 0:
        return None
    nl = buf.find(b"\n", e, min(end, e + 300))
    stop = nl + 1 if nl >= 0 else min(end, e + 8)
    return Found("STL mesh (text)", EXT, stop - pos, info={"ascii": True})


def probe(buf, pos, end):
    head = bytes(buf[pos:pos + 32])
    if any(head.startswith(h) for h in KNOWN_HEADERS):
        return _binary(buf, pos, end, True)
    return _ascii(buf, pos, end)


def weak_probe(buf, pos, end):
    """Binary STL with an unknown header: believed only if the triangles look right."""
    return _binary(buf, pos, end, False)
