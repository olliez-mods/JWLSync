"""python -m jwlsync {setup,process,watch} FOLDER: run the service against a local folder."""

from __future__ import annotations

import argparse
import time

from .service import process
from .storage import LocalStorage


def _print(results) -> None:
    for r in results:
        base = f"{r.base.method}:{r.base.merge_id}" if r.base else "none"
        print(f"{r.upload}: {r.outcome} (base {base})" + (f" -> {r.merged}" if r.merged else ""))
        for line in r.summary.splitlines():
            print(f"    {line}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="jwlsync")
    p.add_argument("command", choices=("setup", "process", "watch"))
    p.add_argument("folder")
    p.add_argument("--interval", type=float, default=10.0, help="watch: seconds between checks")
    p.add_argument("--settle", type=float, default=5.0, help="seconds a file must be unchanged before merging")
    args = p.parse_args(argv)

    storage = LocalStorage(args.folder)
    if args.command == "setup":
        storage.setup()
        print(f"set up {args.folder}")
    elif args.command == "process":
        _print(process(storage, settle=args.settle))
    else:
        while True:
            _print(process(storage, settle=args.settle))
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
