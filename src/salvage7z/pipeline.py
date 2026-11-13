"""The steps behind the command line: inspect, unpack, split, match."""

from __future__ import annotations

import collections
import hashlib
import json
import os
import shutil
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from typing import Callable, Optional

from . import container, lzma2, splitter

Log = Callable[[str], None]


def _quiet(_msg: str) -> None:
    pass


class SalvageError(Exception):
    """A problem the user can act on; shown without a traceback."""


def inspect(path: str, resync: bool = True, max_garbage: int = 64 * lzma2.MIB, log: Log = _quiet):
    """Diagnose the container and map the decodable LZMA2 streams. Reads only."""
    with open(path, "rb") as f:
        diag = container.diagnose(f)
        last = [0]

        def progress(pos: int) -> None:
            if pos - last[0] >= 2 << 30:
                last[0] = pos
                log(f"  scanned {pos / diag.file_size:.0%}")

        result = lzma2.scan(f, diag.file_size, start=container.HEADER_SIZE, limit=diag.packed_end,
                            resync=resync, max_garbage=max_garbage, progress=progress)
    return diag, result


def report(diag, result) -> list:
    out = diag.lines()
    streams, gaps = result.streams, result.gaps
    packed = sum(s.packed for s in streams)
    unpacked = sum(s.unpacked for s in streams)
    out.append(f"LZMA2 streams    : {len(streams)} found, {packed:,} bytes packed -> {unpacked:,} bytes unpacked")
    for s in streams[:50]:
        flags = ("" if s.complete else " INCOMPLETE") + (" (starts after damage)" if s.resynced else "")
        out.append(f"   #{s.index:<4} offset {s.start:>15,}  packed {s.packed:>14,}  unpacked {s.unpacked:>15,}  "
                   f"blocks {len(s.blocks):>4}{flags}")
    if len(streams) > 50:
        out.append(f"   ... and {len(streams) - 50} more")
    for g in gaps:
        if diag.end_header_state == "ok" and g.end == diag.packed_end and g.end - g.start < lzma2.MIB:
            out.append(f"not LZMA2        : {g.start:,} .. {g.end:,}  ({g.end - g.start:,} bytes; "
                       "most likely the archive's own packed index, not damage)")
            continue
        out.append(f"unreadable region: {g.start:,} .. {g.end:,}  ({g.end - g.start:,} bytes, {g.kind})")
    if result.gave_up_at is not None:
        out.append(f"search stopped at {result.gave_up_at:,}: too much unrecognisable data. "
                   "Raise --max-gap to keep looking.")
    if not streams:
        out.append("No LZMA2 data found. The archive may be encrypted, or packed with another method "
                   "(LZMA, PPMd, BZip2, Copy); this tool cannot help with those yet.")
    return out


def _unpack_one(args) -> list:
    path, size, stream, outdir = args
    stem = os.path.join(outdir, f"stream_{stream.index:04d}")
    with open(path, "rb") as f:
        segs = lzma2.decode_stream(f, size, stream, lambda part: f"{stem}.bin" if part == 0 else f"{stem}_part{part + 1}.bin")
    return segs


def unpack(path: str, streams: list, outdir: str, jobs: int = 1, log: Log = _quiet) -> list:
    """Decode every stream to outdir. Returns the Segments written, in archive order."""
    os.makedirs(outdir, exist_ok=True)
    size = os.path.getsize(path)
    tasks = [(path, size, s, outdir) for s in streams]
    segments: list = []
    if jobs <= 1 or len(tasks) <= 1:
        results = map(_unpack_one, tasks)
        for s, segs in zip(streams, results):
            segments += segs
            _log_stream(log, s, segs)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for s, segs in zip(streams, pool.map(_unpack_one, tasks)):
                segments += segs
                _log_stream(log, s, segs)
    return segments


def _log_stream(log: Log, stream, segs: list) -> None:
    got = sum(x.size for x in segs)
    extra = "" if got == stream.unpacked else f"  ({stream.unpacked - got:,} bytes lost to damage)"
    log(f"  stream #{stream.index}: {got:,} bytes{extra}")


