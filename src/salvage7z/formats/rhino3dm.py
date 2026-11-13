"""Rhino / openNURBS .3dm models.

A .3dm file is a 32-byte header ("3D Geometry File Format" + right-aligned version)
followed by top-level chunks: 4-byte type code, then a length (4 bytes up to version 4,
8 bytes from version 50). The last chunk (type 0x00007FFF) stores the total file length,
which makes the end exact and self-checking.
"""

from __future__ import annotations

import calendar
import re
import struct
import zlib

from .base import Found

EXT = "3dm"
MAGIC = b"3D Geometry File Format "
SCAN_PATTERNS = (re.escape(MAGIC),)

TCODE_ENDOFFILE = 0x00007FFF
TCODE_ENDOFTABLE = 0xFFFFFFFF
TCODE_PROPERTIES_TABLE = 0x10000014
TCODE_OBJECT_TABLE = 0x10000013
TCODE_OBJECT_RECORD = 0x20008070
TCODE_SHORT = 0x80000000
_TOP_LEVEL = {0x00000001, TCODE_ENDOFFILE, 0x00007FFE} | {0x10000010 + i for i in range(0x30)}


def _version(buf, pos):
    try:
        return int(bytes(buf[pos + 24:pos + 32]).decode("ascii").strip())
    except ValueError:
        return None


def probe(buf, pos, end):
    if buf[pos:pos + 24] != MAGIC or pos + 32 > end:
        return None
    ver = _version(buf, pos)
    if ver is None or ver < 2:          # version 1 files use a different layout
        return None
    big = ver >= 50
    hl = 12 if big else 8
    fmt = "<q" if big else "<i"
    q = pos + 32
    open_table = False
    nrec = 0
    info = {"version": ver}
    while q + hl <= end:
        tcode = struct.unpack_from("<I", buf, q)[0]
        val = struct.unpack_from(fmt, buf, q + 4)[0]
        if tcode not in _TOP_LEVEL:
            if open_table and tcode == TCODE_OBJECT_RECORD:
                # Object table whose length was never filled in: the save that produced
                # this file did not finish. Walk the records that are there.
                while q + hl <= end:
                    t2 = struct.unpack_from("<I", buf, q)[0]
                    v2 = struct.unpack_from(fmt, buf, q + 4)[0]
                    if t2 != TCODE_OBJECT_RECORD or v2 < 0 or q + hl + v2 > end:
                        break
                    q += hl + v2
                    nrec += 1
                if q + hl <= end and struct.unpack_from("<I", buf, q)[0] == TCODE_ENDOFTABLE:
                    q += hl
                    open_table = False
                    continue
                return Found("Rhino 3D model", EXT, q - pos, complete=False, absorb_unknown=True, info=info,
                             note=f"the original save was never finished: object table left open after {nrec} objects")
            return None
        q += hl
        if tcode == TCODE_ENDOFFILE:
            if val not in (4, 8) or q + val > end:
                break
            stated = struct.unpack_from("<Q" if val == 8 else "<I", buf, q)[0]
            q += val
            if stated != q - pos:
                return Found("Rhino 3D model", EXT, q - pos, complete=False, info=info,
                             note=f"end marker says {stated:,} bytes but {q - pos:,} were walked")
            return Found("Rhino 3D model", EXT, q - pos, info=info)
        if not tcode & TCODE_SHORT:
            if val < 0:
                return None
            if q + val > end:
                break
            q += val
            open_table = tcode == TCODE_OBJECT_TABLE and val == 0
    return Found("Rhino 3D model", EXT, end - pos, complete=False, info=info,
                 note="cut off: the data ends before the file's end marker")


# ---------------------------------------------------------------------------
# metadata (document properties) and the embedded thumbnail
# ---------------------------------------------------------------------------

def _string(buf, q, end):
    n = struct.unpack_from("<I", buf, q)[0]
    q += 4
    if n == 0:
        return "", q
    if n > 100000 or q + 2 * n > end:
        raise ValueError("string")
    return bytes(buf[q:q + 2 * n]).decode("utf-16le", "replace").rstrip("\0"), q + 2 * n


