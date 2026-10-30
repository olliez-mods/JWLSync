# jwlkit

Read, write and merge JW Library `.jwlibrary` backups. Pure Python, no dependencies.

What sets it apart from other mergers: **3-way merging**. Give it the backup a device last restored
(the *base*) and it works out what that device actually added, edited and **deleted**, then applies
just that to a master copy. Union-style mergers can't tell a deletion from "never had it", so
deleted highlights and notes keep coming back.

## Install

```
pip install ./jwlkit        # or: pip install -e ./jwlkit
```

## Command line

```
jwlkit info backup.jwlibrary [...]                         # what's inside
jwlkit diff old.jwlibrary new.jwlibrary                    # what changed between two backups
jwlkit merge master.jwlibrary upload.jwlibrary --base restored.jwlibrary -o merged.jwlibrary
jwlkit combine a.jwlibrary b.jwlibrary c.jwlibrary -o merged.jwlibrary   # union, no deletions
```

Who wins a conflict (both sides changed the same thing differently):
- **Notes** always go to the newer edit, by each note's own `LastModified`.
- Everything else has no per-entry timestamp. `merge --prefer upload|master|newer` decides
  (default `upload`; `newer` compares the two backups' dates and falls back to upload when unknown).
- `combine` sorts its inputs oldest → newest by backup date, so the newest wins; `--keep-order`
  uses command-line order instead.

A backup's date is the later of its manifest `lastModifiedDate` and its database `LastModified`
row (`Backup.last_modified` / `Dataset.last_modified`). It covers the whole backup, not individual
changes, and some devices don't keep it current, so treat it as a hint.

`python -m jwlkit ...` works too.

## Library

```python
from jwlkit import open_backup, read, merge, write_backup

with open_backup("restored.jwlibrary") as b, open_backup("master.jwlibrary") as m, \
     open_backup("upload.jwlibrary") as u:
    merged, report = merge(read(m), read(u), base=read(b))
    write_backup(merged, "merged.jwlibrary")
print(report.summary())
```

Keep the backups open until `write_backup` returns; playlist media is copied from their extracted files.

## Merge rules

| on the device (vs base) | on the master (vs base) | result |
|---|---|---|
| unchanged | anything | master |
| added / edited | unchanged | device |
| deleted | unchanged | deleted |
| deleted | edited | master (an edit beats a delete) |
| added / edited | deleted | device (an edit beats a delete) |
| edited | edited differently | notes: newer `LastModified`; everything else: per `prefer` (device by default) |

Without a base nothing is deleted. After merging, dangling references are repaired (a note whose
highlight was deleted keeps its text; tag links to deleted tags/notes are dropped), and a new
highlight that overlaps another device's highlight replaces it unless that one has a note attached.
Playlists merge as whole units.

## Format support

- Schema versions 13–16 are read; output is always schema 16 (current JW Library).
- `userData.db-wal` / `-shm` inside the archive are replayed, so recent changes aren't lost.
- Everything is matched by GUID or natural key, never by the per-device integer IDs.
- Timestamps in any of the platform formats (`Z`, `+00:00`, `-0400`) compare correctly.

See `../docs/format-notes.md` for the format details.

## Tests

```
cd jwlkit
PYTHONPATH="src;tests" python -m unittest discover -s tests      # ':' instead of ';' on Linux/macOS
JWLKIT_TESTDATA=/path/to/backups ...                             # also round-trip real backups
```
