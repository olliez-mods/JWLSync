# JWLSync

Cloud backup + cross-device merge for JW Library, driven entirely through a Google Drive folder.
JW Library has no cloud sync; it can only export/import `.jwlibrary` backup files. JWLSync watches a
Drive folder, merges each new backup into the running "master" copy, and leaves one up-to-date file
that any device can restore from.

## How it works (user's view)

1. User creates a Google Drive folder, sets sharing to **"Anyone with the link can edit"**, and pastes
   the link into the JWLSync web page. That's the whole setup.
2. JWLSync sets the folder up with three subfolders:
   - `JW Library Save` — where devices save backups to and restore from. Contains the current merged file.
   - `History` — every raw upload and every previous merged file.
   - `User` — per-user files (merge log, "what changed" reports, config).
   Existing contents are **not deleted** — they get moved into `History` (or setup requires an empty folder).
3. User backs up from any device (phone, PC, tablet) into `JW Library Save`.
4. Within ~10 seconds JWLSync merges it with the last merged file, writes a new merged file into
   `JW Library Save`, and moves the raw upload + the previous merged file into `History`.
5. Any device restores from the single merged file whenever it wants the full, combined state.

User habit to document: **back up → wait for merge → restore.** Restoring replaces device data, so
anything changed on a device since its last backup is lost if it restores without backing up first.

## Key design decisions

### Merge: 3-way, not union
Naive merge (union of both files, like JWLMerge) resurrects deletions. JWLSync uses the **last merged
file as the common base**:
- in base, missing from upload → deleted on that device → delete
- in upload, not in base → new → add
- changed on both sides → newer wins (Note has LastModified; others need per-table rules)

The merged file always comes from the base + the upload's diff, so `History` must keep the last merged
file reliably (tag it via Drive `appProperties`).

### Drive access: link-shared folder + bot account
- Users share by **link** (anyone with link = editor). No per-user OAuth.
- The server acts as a **dedicated bot Google account** (OAuth once, store refresh token).
  Service accounts can't be used — they have no storage quota, so they can't create files in a user's folder.
- Files the bot creates are owned by the bot and count against **its** 15 GB, not the user's.
  Backups are ~0.2–9 MB (playlists embed media). Prune History if quota becomes an issue.
- Folder ID is parsed from the pasted link; handle `resourcekey` param for older links.

### Change detection: webhooks (+ backstop poll)
- `files.watch` on each user's `JW Library Save` folder (not `changes.watch` — the bot's change feed
  doesn't reliably cover link-shared folders it doesn't own). Verify children-added notifications fire early.
- HTTPS endpoint on our own domain, cert via Certbot.
- Watch channels expire (~1 day for files) → renewal job.
- Notifications aren't guaranteed → slow backstop poll (~15 min).
- Debounce: notifications can fire mid-upload; wait until size/md5 is stable before merging.
- Ignore files JWLSync itself wrote (mark with `appProperties`) to avoid merge loops.
- Target: upload → merged file in under 10 seconds.

## `.jwlibrary` format
Full details, natural keys per table, how JWLMerge / go-jwlm / JWLManager merge, and JWLSync's
merge rules: **`docs/format-notes.md`**. Highlights:
- Zip: `manifest.json`, `userData.db`, `default_thumbnail.png`, playlist media, and on newer Android
  **`userData.db-wal`/`-shm`** — must replay the WAL or data is lost.
- Manifest `hash` = SHA-256 of `userData.db`; `schemaVersion` = `PRAGMA user_version`.
- Schemas 13/14/16 in real data. Normalize all inputs up to 16 (JWLManager's upgrade), output 16.
- Integer IDs are per-device; identity = GUIDs (Note, UserMark) or natural keys (Location, Tag, ...).
- Per-device base tracking needed so a stale device doesn't "delete" other devices' work.

Test data: `testdata/originals/` — read-only copies of the user's real backups. Never modify.

## Stack
- Python, FastAPI, built-in `sqlite3`, Google Drive API (`google-api-python-client`).
- One process: setup page + webhook endpoint + renewal/backstop jobs.
- Small SQLite DB for app state (users, folder IDs, watch channels, last-merged file IDs).
- Intended host: server-tower (alongside Foreman).

## Plan
1. **Merge engine (CLI)** — riskiest part, do first. Map the schema from real backups, build the 3-way
   merge, round-trip test: merged file must import cleanly into JW Library.
   Test data: the user's real backups in their Drive `JW Library` folder (PC + two Samsung phones),
   downloaded as **read-only copies** — never modify the originals.
2. **Drive layer** — bot account auth, open folder by link, setup of the three subfolders, upload/move/tag files.
3. **Change detection** — webhook endpoint, channel renewal job, backstop poll, debounce, loop guard.
4. **Setup web page** — paste link → validate access → set up folder → start watching.

## Layout
- `jwlkit/` — the merge engine as a standalone, dependency-free Python package (own pyproject,
  README, CLI, tests). Usable outside JWLSync; the service will import it.
- `jwlsync/` — the service. `storage.py`: `Storage` interface over the three folders +
  `LocalStorage` (a directory on disk); the Drive layer will be a second implementation.
  `service.py`: `process(storage)` merges every settled upload in `JW Library Save`.
  CLI: `python -m jwlsync {setup,process,watch} FOLDER`.
- `tests/` — service tests (simulated devices + replay of the real backups).
- `docs/format-notes.md` — format + merge research.
- `testdata/originals/` — the user's real backups, read-only. Personal data: never publish/commit.

Run tests:
- `cd jwlkit && PYTHONPATH="src;tests" JWLKIT_TESTDATA=../testdata/originals python -m unittest discover -s tests`
- `PYTHONPATH="jwlkit/src;jwlkit/tests;." JWLKIT_TESTDATA=testdata/originals python -m unittest discover -s tests`
  (add `JWLSYNC_VERBOSE=1` to print the replay's per-upload decisions)

## Stamping (how the base is found)
Restoring keeps the database file itself: in the test data, rows of restored go-jwlm merges came
back in later iPad/PC backups with identical integer IDs, and the iPad even kept go-jwlm's schema
text. So every merged file gets an extra `JWLSync(Key, Value)` table (merge_id, seq, created,
source, base) and the device's next backup carries it back. Base = stamp → else content match
(≥98% of a recent merged file's entries present, `jwlkit.base`) → else none (union, no deletes).
App state in `User/state.json` (current merge id, merge id → file id), log in `User/merge-log.txt`.
Unverified: that the extra table survives restore + backup on each platform (esp. Android, and
app schema upgrades) — check alongside the device restore test.

## Status
- [x] 1. Merge engine (`jwlkit`) — 40 tests pass, all 20 real backups round-trip identically.
      Real 3-way check: PC upload vs the merge it restored applied exactly its 13 changes.
      Tag/playlist renames detected by item overlap and carried across devices (2026-10-31).
      Base detection via stamp + content fallback (2026-10-31).
      Not yet done: restoring a jwlkit-written file on real devices (Android/iOS/Windows).
- [x] 1b. Service core (`jwlsync`) on local storage — 11 tests pass incl. real-backup replay (2026-10-31).
- [ ] 2. Drive layer (implement `Storage` on Drive)
- [ ] 3. Change detection
- [ ] 4. Setup page

## Open questions
- Merge rules per table beyond Note (UserMark colour changes, tag ordering, playlist edits).
- Handling of schema-version mismatches between devices (refuse vs. migrate).
- History retention policy.
- Domain/hostname for the webhook endpoint.
