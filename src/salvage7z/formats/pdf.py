"""PDF: ends at an %%EOF marker.

A PDF saved several times has several %%EOF markers, so the end is the first %%EOF that
is followed by the start of another recognisable file (or by the end of the data). If
that cannot be decided, the first plausible %%EOF is used and the result is flagged.
"""

from __future__ import annotations

from .base import Found

EXT = "pdf"
SCAN_PATTERNS = (rb"%PDF-[12]\.[0-9]",)
_LOOKAHEAD = 64 << 20


def probe(buf, pos, end):
    from . import SCAN, looks_like_start        # late import: the registry imports this module

    if buf[pos:pos + 5] != b"%PDF-":
        return None
    q = pos
    while True:
        e = buf.find(b"%%EOF", q, end)
        if e < 0:
            return None
        stop = e + 5
        while stop < end and buf[stop] in (10, 13):
            stop += 1
        if stop >= end or looks_like_start(buf, stop, end):
            return Found("PDF document", EXT, stop - pos)
        if looks_like_start(buf, e + 5, end):
            return Found("PDF document", EXT, e + 5 - pos)
        more = buf.find(b"%%EOF", stop, min(end, stop + _LOOKAHEAD))
        other = SCAN.search(buf, stop, min(end, stop + _LOOKAHEAD))
        if more < 0 or (other is not None and other.start() < more):
            return Found("PDF document", EXT, stop - pos, complete=False,
                         note="end guessed: the bytes after %%EOF are not a file type this tool knows")
        q = stop