def recover(archive: str, outdir: str, jobs: int = 1, split: bool = True, keep_streams: bool = False,
            previews: bool = False, resync: bool = True, max_garbage: int = 64 * lzma2.MIB,
            force: bool = False, log: Log = _quiet) -> dict:
    """Full run: inspect, unpack, split into files, write the manifest. Returns a summary dict."""
    log("Inspecting archive ...")
    diag, result = inspect(archive, resync, max_garbage, log)
    for line in report(diag, result):
        log(line)
    if not result.streams:
        raise SalvageError("nothing recoverable was found")
    os.makedirs(outdir, exist_ok=True)
    unpacked = sum(s.unpacked for s in result.streams)
    need = unpacked + (max(s.unpacked for s in result.streams) if split and not keep_streams else unpacked if split else 0)
    free = shutil.disk_usage(outdir).free
    if free < need and not force:
        raise SalvageError(f"not enough free space in {outdir}: about {need / 1e9:.1f} GB needed, "
                           f"{free / 1e9:.1f} GB free (use --force to try anyway)")
    with open(os.path.join(outdir, "scan.json"), "w", encoding="utf-8") as o:
        json.dump({"archive": os.path.basename(archive), "file_size": diag.file_size,
                   "end_header": diag.end_header_state,
                   "streams": [asdict(s) for s in result.streams], "gaps": [asdict(g) for g in result.gaps]}, o, indent=1)
    sdir = os.path.join(outdir, "streams")
    log(f"Unpacking {len(result.streams)} stream(s) with {jobs} process(es) ...")
    segments = unpack(archive, result.streams, sdir, jobs, log)
    summary = {"streams": len(result.streams), "segments": len(segments),
               "unpacked_bytes": sum(s.size for s in segments), "expected_bytes": unpacked, "files": 0, "by_kind": {}}
    if not split:
        return summary
    log("Splitting into files ...")
    records: list = []
    for seg in segments:
        prefix = f"s{seg.stream:04d}" + (f"p{seg.part + 1}" if seg.part else "")
        recs = splitter.split_file(seg.path, outdir, prefix, seg.starts_mid_file, seg.truncated, previews)
        records += recs
        log(f"  {os.path.basename(seg.path)}: {len(recs)} file(s)")
        if not keep_streams:
            os.remove(seg.path)
    if not keep_streams:
        try:
            os.rmdir(sdir)
        except OSError:
            pass
    splitter.write_manifest(outdir, records)
    counts = collections.Counter((r["kind"], r["status"]) for r in records)
    summary["files"] = len(records)
    summary["by_kind"] = {f"{k} ({s})": n for (k, s), n in sorted(counts.items())}
    return summary


def split_only(inputs: list, outdir: str, previews: bool = False, log: Log = _quiet) -> list:
    """Split already-decoded raw data (for example the stream 7-Zip's own recovery method leaves)."""
    os.makedirs(outdir, exist_ok=True)
    records: list = []
    for i, path in enumerate(inputs):
        recs = splitter.split_file(path, outdir, f"r{i:04d}", previews=previews)
        records += recs
        log(f"  {os.path.basename(path)}: {len(recs)} file(s)")
    splitter.write_manifest(outdir, records)
    return records


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(8 << 20)
            if not b:
                return h.hexdigest()
            h.update(b)


def match(outdir: str, search_dirs: list, rename: bool = False, log: Log = _quiet) -> list:
    """Find byte-identical copies of recovered files elsewhere, to get their names back.

    Compares by size first, then SHA-256. With rename=True the recovered file becomes
    "<id>__<original name>". Returns [(record, [paths])] for files that matched.
    """
    mpath = os.path.join(outdir, "manifest.json")
    if not os.path.exists(mpath):
        raise SalvageError(f"{mpath} not found; run `recover` or `split` first")
    with open(mpath, encoding="utf-8") as f:
        records = json.load(f)
    wanted = collections.defaultdict(list)
    for r in records:
        if r["status"] == "complete":
            wanted[r["bytes"]].append(r)
    own = os.path.abspath(outdir)
    candidates = collections.defaultdict(list)
    for root in search_dirs:
        for dp, dn, fn in os.walk(root):
            if os.path.abspath(dp).startswith(own):
                dn[:] = []
                continue
            for name in fn:
                p = os.path.join(dp, name)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size in wanted:
                    candidates[size].append(p)
    hits = []
    for size, paths in candidates.items():
        digests = {}
        for p in paths:
            try:
                digests[p] = _sha256(p)
            except OSError:
                pass
        for r in wanted[size]:
            same = sorted(p for p, d in digests.items() if d == r["sha256"])
            if same:
                r["identical_copies"] = same
                hits.append((r, same))
    hits.sort(key=lambda x: x[0]["id"])
    if rename:
        for r, same in hits:
            base = os.path.basename(same[0])
            new = f"{r['id']}__{base}"
            src = os.path.join(outdir, "files", r["file"])
            if os.path.exists(src) and not r["file"].startswith(f"{r['id']}__"):
                os.replace(src, os.path.join(outdir, "files", new))
                r["file"] = new
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=1)
    import csv

    with open(os.path.join(outdir, "matches.csv"), "w", newline="", encoding="utf-8-sig") as o:
        w = csv.writer(o)
        w.writerow(["id", "file", "identical_copy"])
        for r, same in hits:
            for p in same:
                w.writerow([r["id"], r["file"], p])
    log(f"{len(hits)} of {len(records)} recovered files have an identical copy in the searched folders")
    return hits
