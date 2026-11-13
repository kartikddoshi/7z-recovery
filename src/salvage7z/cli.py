"""Command line for salvage7z."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__, lzma2, pipeline


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="salvage7z",
        description="Recover data from a damaged .7z archive that 7-Zip can no longer open "
                    "(lost end header, truncated file, zeroed or corrupt regions).",
    )
    p.add_argument("--version", action="version", version=f"salvage7z {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def scan_options(sp):
        sp.add_argument("--no-resync", action="store_true",
                        help="stop at the first damaged spot instead of searching for more data after it")
        sp.add_argument("--max-gap", type=int, default=64, metavar="MIB",
                        help="give up after this many MiB of unrecognisable (non-zero) data (default: 64)")

    sp = sub.add_parser("info", help="diagnose an archive and show what looks recoverable (reads only)")
    sp.add_argument("archive")
    scan_options(sp)

    sp = sub.add_parser("recover", help="unpack everything recoverable and split it into files")
    sp.add_argument("archive")
    sp.add_argument("outdir")
    sp.add_argument("-j", "--jobs", type=int, default=min(4, os.cpu_count() or 1),
                    help="streams to unpack in parallel (default: %(default)s)")
    sp.add_argument("--no-split", action="store_true", help="only unpack the raw streams; do not cut them into files")
    sp.add_argument("--keep-streams", action="store_true", help="keep the raw unpacked streams after splitting")
    sp.add_argument("--previews", action="store_true", help="also save thumbnails embedded in recovered files")
    sp.add_argument("--force", action="store_true", help="run even if free disk space looks too small")
    scan_options(sp)

    sp = sub.add_parser("split", help="cut already-unpacked raw data into files")
    sp.add_argument("outdir")
    sp.add_argument("raw", nargs="+", help="raw stream file(s), e.g. the output of 7-Zip's own recovery method")
    sp.add_argument("--previews", action="store_true")

    sp = sub.add_parser("match", help="find identical copies of recovered files elsewhere to get names back")
    sp.add_argument("outdir", help="output folder of a previous `recover` or `split`")
    sp.add_argument("search", nargs="+", help="folder(s) to search")
    sp.add_argument("--rename", action="store_true", help="rename matched files to <id>__<original name>")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "info":
            diag, result = pipeline.inspect(args.archive, not args.no_resync, args.max_gap * lzma2.MIB, _log)
            print("\n".join(pipeline.report(diag, result)))
            return 0 if result.streams else 2
        if args.command == "recover":
            summary = pipeline.recover(args.archive, args.outdir, jobs=max(1, args.jobs), split=not args.no_split,
                                       keep_streams=args.keep_streams or args.no_split, previews=args.previews,
                                       resync=not args.no_resync, max_garbage=args.max_gap * lzma2.MIB,
                                       force=args.force, log=_log)
            print(f"Unpacked {summary['unpacked_bytes']:,} of {summary['expected_bytes']:,} bytes "
                  f"from {summary['streams']} stream(s).")
            if not args.no_split:
                print(f"{summary['files']} file(s) written to {os.path.join(args.outdir, 'files')}:")
                for k, n in summary["by_kind"].items():
                    print(f"  {n:>6}  {k}")
                print(f"Details: {os.path.join(args.outdir, 'manifest.csv')}")
            return 0
        if args.command == "split":
            records = pipeline.split_only(args.raw, args.outdir, args.previews, _log)
            print(f"{len(records)} file(s) written to {os.path.join(args.outdir, 'files')}")
            return 0
        if args.command == "match":
            hits = pipeline.match(args.outdir, args.search, args.rename, _log)
            for r, same in hits:
                print(f"{r['file']}  ==  {same[0]}" + (f"  (+{len(same) - 1} more)" if len(same) > 1 else ""))
            return 0
    except pipeline.SalvageError as e:
        print(f"salvage7z: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"salvage7z: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
