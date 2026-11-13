"""Cut a decoded solid block back into individual files.

Files in a solid block are simply concatenated. Starting at offset 0, ask every format
module "is this yours, and how long is it?", jump that far, and repeat. When nothing
recognises the bytes, search forward for the next position where a format module can
verify a file, and keep the bytes in between as one unidentified piece.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import time
from dataclasses import dataclass
from typing import Optional

from . import formats
from .formats import Found

SLICE = 16 << 20


@dataclass
class Item:
    offset: int
    found: Found
    identified: bool = True


def _next_start(buf, pos: int, end: int) -> int:
    q = pos
    while True:
        m = formats.SCAN.search(buf, q, end)
        if m is None:
            return end
        q = m.start()
        if formats.probe(buf, q, end) is not None:
            return q
        q += 1


def _looks_like_text(buf, pos: int, length: int) -> bool:
    sample = bytes(buf[pos:pos + min(length, 65536)])
    if not sample or b"\0" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.start < len(sample) - 4:       # a multi-byte character cut by the sample edge is fine
            return False
    control = sum(1 for c in sample if c < 32 and c not in (9, 10, 13))
    return control == 0


def split_buffer(buf, end: int, starts_mid_file: bool = False, truncated: bool = False) -> list:
    """Return the Items found in buf[0:end], in order.

    Together they cover every byte, except the few a format asks to drop after a cut-off
    file (Found.discard_after).
    """
    items: list = []
    p = 0
    while p < end:
        r = formats.probe(buf, p, end)
        if r is None:
            w = formats.weak_probe(buf, p, end)
            if w is not None:
                nxt = p + w.length
                if nxt == end or formats.probe(buf, nxt, end) is not None or formats.weak_probe(buf, nxt, end) is not None:
                    r = w
        if r is not None:
            items.append(Item(p, r))
            p += r.length + r.discard_after
            continue
        q = _next_start(buf, p + 1, end)
        if items and items[-1].found.absorb_unknown:
            items[-1].found.length += q - p
        else:
            items.append(Item(p, Found("unidentified data", "bin", q - p, complete=False), identified=False))
        p = q
    for i, it in enumerate(items):
        f = it.found
        if not it.identified:
            if _looks_like_text(buf, it.offset, f.length):
                f.kind, f.ext = "text", "txt"
            if i == 0 and starts_mid_file:
                f.note = "end of a file whose beginning was in the damaged part of the archive"
            elif i == len(items) - 1 and truncated:
                f.note = "start of a file that continues into the damaged part of the archive"
            else:
                f.note = "no known file type starts here; may be one file or several"
        else:
            formats.describe(buf, it.offset, f)
    return items


def _status(it: Item) -> str:
    if not it.identified:
        return "unidentified"
    return "complete" if it.found.complete else "partial"


def write_items(buf, items: list, outdir: str, prefix: str, source: str, previews: bool = False) -> list:
    """Write each Item to outdir/files and return manifest records."""
    fdir = os.path.join(outdir, "files")
    os.makedirs(fdir, exist_ok=True)
    records = []
    for n, it in enumerate(items, 1):
        f = it.found
        fid = f"{prefix}-{n:04d}"
        name = f"{fid}.{f.ext}"
        path = os.path.join(fdir, name)
        h = hashlib.sha256()
        with open(path, "wb") as out:
            done = 0
            while done < f.length:
                chunk = buf[it.offset + done: it.offset + min(f.length, done + SLICE)]
                for off, data in f.patches:
                    if done <= off and off + len(data) <= done + len(chunk):
                        chunk = chunk[:off - done] + data + chunk[off - done + len(data):]
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
        mtime = f.info.get("mtime")
        if mtime:
            try:
                os.utime(path, (mtime, mtime))
            except (OSError, OverflowError, ValueError):
                pass
        if previews and it.identified:
            for suffix, data in formats.sidecars(buf, it.offset, f):
                pdir = os.path.join(outdir, "previews")
                os.makedirs(pdir, exist_ok=True)
                with open(os.path.join(pdir, f"{fid}.{suffix}"), "wb") as o:
                    o.write(data)
        info = {k: v for k, v in f.info.items() if not k.startswith("_") and k != "mtime"}
        for k in ("created", "edited"):
            if isinstance(info.get(k), int):
                info[k] = _iso(info[k])
        records.append({
            "id": fid, "file": name, "kind": f.kind, "status": _status(it), "bytes": f.length,
            "source": source, "offset": it.offset, "modified": _iso(mtime) if mtime else "",
            "note": f.note, "info": info, "sha256": h.hexdigest(),
        })
    return records


def _iso(t: Optional[int]) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) if t else ""


def split_file(path: str, outdir: str, prefix: str, starts_mid_file: bool = False, truncated: bool = False,
               previews: bool = False) -> list:
    """Split one file of decoded data into outdir/files. Returns manifest records."""
    size = os.path.getsize(path)
    if size == 0:
        return []
    with open(path, "rb") as fh:
        buf = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            items = split_buffer(buf, size, starts_mid_file, truncated)
            return write_items(buf, items, outdir, prefix, os.path.basename(path), previews)
        finally:
            buf.close()


MANIFEST_COLUMNS = ["id", "file", "kind", "status", "bytes", "modified", "note", "source", "offset", "sha256", "info"]


def write_manifest(outdir: str, records: list) -> None:
    import csv

    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as o:
        json.dump(records, o, indent=1)
    with open(os.path.join(outdir, "manifest.csv"), "w", newline="", encoding="utf-8-sig") as o:
        w = csv.writer(o)
        w.writerow(MANIFEST_COLUMNS)
        for r in records:
            w.writerow([json.dumps(r[c], ensure_ascii=False) if c == "info" else r.get(c, "") for c in MANIFEST_COLUMNS])
