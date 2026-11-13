"""File-format modules.

Inside a solid block, files sit back to back with no names and no separators. The only
way to find where one ends is to understand its format. Each module here knows one
format and exposes:

    SCAN_PATTERNS   tuple of bytes regexes that mark a likely start of this format.
                    Used to look for the next file after bytes nobody recognised.
                    Leave empty if the signature is too short to be trusted.
    probe(buf, pos, end) -> Found | None
                    Is there a file of this format at `pos`? If so, how long is it?
                    Must not read at or beyond `end`. May raise struct.error/IndexError/
                    ValueError on malformed data; that counts as "no".
    weak_probe(buf, pos, end) -> Found | None        (optional)
                    For formats with no signature. Only tried when nothing else matched,
                    and only believed if a recognisable file follows.
    describe(buf, pos, found)                        (optional, needs EXT)
                    Add metadata to found.info. A unix time under "mtime" becomes the
                    recovered file's date. Keys starting with "_" stay out of the manifest.
    sidecars(buf, pos, found) -> [(suffix, bytes)]   (optional, needs EXT)
                    Extra files worth saving next to the recovered one (e.g. a thumbnail).

To add a format: write a module like the others, add it to MODULES, add a test.
Order matters only when two formats share a signature; put the stricter one first.
"""

from __future__ import annotations

import re
import struct
from typing import Optional

from . import bmp, gif, jpeg, ole, pdf, png, rhino3dm, riff, sevenzip, stl, zipfmt
from .base import Found

MODULES = [rhino3dm, stl, sevenzip, jpeg, png, pdf, zipfmt, ole, gif, riff, bmp]

SCAN = re.compile(b"|".join(b"(?:" + p + b")" for m in MODULES for p in m.SCAN_PATTERNS), re.DOTALL)

_ERRORS = (struct.error, IndexError, ValueError)


def probe(buf, pos: int, end: int) -> Optional[Found]:
    for m in MODULES:
        try:
            r = m.probe(buf, pos, end)
        except _ERRORS:
            r = None
        if r is not None and r.length > 0:
            return r
    return None


def weak_probe(buf, pos: int, end: int) -> Optional[Found]:
    for m in MODULES:
        f = getattr(m, "weak_probe", None)
        if f is None:
            continue
        try:
            r = f(buf, pos, end)
        except _ERRORS:
            r = None
        if r is not None and r.length > 0:
            return r
    return None


def looks_like_start(buf, pos: int, end: int) -> bool:
    """Cheap test: does a known signature begin at `pos`?"""
    m = SCAN.match(buf, pos, end)
    return m is not None


def _hook(name: str, found: Found):
    for m in MODULES:
        f = getattr(m, name, None)
        if f is not None and getattr(m, "EXT", None) == found.ext:
            return f
    return None


def describe(buf, pos: int, found: Found) -> None:
    """Let the format add metadata to found.info (optional module hook `describe`)."""
    f = _hook("describe", found)
    if f is not None:
        try:
            f(buf, pos, found)
        except _ERRORS:
            pass


def sidecars(buf, pos: int, found: Found) -> list:
    f = _hook("sidecars", found)
    if f is None:
        return []
    try:
        return f(buf, pos, found) or []
    except _ERRORS:
        return []


__all__ = ["Found", "MODULES", "SCAN", "probe", "weak_probe", "looks_like_start", "describe", "sidecars"]
