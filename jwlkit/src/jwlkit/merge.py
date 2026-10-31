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

Tags are keyed by name, so a rename looks like a delete plus an add. Before merging, renames are
detected on both sides (a tag gone since base and a new tag of the same type holding mostly the same
items) and applied to the other datasets, so links other devices added to the old name follow it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .archive import parse_time
from .model import TABLES, TAG_FAVORITE, TAG_PLAYLIST, Dataset

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
    if base:
        base, master, upload = _apply_renames(base, master, upload, master_wins, rep)
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


# Share of items (Jaccard) a vanished and a new tag must have in common to count as a rename.
RENAME_SIMILARITY = 0.5


def _items_by_tag(ds: Dataset) -> dict[tuple, set]:
    out: dict[tuple, set] = {}
    for tkey, target in ds.tagmaps:
        out.setdefault(tkey, set()).add(target)
    for tkey, items in ds.playlists.items():
        out.setdefault(tkey, set()).update(repr(i) for i in items)
    return out


def find_renames(base: Dataset, side: Dataset) -> dict[tuple, tuple]:
    """Tags renamed on `side` since `base`, as {old key: new key}.

    A tag missing from side pairs with a tag new on side of the same type when they share at least
    RENAME_SIMILARITY of their items; best matches first. Two empty tags pair only when they're the
    only vanished and only new tag of their type.
    """
    gone = [k for k in base.tags if k not in side.tags and k[0] != TAG_FAVORITE]
    new = [k for k in side.tags if k not in base.tags and k[0] != TAG_FAVORITE]
    if not gone or not new:
        return {}
    base_items, side_items = _items_by_tag(base), _items_by_tag(side)
    pairs = []
    for old in gone:
        for cand in new:
            if old[0] != cand[0]:
                continue
            a, b = base_items.get(old, set()), side_items.get(cand, set())
            if a or b:
                score = len(a & b) / len(a | b)
            elif sum(k[0] == old[0] for k in gone) == 1 and sum(k[0] == old[0] for k in new) == 1:
                score = 1.0
            else:
                continue
            if score >= RENAME_SIMILARITY:
                pairs.append((-score, repr(old), repr(cand), old, cand))
    renames, taken = {}, set()
    for *_, old, cand in sorted(pairs):
        if old not in renames and cand not in taken:
            renames[old] = cand
            taken.add(cand)
    return renames


def _rekey(ds: Dataset, mapping: dict[tuple, tuple]) -> Dataset:
    """Copy of ds with tags renamed per mapping; renaming onto an existing tag merges the two."""
    mapping = {old: new for old, new in mapping.items() if old in ds.tags}
    if not mapping:
        return ds
    out = copy.copy(ds)
    out.tags = {}
    for k, v in ds.tags.items():
        if k in mapping:
            out.tags.setdefault(mapping[k], {"name": mapping[k][1]})
        else:
            out.tags[k] = v
    out.tagmaps = {}
    for (tkey, target), pos in ds.tagmaps.items():
        out.tagmaps.setdefault((mapping.get(tkey, tkey), target), pos)
    out.playlists = {k: v for k, v in ds.playlists.items() if k not in mapping}
    for old, new in mapping.items():
        if old in ds.playlists:
            kept = out.playlists.get(new, ())
            out.playlists[new] = kept + tuple(i for i in ds.playlists[old] if i not in kept)
    return out


def _apply_renames(base: Dataset, master: Dataset, upload: Dataset, master_wins: bool,
                   rep: MergeReport) -> tuple[Dataset, Dataset, Dataset]:
    """Carry renames made on either side over to the other side and the base.

    The same tag renamed differently on both sides is a conflict; the losing name is folded into
    the winning one.
    """
    up, ms = find_renames(base, upload), find_renames(base, master)
    to_base, to_master, to_upload = {}, {}, {}
    for old in sorted(up.keys() | ms.keys(), key=repr):
        u, m = up.get(old), ms.get(old)
        new = u or m
        if u and m and u != m:
            rep.tables["tags"].conflicts += 1
            new = m if master_wins else u
            if master_wins:
                to_upload[u] = m
            else:
                to_master[m] = u
            rep.notes.append(f"tag {old[1]!r} renamed to both {m[1]!r} and {u[1]!r}; kept {new[1]!r}")
        elif u and not m:
            rep.tables["tags"].changed += 1
            rep.notes.append(f"tag {old[1]!r} renamed to {u[1]!r}")
        to_base[old] = to_master[old] = to_upload[old] = new
    return _rekey(base, to_base), _rekey(master, to_master), _rekey(upload, to_upload)


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