def _time(buf, q):
    sec, mi, hr, md, mon, yr, _wd, _yd = struct.unpack_from("<8i", buf, q)
    if not (70 <= yr <= 200 and 0 <= mon < 12 and 1 <= md <= 31):
        return None, q + 32
    try:
        return calendar.timegm((yr + 1900, mon + 1, md, hr, mi, sec)), q + 32
    except (ValueError, OverflowError):
        return None, q + 32


def properties(buf, pos, length, ver):
    """Read created/edited info, notes and the thumbnail location from the properties table."""
    big = ver >= 50
    hl = 12 if big else 8
    fmt = "<q" if big else "<i"
    q = pos + 32
    end = pos + length
    info = {}
    while q + hl <= end:
        t = struct.unpack_from("<I", buf, q)[0]
        v = struct.unpack_from(fmt, buf, q + 4)[0]
        body = q + hl
        if t == TCODE_PROPERTIES_TABLE:
            e = min(end, body + v)
            s = body
            while s + hl <= e:
                st = struct.unpack_from("<I", buf, s)[0]
                sv = struct.unpack_from(fmt, buf, s + 4)[0]
                sb = s + hl
                if st == TCODE_ENDOFTABLE:
                    break
                if st & TCODE_SHORT:
                    s = sb
                    continue
                if sv < 0 or sb + sv > e:
                    break
                try:
                    if st == 0x20008021:        # revision history
                        x = sb + 1
                        info["created_by"], x = _string(buf, x, sb + sv)
                        info["created"], x = _time(buf, x)
                        info["edited_by"], x = _string(buf, x, sb + sv)
                        info["edited"], x = _time(buf, x)
                        info["revisions"] = struct.unpack_from("<i", buf, x)[0]
                    elif st == 0x20008022:      # notes
                        info["notes"], _ = _string(buf, sb + 5, sb + sv)
                    elif st == 0x20008024:      # application that wrote the file
                        info["application"], _ = _string(buf, sb + 1, sb + sv)
                    elif st in (0x20008023, 0x20008025):
                        info["_preview"] = (st, sb, sv)
                except (struct.error, ValueError):
                    pass
                s = sb + sv
            break
        if t & TCODE_SHORT:
            q = body
        elif v < 0:
            break
        else:
            q = body + v
    return info


def describe(buf, pos, found):
    """Fill found.info with document properties; called by the splitter after probe()."""
    props = properties(buf, pos, found.length, found.info.get("version", 4))
    for k in ("created_by", "edited_by", "revisions", "notes", "application", "created", "edited", "_preview"):
        if props.get(k) not in (None, ""):
            found.info[k] = props[k]
    stamp = props.get("edited") or props.get("created")
    if stamp:
        found.info["mtime"] = stamp


def preview_bmp(buf, preview, big):
    """Turn the embedded thumbnail (a Windows DIB, optionally zlib-packed) into a .bmp file."""
    st, sb, _sv = preview
    hdr = bytes(buf[sb:sb + 40])
    size, w, h, _planes, bpp, _comp, _simg, _xp, _yp, used, _imp = struct.unpack("<IiiHHIIiiII", hdr)
    if size != 40 or not (0 < w <= 4096) or not (0 < abs(h) <= 4096) or bpp not in (8, 24, 32):
        return None
    pal = (used or (256 if bpp == 8 else 0)) * 4
    need = pal + ((w * bpp + 31) // 32) * 4 * abs(h)
    if st == 0x20008023:
        bits = bytes(buf[sb + 40:sb + 40 + need])
    else:
        x = sb + 40
        x += 8 if big else 4            # unpacked size
        x += 4                          # CRC
        method = buf[x]
        x += 1
        if method == 0:
            bits = bytes(buf[x:x + need])
        else:
            hl = 12 if big else 8
            clen = struct.unpack_from("<q" if big else "<i", buf, x + 4)[0]
            bits = zlib.decompressobj().decompress(bytes(buf[x + hl:x + hl + clen]))
    if len(bits) < need:
        return None
    return b"BM" + struct.pack("<IHHI", 54 + len(bits), 0, 0, 54 + pal) + hdr + bits


def sidecars(buf, pos, found):
    preview = found.info.get("_preview")
    if not preview:
        return []
    try:
        bmp = preview_bmp(buf, preview, found.info.get("version", 4) >= 50)
    except (struct.error, zlib.error, IndexError):
        bmp = None
    return [("preview.bmp", bmp)] if bmp else []
