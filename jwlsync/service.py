"""Turning uploads into merged files.

`process(storage)` handles everything in the user's `JW Library Save` folder as one batch, leaving
only the current merged file there:

1. Files JWLSync wrote itself (left by an interrupted run) go to `History/Merged`; anything that
   isn't a readable backup goes to `History/Uploads` as rejected.
2. Of several uploads from one device carrying the same stamp, only the newest counts: the older
   ones are earlier states of that device (anything missing from the newer one was deleted there)
   and go to `History/Uploads` as superseded.
3. The rest are merged into the current merged file in the order their data last changed, each
   against its own base: the merged file its device last restored (stamp first, then content, see
   jwlkit.base). No base means nothing is deleted.
4. If anything changed, one new merged file is written, stamped with a fresh merge id; the previous
   one moves to `History/Merged` and the uploads to `History/Uploads`.

Every outcome is appended to `User/merge-log.txt`. State lives in `User/state.json`: the current
merge id and, per merge id, where its file is now.
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

from .storage import MERGED, SAVE, UPLOADS, Entry, Storage

STATE = "state.json"
LOG = "merge-log.txt"
EXTENSION = ".jwlibrary"
WRITER = "JWLSync"  # device name in the manifest of files we write
# Recent merged files compared by content when an upload carries no known stamp.
CONTENT_CANDIDATES = 10


@dataclass
class Result:
    upload: str
    device: str
    outcome: str                  # "merged", "unchanged", "superseded", "rejected" or "leftover"
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


@dataclass
class _Upload:
    entry: Entry
    device: str
    ds: Dataset

    @property
    def order(self) -> tuple[float, float]:
        changed = self.ds.last_modified.timestamp() if self.ds.last_modified else self.entry.modified
        return changed, self.entry.modified

    @property
    def stamp_id(self) -> str | None:
        return self.ds.stamp.get("merge_id") or None


def _open_uploads(storage: Storage, entries: list[Entry], files: _Files, stamp: str,
                  results: list[Result]) -> list[_Upload]:
    """Readable device backups among entries, oldest data first; everything else is moved out."""
    uploads = []
    for e in entries:
        if e.ours:
            storage.move(e.id, MERGED)
            results.append(Result(e.name, WRITER, "leftover", summary="written by an interrupted run"))
            continue
        try:
            if not e.name.lower().endswith(EXTENSION):
                raise ValueError(f"not a {EXTENSION} file")
            backup = files.open(e.id)
            u = _Upload(e, backup.device_name, read(backup))
        except (BackupError, OSError, ValueError) as err:
            storage.move(e.id, UPLOADS, f"{stamp} rejected {e.name}")
            results.append(Result(e.name, "", "rejected", summary=str(err)))
            continue
        if u.device == WRITER:
            storage.move(e.id, MERGED)
            results.append(Result(e.name, WRITER, "leftover", summary="written by JWLSync"))
            continue
        uploads.append(u)
    return sorted(uploads, key=lambda u: u.order)


def _drop_superseded(storage: Storage, uploads: list[_Upload], state: dict, stamp: str,
                     results: list[Result]) -> list[_Upload]:
    newest = {(u.device, u.stamp_id): u for u in uploads if u.stamp_id in state["merges"]}
    keep = []
    for u in uploads:
        latest = newest.get((u.device, u.stamp_id))
        if latest and latest is not u:
            storage.move(u.entry.id, UPLOADS, f"{stamp} superseded {u.entry.name}")
            results.append(Result(u.entry.name, u.device, "superseded",
                                  summary=f"replaced by the newer {latest.entry.name} from the same device"))
        else:
            keep.append(u)
    return keep


def process(storage: Storage, settle: float = 5.0, clock=time.time) -> list[Result]:
    """Merge every file in JW Library Save that hasn't changed for `settle` seconds."""
    state = _load_state(storage)
    current = state["current"]
    current_file = state["merges"].get(current, {}).get("id")
    by_file = {m["id"]: m for m in state["merges"].values()}
    pending = []
    for e in storage.list(SAVE):
        if e.id == current_file:
            continue
        if e.id in by_file:  # an earlier merged file a run didn't get to move
            by_file[e.id]["id"] = storage.move(e.id, MERGED).id
            _save_state(storage, state)
        elif clock() - e.modified >= settle:
            pending.append(e)
    if not pending:
        return []

    now = datetime.fromtimestamp(clock(), timezone.utc)
    stamp = f"{now:%Y-%m-%d %H.%M.%S}"
    results: list[Result] = []
    with ExitStack() as stack:
        files = _Files(storage, stack)
        uploads = _open_uploads(storage, pending, files, stamp, results)
        uploads = _drop_superseded(storage, uploads, state, stamp, results)

        master = files.dataset(current_file) if current else Dataset()
        merged: list[Result] = []
        changed = False
        for u in uploads:
            match, base = _find_base(u.ds, state, files)
            out, report = merge(master, u.ds, base)
            r = Result(u.entry.name, u.device, "merged", match, summary=report.summary())
            if (current or changed) and _same_content(out, master):
                r.outcome = "unchanged"
            else:
                master, changed = out, True
            merged.append(r)

        if changed:
            state["seq"] += 1
            merge_id = uuid.uuid4().hex[:12]
            sources = ", ".join(r.upload for r in merged if r.outcome == "merged")
            master.stamp = {"merge_id": merge_id, "seq": str(state["seq"]), "created": now.isoformat(timespec="seconds"),
                            "source": sources, "base": ", ".join(sorted({r.base.merge_id for r in merged if r.base}))}
            out_path = files.dir / f"out-{merge_id}{EXTENSION}"
            write_backup(master, out_path, device_name=WRITER)
            written = storage.upload(SAVE, out_path, f"JWLSync {stamp}{EXTENSION}")
            state["merges"][merge_id] = {"id": written.id, "seq": state["seq"], "source": sources}
            state["current"] = merge_id
            _save_state(storage, state)
            if current:
                old = state["merges"][current]
                old["id"] = storage.move(old["id"], MERGED).id
                _save_state(storage, state)
            for r in merged:
                if r.outcome == "merged":
                    r.merged = written.name
        for u in uploads:
            storage.move(u.entry.id, UPLOADS, f"{stamp} {u.entry.name}")
        results += merged
    for r in results:
        _log(storage, now, r)
    return results
