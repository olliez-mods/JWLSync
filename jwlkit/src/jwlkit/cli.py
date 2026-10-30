"""Command line interface: `jwlkit info|diff|merge|combine ...`."""

from __future__ import annotations

import argparse
import sys
from contextlib import ExitStack

from . import BackupError, combine, merge, open_backup, read, write_backup
from .merge import PREFER, MergeReport, _merge_table
from .model import TABLES


def _info(args) -> int:
    for path in args.files:
        with open_backup(path) as b:
            ds = read(b)
            udb = b.manifest["userDataBackup"]
            modified = ds.last_modified.strftime("%Y-%m-%d %H:%M UTC") if ds.last_modified else "unknown"
            print(f"{path}\n  device {udb.get('deviceName')}  schema {udb.get('schemaVersion')}  "
                  f"modified {modified}  media files {len(b.media)}")
            print("  " + "  ".join(f"{k} {v}" for k, v in ds.counts().items()))
    return 0


def _diff(args) -> int:
    with open_backup(args.old) as a, open_backup(args.new) as b:
        old, new = read(a), read(b)
    rep = MergeReport()
    for t in TABLES:
        _merge_table(t, getattr(old, t), getattr(old, t), getattr(new, t), rep.tables[t])
    print(rep.summary())
    return 0


def _merge(args) -> int:
    with ExitStack() as stack:
        master = read(stack.enter_context(open_backup(args.master)))
        upload = read(stack.enter_context(open_backup(args.upload)))
        base = read(stack.enter_context(open_backup(args.base))) if args.base else None
        out, rep = merge(master, upload, base, prefer=args.prefer)
        write_backup(out, args.output, device_name=args.device_name)
    print(rep.summary())
    return 0


def _combine(args) -> int:
    with ExitStack() as stack:
        datasets = [read(stack.enter_context(open_backup(p))) for p in args.files]
        out, rep = combine(*datasets, sort=not args.keep_order)
        write_backup(out, args.output, device_name=args.device_name)
    print(rep.summary())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jwlkit", description="Read, diff and merge JW Library backups.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info", help="show what a backup contains")
    s.add_argument("files", nargs="+")
    s.set_defaults(func=_info)

    s = sub.add_parser("diff", help="show what changed between two backups")
    s.add_argument("old")
    s.add_argument("new")
    s.set_defaults(func=_diff)

    s = sub.add_parser("merge", help="apply a device's changes to a master copy (3-way with --base)")
    s.add_argument("master")
    s.add_argument("upload")
    s.add_argument("--base", help="the backup the uploading device last restored; enables deletions")
    s.add_argument("--prefer", choices=PREFER, default="upload",
                   help="who wins conflicts other than notes (notes always go to the newer edit)")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--device-name", default="JWLSync")
    s.set_defaults(func=_merge)

    s = sub.add_parser("combine", help="union several backups, no deletions (newest backup wins conflicts)")
    s.add_argument("files", nargs="+")
    s.add_argument("--keep-order", action="store_true",
                   help="don't sort by date; later files on the command line win conflicts")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--device-name", default="JWLSync")
    s.set_defaults(func=_combine)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
