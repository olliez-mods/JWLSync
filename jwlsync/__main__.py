"""python -m jwlsync {setup,process,watch} FOLDER: run the service against a folder.

FOLDER is a local directory or a Google Drive folder link; Drive needs --token (see `auth`).
python -m jwlsync auth CLIENT_SECRETS --token TOKEN: log in once as the bot account.
"""

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


def _storage(folder: str, token: str | None):
    if not folder.startswith("https://"):
        return LocalStorage(folder)
    from .drive import GoogleDrive, load_credentials, open_folder

    if not token:
        raise SystemExit("a Drive folder needs --token (create one with `python -m jwlsync auth`)")
    return open_folder(GoogleDrive(load_credentials(token)), folder)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="jwlsync")
    p.add_argument("command", choices=("setup", "process", "watch", "auth"))
    p.add_argument("folder", help="local folder or Drive folder link; for auth, the OAuth client secrets file")
    p.add_argument("--token", help="bot account token file (Drive)")
    p.add_argument("--interval", type=float, default=10.0, help="watch: seconds between checks")
    p.add_argument("--settle", type=float, default=5.0, help="seconds a file must be unchanged before merging")
    args = p.parse_args(argv)

    if args.command == "auth":
        from .drive import authorize

        if not args.token:
            raise SystemExit("auth needs --token: where to save the bot's token")
        authorize(args.folder, args.token)
        print(f"saved token to {args.token}")
        return
    storage = _storage(args.folder, args.token)
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
