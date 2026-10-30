# .jwlibrary format & merge research

Sources: the user's real backups (`testdata/originals/`, 20 files: PC, iPad, iPhone, Samsung tablet,
two Samsung phones, 5 go-jwlm merge outputs), plus the source of three existing tools:
- **go-jwlm** (github.com/AndreasSko/go-jwlm) — Go, the most careful merger; the user's past merges came from it.
- **JWLMerge** (github.com/mamertofabian/jwlmerge, mirror of AntonyCorbett/JWLMerge) — C#, simple union merge.
- **JWLManager** (github.com/erykjj/jwlmanager) — Python, actively maintained; has schema upgrade code.

## Archive layout
Zip containing:
- `manifest.json`
- `userData.db` — SQLite
- **`userData.db-wal`, `userData.db-shm`, `userData.db-journal`** — present in newer Android backups!
  Newest real backup has a 241 KB WAL next to a 241 KB db. Data may live only in the WAL.
  → Extract all `userData.db*` files together, open with sqlite (auto-replays WAL), then checkpoint
  (`PRAGMA wal_checkpoint(TRUNCATE)` / `journal_mode=DELETE`) before reading/writing.
  Output archives should contain only a clean `userData.db` (no WAL).
- `default_thumbnail.png`
- Playlist media files, named by `IndependentMedia.FilePath` (e.g. `<uuid>.jpg`, `<uuid>`). Can be MBs.

## manifest.json
```json
{"name":"UserdataBackup_2026-09-17_Samsung_SM-F976U1.jwlibrary","creationDate":"2026-09-17",
 "version":1,"type":0,
 "userDataBackup":{"lastModifiedDate":"2026-09-17T13:11:32Z","deviceName":"Samsung_SM-F976U1",
   "databaseName":"userData.db","hash":"<sha256 hex of userData.db>","schemaVersion":16}}
```
- `version` must be 1. `type` 0 = full backup, 1 = playlist export (JWLManager).
- `hash` = SHA-256 hex of the final `userData.db` file bytes (all tools agree; JWLMerge's var is named sha1 but uses SHA256).
- `schemaVersion` = `PRAGMA user_version` of the db.
- Writers set `LastModified` table value = manifest `lastModifiedDate`, then hash. Compact JSON.
- go-jwlm writes `deviceName: "go-jwlm"`; we'll write `JWLSync`.

## Schema versions seen
| ver | devices | difference |
|---|---|---|
| 13 | iPad (2025) | Location lacks `Specialty`, `Edition`; has `grdb_migrations` table (iOS) |
| 14 | PC, iPhone, Samsung tablet (2025) | same as 13 for our purposes |
| 16 | Samsung phones (2026) | Location + `Specialty`, `Edition`, new unique index `IX_Location_Media` |

- JWLManager upgrades <16 → 16 in place (add two columns, rebuild Location, add index, `user_version=16`).
  We do the same: **normalize every input to 16 before merging**, always output 16.
  (Current JW Library on all platforms should be on 16 now; old devices need app updates to import a v16 file.)
- go-jwlm now only accepts 16. JWLManager accepts >11.
- Platform-specific tables (`android_metadata`, `grdb_migrations`) — keep whatever the base has; ignore for merge.

## Tables & identity ("what makes two rows the same")
Integer IDs are local per device and **never** comparable across files. Identity comes from:

| Table | Natural key (go-jwlm `UniqueKey`) | Notes |
|---|---|---|
| Location | Book, Chapter, DocumentId, Track, IssueTagNumber, KeySymbol, MepsLanguage, Type, Specialty, Edition | Title is NOT part of key. Type 0=doc/chapter, 1=Bible, 2/3=audio/video |
| UserMark | `UserMarkGuid` | highlight. ColorIndex 1–6, StyleIndex, Version (all 1 in data) |
| BlockRange | belongs to a UserMark (BlockType, Identifier, StartToken, EndToken) | 1–7 per mark; BlockType 1=paragraph, 2=verse |
| Note | `Guid` | has LastModified + Created. May have no UserMark (most don't) and rarely no Location |
| Tag | (Type, Name) | **Type 0 = "Favorite"** (one, built-in), **1 = user tag**, **2 = playlist** |
| TagMap | (Tag, and exactly one of Note / Location / PlaylistItem) | `Position` must be unique per Tag |
| Bookmark | (PublicationLocation, Slot) | Slot 0–9 per publication |
| InputField | (Location, TextTag) | text typed into workbook/worksheet fields |
| IndependentMedia | file content hash | playlist images/videos |
| PlaylistItem | no guid! go-jwlm keys on (playlist tag + media/location + repeat index) | plus Marker / *Map tables |
| PlaylistItemAccuracy | static lookup (2 rows) | copy as-is |
| LastModified | single row, triggers update it; INSERT/DELETE blocked by triggers | |

## How existing tools merge (all are 2-way unions)
**JWLMerge**
- UserMark: if guid exists → keep destination's (colour changes are lost). Else insert with remapped IDs.
- Note: guid match → newer `LastModified` wins (title + content). Else insert.
- InputField: existing kept, new inserted (edits lost).
- Tag by (Type,Name); TagMap deduped; then **renumbers Position per Tag** to satisfy unique constraint.
- **Ignores playlists entirely.**
- "Cleaner": drops orphan Locations/TagMaps/BlockRanges after merge; FK check before writing.

**go-jwlm**
- Same keys as above, but anything not identical is a **conflict the user must resolve** (interactive).
- Detects **overlapping highlights**: two different UserMarks whose BlockRanges overlap on the same
  paragraph/verse → conflict. Identical-range duplicates are auto-resolved.
- Handles playlists (items, markers, media dedup by content hash).
- VACUUMs the output.

**Nobody handles deletions** — a union can't. This is JWLSync's main improvement.

## JWLSync merge rules (3-way: base = last merged file, ours = base copy, theirs = new upload)
For each table, key rows by natural key (resolving FK IDs → natural keys first, e.g. Note.LocationId → Location key).
- key in theirs, not in base → **added on device → add**
- key in base, not in theirs → **deleted on device → delete** (cascade: UserMark → BlockRanges, Note → TagMaps)
- in both, content differs → **edited on device → take theirs**, except:
  - Note: compare `LastModified`; if the master's copy is newer (changed by another device since the
    base), keep the master's. (Only relevant if the master moved after this device's last restore.)
- Overlapping highlights after merge → keep newest (theirs); remove the overlapped older mark
  (unless it carries a Note — then keep both). TODO confirm JW Library tolerates overlap.
- TagMap Position: renumber per Tag after merge, preserving theirs' order.
- Playlists: v1 = treat playlist (Tag type 2) + its items as one unit; replaced wholesale by the newer side.
  Copy referenced media files between archives.
- Rebuild IDs densely, run an orphan cleaner + FK check, VACUUM, write manifest.

### Important nuance: "base" depends on the device
A 3-way merge is only correct if `base` = the state the uploading device last saw. A device that
hasn't restored the merged file in a while is diffing against an older state.
- v1 approach: base = the latest merged file. A stale device would then look like it "deleted"
  everything other devices added. **Mitigation: per-device base tracking** — remember, per
  `deviceName`, which merged file it last restored (detect by matching UserMark/Note guid sets),
  and diff that device's upload against *that* version from History, then apply the diff to the latest master.
- First upload from a never-seen device: no base → plain union (add only, no deletes).
