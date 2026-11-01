"""python -m jwlsync {setup,process,watch} [FOLDER]: run the service against a folder.

FOLDER is a local directory or a Google Drive folder link; Drive needs a token (see `auth`).
Without FOLDER, every folder in the config's [folders] section is used.
python -m jwlsync auth CLIENT_SECRETS --token TOKEN: log in once as the bot account.

Settings come from config.ini (or --config), created from config.ini.template on first run;
command-line flags override it.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from .service import process
from .storage import LocalStorage

DEFAULT_CONFIG = "config.ini"


def _print(results) -> None:
    for r in results:
        base = f" (base {r.base.method}:{r.base.merge_id})" if r.base else (
            " (base none)" if r.outcome in ("merged", "unchanged") else "")
        print(f"{r.upload}: {r.outcome}{base}" + (f" -> {r.merged}" if r.merged else ""))
        for line in r.summary.splitlines():
            print(f"    {line}")


def _storage(folder: str, token: str | None):
    if not folder.startswith("https://"):
        return LocalStorage(folder)
    from .drive import GoogleDrive, load_credentials, open_folder

    if not token:
        raise SystemExit("a Drive folder needs a token (create one with `python -m jwlsync auth`)")
    return open_folder(GoogleDrive(load_credentials(token)), folder)


def _load_config(path: str | None) -> tuple[dict, list[str]]:
    """([jwlsync] settings, folders). Relative paths are relative to the file.

    A missing config.ini is created from config.ini.template, and settings the template gains later
    are added to it (quick_ini's template sync)."""
    from quick_ini import QuickIni

    file = Path(path or DEFAULT_CONFIG)
    if not file.exists() and not Path(f"{file}.template").exists():
        if path:
            raise SystemExit(f"config file not found: {path}")
        return {}, []
    if not QuickIni.load_file(str(file), template=True):
        raise SystemExit(f"can't load {file}: {QuickIni.get_last_error()}")
    report = QuickIni.get_sync_report()
    if report and report["created"]:
        print(f"created {file} from its template; add your folders to it")
    elif report and report["added"]:
        print(f"added new settings to {file}: {', '.join(report['added'])}")
    settings = {k: v for k, v in QuickIni.get_all("jwlsync").items() if v is not None}
    if settings.get("token"):
        settings["token"] = str(file.parent / str(settings["token"]))
    listed = settings.pop("folders", None) or []
    folders = [str(f) for f in (listed if isinstance(listed, list) else [listed]) if f]
    return settings, [f if f.startswith("https://") else str(file.parent / f) for f in folders]


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="jwlsync")
    p.add_argument("command", choices=("setup", "process", "watch", "auth"))
    p.add_argument("folder", nargs="?",
                   help="local folder or Drive folder link (default: all in the config); for auth, the OAuth client secrets file")
    p.add_argument("--config", help=f"settings file (default: {DEFAULT_CONFIG} if present)")
    p.add_argument("--token", help="bot account token file (Drive)")
    p.add_argument("--interval", type=float, help="watch: seconds between checks (default 10)")
    p.add_argument("--settle", type=float, help="seconds a file must be unchanged before merging (default 5)")
    args = p.parse_args(argv)
    settings, folders = _load_config(args.config)
    token = args.token or settings.get("token")
    interval = args.interval if args.interval is not None else float(settings.get("interval", 10))
    settle = args.settle if args.settle is not None else float(settings.get("settle", 5))

    if args.command == "auth":
        from .drive import authorize

        if not args.folder or not token:
            raise SystemExit("auth needs CLIENT_SECRETS and a token path (--token or config)")
        authorize(args.folder, token)
        print(f"saved token to {token}")
        return
    targets = [args.folder] if args.folder else folders
    if not targets:
        raise SystemExit("no folder given and no folders in the config")
    storages = [(t, _storage(t, token)) for t in targets]
    if args.command == "setup":
        for t, storage in storages:
            storage.setup()
            print(f"set up {t}")
    elif args.command == "process":
        for _, storage in storages:
            _print(process(storage, settle=settle))
    else:
        while True:
            for _, storage in storages:
                _print(process(storage, settle=settle))
            time.sleep(interval)


if __name__ == "__main__":
    main()
