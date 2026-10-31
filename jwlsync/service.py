"""Turning uploads into merged files.

`process(storage)` handles every new backup in the user's `JW Library Save` folder, oldest first:

1. Find the base: the merged file the uploading device last restored (stamp first, then content,
   see jwlkit.base). No base means a device seen for the first time; nothing is deleted.
2. 3-way merge the upload into the current merged file.
3. Write the result into `JW Library Save` as a new merged file, stamped with a fresh merge id,
   then move the previous merged file and the upload into `History`.

An upload that changes nothing is just moved to History. A file that isn't a readable backup is
moved to History with a "rejected" prefix. Every outcome is appended to `User/merge-log.txt`.

State lives in `User/state.json`: the current merge id and, per merge id, where its file is now.
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jwlkit import BackupError, BaseMatch, Dataset, find_base, merge, open_backup, read, write_backup
from jwlkit.model import TABLES

from .storage import HISTORY, SAVE, Entry, Storage

STATE = "state.json"
LOG = "merge-log.txt"
EXTENSION = ".jwlibrary"
# Recent merged files compared by content when an upload carries no known stamp.
CONTENT_CANDIDATES = 10


@dataclass
class Result:
    upload: str
    device: str
    outcome: str                  # "merged", "unchanged" or "rejected"
    base: BaseMatch | None = None
    merged: str | None = None     # name of the new merged file
    summary: str = ""


def _load_state(storage: Storage) -> dict:
    text = storage.read_text(STATE)
    state = json.loads(text) if text else {}
    state.setdefault("seq", 0)
    state.setdefault("current", None)
    state.setdefault("merges", {})
    return state


def _save_state(storage: Storage, state: dict) -> None:
    storage.write_text(STATE, json.dumps(state, indent=1, sort_keys=True))


def _log(storage: Storage, when: datetime, r: Result) -> None:
    base = f"{r.base.method} {r.base.merge_id}" if r.base else "none"
    lines = [f"{when:%Y-%m-%d %H:%M:%S}Z  {r.upload} ({r.device or 'unknown device'})  {r.outcome}",
             f"  base: {base}"]
    if r.merged:
        lines.append(f"  wrote: {r.merged}")
    lines += [f"  {line}" for line in r.summary.splitlines()]
    storage.write_text(LOG, (storage.read_text(LOG) or "") + "\n".join(lines) + "\n\n")


class _Files:
    """Downloads and opens backups once per process() run; closes them all at the end."""

    def __init__(self, storage: Storage, stack: ExitStack):
        self.storage, self.stack = storage, stack
        self.dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="jwlsync-")))
        self.cache: dict[str, Dataset] = {}

    def open(self, file_id: str):
        dest = self.dir / f"{len(list(self.dir.iterdir()))}{EXTENSION}"
        self.storage.download(file_id, dest)
        return self.stack.enter_context(open_backup(dest))

    def dataset(self, file_id: str) -> Dataset:
        if file_id not in self.cache:
            self.cache[file_id] = read(self.open(file_id))
        return self.cache[file_id]


def _same_content(a: Dataset, b: Dataset) -> bool:
    return all(getattr(a, t) == getattr(b, t) for t in TABLES)


def _find_base(upload: Dataset, state: dict, files: _Files) -> tuple[BaseMatch | None, Dataset | None]:
    merges = state["merges"]
    sid = upload.stamp.get("merge_id")
    if sid in merges:
        return BaseMatch(sid, "stamp", 1.0), files.dataset(merges[sid]["id"])
    recent = sorted(merges, key=lambda m: merges[m]["seq"], reverse=True)[:CONTENT_CANDIDATES]
    candidates = {}
    for mid in recent:
        try:
            candidates[mid] = files.dataset(merges[mid]["id"])
        except (OSError, BackupError):
            continue  # pruned or unreadable; just not a candidate
    match = find_base(upload, candidates)
    return (match, candidates[match.merge_id]) if match else (None, None)


def _process_one(storage: Storage, entry: Entry, state: dict, files: _Files, now: datetime) -> Result:
    stamp = f"{now:%Y-%m-%d %H.%M.%S}"
    try:
        backup = files.open(entry.id)
        upload = read(backup)
    except (BackupError, OSError, ValueError) as e:
        storage.move(entry.id, HISTORY, f"{stamp} rejected {entry.name}")
        return Result(entry.name, "", "rejected", summary=str(e))

    current = state["current"]
    master = files.dataset(state["merges"][current]["id"]) if current else Dataset()
    match, base = _find_base(upload, state, files)
    merged, report = merge(master, upload, base)
    result = Result(entry.name, backup.device_name, "merged", match, summary=report.summary())

    if current and _same_content(merged, master):
        result.outcome = "unchanged"
        storage.move(entry.id, HISTORY, f"{stamp} {entry.name}")
        return result

    state["seq"] += 1
    merge_id = uuid.uuid4().hex[:12]
    merged.stamp = {"merge_id": merge_id, "seq": str(state["seq"]), "created": now.isoformat(timespec="seconds"),
                    "source": entry.name, "base": match.merge_id if match else ""}
    name = f"JWLSync {stamp}{EXTENSION}"
    out = files.dir / f"out-{merge_id}{EXTENSION}"
    write_backup(merged, out)
    written = storage.upload(SAVE, out, name)
    state["merges"][merge_id] = {"id": written.id, "seq": state["seq"], "source": entry.name}
    state["current"] = merge_id
    _save_state(storage, state)
    files.cache[written.id] = merged

    if current:
        old = state["merges"][current]
        moved = storage.move(old["id"], HISTORY)
        files.cache[moved.id] = files.cache.pop(old["id"], master)
        old["id"] = moved.id
    storage.move(entry.id, HISTORY, f"{stamp} {entry.name}")
    result.merged = written.name
    return result


def process(storage: Storage, settle: float = 5.0, clock=time.time) -> list[Result]:
    """Merge every backup in JW Library Save that hasn't changed for `settle` seconds."""
    state = _load_state(storage)
    current_file = state["merges"].get(state["current"], {}).get("id")
    known = {m["id"] for m in state["merges"].values()}
    uploads = [e for e in storage.list(SAVE)
               if e.name.lower().endswith(EXTENSION) and e.id != current_file and e.id not in known
               and clock() - e.modified >= settle]
    results = []
    if not uploads:
        return results
    with ExitStack() as stack:
        files = _Files(storage, stack)
        for entry in uploads:
            now = datetime.fromtimestamp(clock(), timezone.utc)
            r = _process_one(storage, entry, state, files, now)
            _save_state(storage, state)
            _log(storage, now, r)
            results.append(r)
    return results
