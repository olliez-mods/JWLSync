"""Merging Datasets.

`merge(master, upload, base)` applies what changed on a device to the master copy:

    upload vs base      master vs base      result
    ---------------     ---------------     ------------------------------------------
    unchanged           anything            master
    added/edited        unchanged           upload
    deleted             unchanged           deleted
    deleted             edited              master   (an edit beats a delete)
    added/edited        deleted             upload   (an edit beats a delete)
    edited              edited differently  conflict: notes -> newer LastModified, else `prefer`
                                            (upload by default; or master; or the newer backup)

`base` must be the state the uploading device last restored. With no base (a device seen for the
first time, or a plain combine of several files) nothing is deleted: entries are added, and entries
present on both sides are resolved like a conflict.

Playlists merge as whole units (a playlist and its ordered items), since playlist items have no
stable identity of their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .archive import parse_time
from .model import TABLES, TAG_PLAYLIST, Dataset

_MISSING = object()


@dataclass
class TableReport:
    added: int = 0
    removed: int = 0
    changed: int = 0
    conflicts: int = 0

    def any(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.conflicts)


@dataclass
class MergeReport:
    tables: dict[str, TableReport] = field(default_factory=lambda: {t: TableReport() for t in TABLES})
    notes: list[str] = field(default_factory=list)  # human-readable remarks (overlaps, repairs)

    def summary(self) -> str:
        lines = []
        for name, t in self.tables.items():
            if t.any():
                lines.append(f"{name:12} +{t.added} -{t.removed} ~{t.changed}" + (f" !{t.conflicts}" if t.conflicts else ""))
        lines.extend(self.notes)
        return "\n".join(lines) or "no changes"


PREFER = ("upload", "master", "newer")


def _resolve(table: str, m, u, master_wins: bool):
    if table == "notes":
        mt, ut = parse_time(m["modified"]), parse_time(u["modified"])
        if mt and ut and mt != ut:
            return m if mt > ut else u
    return m if master_wins else u


def _master_wins(master: Dataset, upload: Dataset, prefer: str) -> bool:
    if prefer not in PREFER:
        raise ValueError(f"prefer must be one of {PREFER}, not {prefer!r}")
    if prefer == "newer":
        return bool(master.last_modified and upload.last_modified and master.last_modified > upload.last_modified)
    return prefer == "master"


def _merge_table(table: str, base: dict | None, master: dict, upload: dict, rep: TableReport,
                 master_wins: bool = False) -> dict:
    result = dict(master)
    keys = set(upload) if base is None else set(upload) | set(base)
    for k in keys:
        m = master.get(k, _MISSING)
        u = upload.get(k, _MISSING)
        if base is None:
            if m is _MISSING:
                new = u
            elif m == u:
                continue
            else:
                rep.conflicts += 1
                new = _resolve(table, m, u, master_wins)
        else:
            b = base.get(k, _MISSING)
            if u == b:
                continue
            if m == b:
                new = u
            elif m == u:
                continue
            elif u is _MISSING:
                rep.conflicts += 1
                new = m
            elif m is _MISSING:
                rep.conflicts += 1
                new = u
            else:
                rep.conflicts += 1
                new = _resolve(table, m, u, master_wins)
        if new is m:
            continue
        if new is _MISSING:
            del result[k]
            rep.removed += 1
        elif m is _MISSING:
            result[k] = new
            rep.added += 1
        else:
            result[k] = new
            rep.changed += 1
    return result


def merge(master: Dataset, upload: Dataset, base: Dataset | None = None,
          prefer: str = "upload") -> tuple[Dataset, MergeReport]:
    """Apply upload's changes (relative to base) to master.

    prefer decides conflicts that have no per-entry timestamp (everything but notes, which always
    go to the newer LastModified): "upload" (default), "master", or "newer", which picks the side
    whose backup changed last (Dataset.last_modified) and falls back to upload when unknown.
    """
    master_wins = _master_wins(master, upload, prefer)
    rep = MergeReport()
    out = Dataset()
    for table in TABLES:
        setattr(out, table, _merge_table(table, getattr(base, table) if base else None,
                                         getattr(master, table), getattr(upload, table), rep.tables[table],
                                         master_wins))
    # Lookup data: the winning side goes last so its display fields (e.g. Location.Title) stick.
    sources = (base, upload, master) if master_wins else (base, master, upload)
    for src in sources:
        if src:
            out.locations.update(src.locations)
            out.media.update(src.media)
            out.media_files.update(src.media_files)
    times = [d.last_modified for d in (master, upload) if d.last_modified]
    out.last_modified = max(times) if times else None
    # Overlapping highlights: the winning side's new or changed marks are kept.
    side, other = (master, upload) if master_wins else (upload, master)
    ref = base.usermarks if base else other.usermarks
    touched = {g for g, m in side.usermarks.items() if ref.get(g) != m}
    repair(out, rep, prefer=touched)
    return out, rep


def combine(*datasets: Dataset, sort: bool = True) -> tuple[Dataset, MergeReport]:
    """Union of several backups with no deletions, like JWLMerge.

    Conflicts go to the later dataset. With sort=True (default) datasets are ordered oldest to
    newest by last_modified first, so the newest backup wins; sort=False keeps the given order.
    """
    if not datasets:
        return Dataset(), MergeReport()
    if sort:
        # Stable: undated datasets go first, ties keep their given order.
        datasets = sorted(datasets, key=lambda d: (d.last_modified is not None, d.last_modified or 0))
    out, total = datasets[0], MergeReport()
    for ds in datasets[1:]:
        out, rep = merge(out, ds)
        for name, t in rep.tables.items():
            tt = total.tables[name]
            tt.added += t.added; tt.removed += t.removed; tt.changed += t.changed; tt.conflicts += t.conflicts
        total.notes.extend(rep.notes)
    return out, total


def repair(ds: Dataset, rep: MergeReport, prefer: set[str] = frozenset()) -> None:
    """Fix references a merge can leave dangling, and overlapping highlights."""
    _drop_overlaps(ds, rep, prefer)

    for guid, n in list(ds.notes.items()):
        if n["usermark"] is not None and n["usermark"] not in ds.usermarks:
            ds.notes[guid] = {**n, "usermark": None}

    for key in [k for k in ds.tagmaps if k[0] not in ds.tags or (k[1][0] == "n" and k[1][1] not in ds.notes)]:
        del ds.tagmaps[key]

    for tkey in [k for k in ds.playlists if k not in ds.tags]:
        del ds.playlists[tkey]
    for tkey in ds.tags:
        if tkey[0] == TAG_PLAYLIST:
            ds.playlists.setdefault(tkey, ())


def _drop_overlaps(ds: Dataset, rep: MergeReport, prefer: set[str]) -> None:
    """Two devices can highlight overlapping text independently; JW Library expects marks in the
    same paragraph/verse not to overlap. Keep the preferred (just-uploaded) mark and drop the other,
    unless the other carries a note, in which case keep both."""
    if not prefer:
        return
    with_notes = {n["usermark"] for n in ds.notes.values() if n["usermark"]}
    spans: dict[tuple, list] = {}
    for guid, m in ds.usermarks.items():
        for bt, ident, start, end in m["ranges"]:
            if start is not None and end is not None:
                spans.setdefault((m["loc"], bt, ident), []).append((start, end, guid))
    drop = set()
    for block in spans.values():
        block.sort()
        for i, (s1, e1, g1) in enumerate(block):
            for s2, e2, g2 in block[i + 1:]:
                if s2 > e1:
                    break
                # Only overlaps between the upload and other devices; leave a device's own data alone.
                if (g1 in prefer) == (g2 in prefer) or g1 in drop or g2 in drop:
                    continue
                lose = g2 if g1 in prefer else g1
                if lose in with_notes:
                    continue
                drop.add(lose)
    for guid in drop:
        del ds.usermarks[guid]
    if drop:
        rep.tables["usermarks"].removed += len(drop)
        rep.notes.append(f"removed {len(drop)} highlight(s) overlapping a newer one")
