"""A device-independent view of a userData.db.

Integer IDs in userData.db are local to one device, so they can't be compared across backups.
`Dataset` replaces every ID with a natural key (GUIDs for notes and highlights, column values for
locations, tags, ...), which makes two backups directly comparable table by table. `read_dataset`
builds one from a database and `write_dataset` turns one back into a fresh schema-16 database.

Keys used:
    Location   (Book, Chapter, DocumentId, Track, IssueTagNumber, KeySymbol, MepsLanguage, Type,
                Specialty, Edition), with NULL ints as 0 and NULL text as ""
    UserMark   UserMarkGuid (its BlockRanges are part of the value)
    Note       Guid
    Tag        (Type, Name); the single type-0 "Favorite" tag is (0, "") since its name is localized
    TagMap     (tag key, ("n", note guid) | ("l", location key))
    Bookmark   (publication location key, Slot)
    InputField (location key, TextTag)
    Playlist   tag key of a type-2 tag; the value is its ordered tuple of items
    Media      IndependentMedia.Hash
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .archive import create_empty_db

LOCATION_COLS = ("BookNumber", "ChapterNumber", "DocumentId", "Track", "IssueTagNumber", "KeySymbol",
                 "MepsLanguage", "Type", "Title", "Specialty", "Edition")

TAG_FAVORITE = 0
TAG_USER = 1
TAG_PLAYLIST = 2

# Tables merged entry-by-entry, in the order they're written.
TABLES = ("tags", "usermarks", "notes", "tagmaps", "bookmarks", "inputfields", "playlists")


def location_key(row) -> tuple:
    def i(v):
        return v or 0

    def s(v):
        return v or ""

    return (i(row["BookNumber"]), i(row["ChapterNumber"]), i(row["DocumentId"]), i(row["Track"]),
            i(row["IssueTagNumber"]), s(row["KeySymbol"]), row["MepsLanguage"], row["Type"],
            s(row["Specialty"]), s(row["Edition"]))


def tag_key(tag_type: int, name: str) -> tuple:
    return (TAG_FAVORITE, "") if tag_type == TAG_FAVORITE else (tag_type, name)


@dataclass
class Dataset:
    tags: dict[tuple, dict] = field(default_factory=dict)          # key -> {"name"}
    usermarks: dict[str, dict] = field(default_factory=dict)
    notes: dict[str, dict] = field(default_factory=dict)
    tagmaps: dict[tuple, int] = field(default_factory=dict)        # key -> position
    bookmarks: dict[tuple, dict] = field(default_factory=dict)
    inputfields: dict[tuple, str] = field(default_factory=dict)
    playlists: dict[tuple, tuple] = field(default_factory=dict)    # tag key -> items
    # Lookup data, not merged directly: full rows for every location key and media hash seen,
    # and where each media file's bytes live on disk.
    locations: dict[tuple, dict] = field(default_factory=dict)
    media: dict[str, dict] = field(default_factory=dict)
    media_files: dict[str, Path] = field(default_factory=dict)
    # When the source backup last changed (Backup.last_modified); used by prefer="newer" and combine.
    last_modified: datetime | None = None

    def counts(self) -> dict[str, int]:
        return {t: len(getattr(self, t)) for t in TABLES}


def _rows(conn: sqlite3.Connection, table: str, order: str = "") -> list[sqlite3.Row]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    return conn.execute(f'SELECT * FROM "{table}" {order}').fetchall()


def _get(row: sqlite3.Row, col: str):
    return row[col] if col in row.keys() else None


def read_dataset(conn: sqlite3.Connection, media_dir: dict[str, Path] | None = None) -> Dataset:
    conn.row_factory = sqlite3.Row
    ds = Dataset()

    loc_by_id: dict[int, tuple] = {}
    for r in _rows(conn, "Location"):
        row = {c: _get(r, c) for c in LOCATION_COLS}
        key = location_key(row)
        loc_by_id[r["LocationId"]] = key
        ds.locations.setdefault(key, row)

    tag_by_id: dict[int, tuple] = {}
    for r in _rows(conn, "Tag"):
        key = tag_key(r["Type"], r["Name"])
        tag_by_id[r["TagId"]] = key
        ds.tags.setdefault(key, {"name": r["Name"]})

    ranges: dict[int, list] = {}
    for r in _rows(conn, "BlockRange"):
        ranges.setdefault(r["UserMarkId"], []).append(
            (r["BlockType"], r["Identifier"], r["StartToken"], r["EndToken"]))
    mark_by_id: dict[int, str] = {}
    for r in _rows(conn, "UserMark"):
        if r["LocationId"] not in loc_by_id:
            continue
        mark_by_id[r["UserMarkId"]] = r["UserMarkGuid"]
        ds.usermarks[r["UserMarkGuid"]] = {
            "color": r["ColorIndex"], "style": r["StyleIndex"], "version": r["Version"],
            "loc": loc_by_id[r["LocationId"]],
            "ranges": tuple(sorted(ranges.get(r["UserMarkId"], []), key=repr)),
        }

    note_by_id: dict[int, str] = {}
    for r in _rows(conn, "Note"):
        note_by_id[r["NoteId"]] = r["Guid"]
        ds.notes[r["Guid"]] = {
            "usermark": mark_by_id.get(r["UserMarkId"]),
            "loc": loc_by_id.get(r["LocationId"]),
            "title": r["Title"], "content": r["Content"],
            "modified": r["LastModified"], "created": r["Created"],
            "block_type": r["BlockType"], "block_id": r["BlockIdentifier"],
        }

    for r in _rows(conn, "Bookmark"):
        if r["PublicationLocationId"] not in loc_by_id or r["LocationId"] not in loc_by_id:
            continue
        ds.bookmarks[(loc_by_id[r["PublicationLocationId"]], r["Slot"])] = {
            "loc": loc_by_id[r["LocationId"]], "title": r["Title"], "snippet": r["Snippet"],
            "block_type": r["BlockType"], "block_id": r["BlockIdentifier"],
        }

    for r in _rows(conn, "InputField"):
        if r["LocationId"] in loc_by_id:
            ds.inputfields[(loc_by_id[r["LocationId"]], r["TextTag"])] = r["Value"]

    media_by_id: dict[int, str] = {}
    media_by_path: dict[str, str] = {}
    for r in _rows(conn, "IndependentMedia"):
        media_by_id[r["IndependentMediaId"]] = r["Hash"]
        media_by_path[r["FilePath"]] = r["Hash"]
        ds.media[r["Hash"]] = {"filename": r["OriginalFilename"], "path": r["FilePath"],
                               "mime": r["MimeType"]}
        if media_dir and r["FilePath"] in media_dir:
            ds.media_files[r["Hash"]] = media_dir[r["FilePath"]]

    items = _read_playlist_items(conn, loc_by_id, media_by_id, media_by_path)
    playlist_items: dict[tuple, list] = {}
    for r in _rows(conn, "TagMap"):
        tkey = tag_by_id.get(r["TagId"])
        if tkey is None:
            continue
        if r["PlaylistItemId"] is not None:
            if tkey[0] == TAG_PLAYLIST and r["PlaylistItemId"] in items:
                playlist_items.setdefault(tkey, []).append((r["Position"], items[r["PlaylistItemId"]]))
        elif r["NoteId"] is not None:
            if r["NoteId"] in note_by_id:
                ds.tagmaps[(tkey, ("n", note_by_id[r["NoteId"]]))] = r["Position"]
        elif r["LocationId"] in loc_by_id:
            ds.tagmaps[(tkey, ("l", loc_by_id[r["LocationId"]]))] = r["Position"]
    # Devices leave gaps in Position; only the order matters, so store ranks 0..n-1 per tag.
    by_tag: dict[tuple, list] = {}
    for key, pos in ds.tagmaps.items():
        by_tag.setdefault(key[0], []).append((pos, repr(key[1]), key))
    for entries in by_tag.values():
        for rank, (_, _, key) in enumerate(sorted(entries)):
            ds.tagmaps[key] = rank
    for tkey in ds.tags:
        if tkey[0] == TAG_PLAYLIST:
            ds.playlists[tkey] = tuple(item for _, item in sorted(playlist_items.get(tkey, []), key=lambda p: p[0]))
    return ds


def _read_playlist_items(conn, loc_by_id, media_by_id, media_by_path) -> dict[int, dict]:
    locmaps: dict[int, list] = {}
    for r in _rows(conn, "PlaylistItemLocationMap"):
        if r["LocationId"] in loc_by_id:
            locmaps.setdefault(r["PlaylistItemId"], []).append(
                (loc_by_id[r["LocationId"]], r["MajorMultimediaType"], r["BaseDurationTicks"]))
    mediamaps: dict[int, list] = {}
    for r in _rows(conn, "PlaylistItemIndependentMediaMap"):
        if r["IndependentMediaId"] in media_by_id:
            mediamaps.setdefault(r["PlaylistItemId"], []).append(
                (media_by_id[r["IndependentMediaId"]], r["DurationTicks"]))
    verses: dict[int, list] = {}
    for r in _rows(conn, "PlaylistItemMarkerBibleVerseMap"):
        verses.setdefault(r["PlaylistItemMarkerId"], []).append(r["VerseId"])
    paras: dict[int, list] = {}
    for r in _rows(conn, "PlaylistItemMarkerParagraphMap"):
        paras.setdefault(r["PlaylistItemMarkerId"], []).append(
            (r["MepsDocumentId"], r["ParagraphIndex"], r["MarkerIndexWithinParagraph"]))
    markers: dict[int, list] = {}
    for r in _rows(conn, "PlaylistItemMarker"):
        mid = r["PlaylistItemMarkerId"]
        markers.setdefault(r["PlaylistItemId"], []).append(
            (r["Label"], r["StartTimeTicks"], r["DurationTicks"], r["EndTransitionDurationTicks"],
             tuple(sorted(verses.get(mid, []))), tuple(sorted(paras.get(mid, [])))))
    items = {}
    for r in _rows(conn, "PlaylistItem"):
        pid = r["PlaylistItemId"]
        items[pid] = {
            "label": r["Label"], "start_trim": r["StartTrimOffsetTicks"], "end_trim": r["EndTrimOffsetTicks"],
            "accuracy": r["Accuracy"], "end_action": r["EndAction"],
            "thumbnail": media_by_path.get(r["ThumbnailFilePath"]),
            "locations": tuple(sorted(locmaps.get(pid, []), key=repr)),
            "media": tuple(sorted(mediamaps.get(pid, []))),
            "markers": tuple(sorted(markers.get(pid, []), key=repr)),
        }
    return items


class _Writer:
    def __init__(self, conn: sqlite3.Connection, ds: Dataset):
        self.conn = conn
        self.ds = ds
        self.loc_ids: dict[tuple, int] = {}
        self.skipped = 0

    def loc(self, key: tuple | None) -> int | None:
        if key is None:
            return None
        if key in self.loc_ids:
            return self.loc_ids[key]
        row = self.ds.locations[key]
        cols = ", ".join(LOCATION_COLS)
        try:
            cur = self.conn.execute(f"INSERT INTO Location ({cols}) VALUES ({', '.join('?' * len(LOCATION_COLS))})",
                                    [row[c] for c in LOCATION_COLS])
            lid = cur.lastrowid
        except sqlite3.IntegrityError:
            # Two keys that differ only in fields outside a UNIQUE constraint; reuse the existing row.
            lid = self.conn.execute(
                "SELECT LocationId FROM Location WHERE "
                "(BookNumber IS ? AND ChapterNumber IS ? AND KeySymbol IS ? AND MepsLanguage IS ? AND Type IS ?) OR "
                "(KeySymbol IS ? AND IssueTagNumber IS ? AND MepsLanguage IS ? AND DocumentId IS ? AND Track IS ? "
                " AND Type IS ? AND COALESCE(Specialty,'') = ? AND COALESCE(Edition,'') = ?)",
                (row["BookNumber"], row["ChapterNumber"], row["KeySymbol"], row["MepsLanguage"], row["Type"],
                 row["KeySymbol"], row["IssueTagNumber"], row["MepsLanguage"], row["DocumentId"], row["Track"],
                 row["Type"], row["Specialty"] or "", row["Edition"] or "")).fetchone()[0]
        self.loc_ids[key] = lid
        return lid

    def insert(self, sql: str, params) -> int | None:
        try:
            return self.conn.execute(sql, params).lastrowid
        except sqlite3.IntegrityError:
            self.skipped += 1
            return None

    def write(self) -> None:
        ds, c = self.ds, self.conn
        tag_ids = {}
        for key in sorted(ds.tags, key=repr):
            tag_ids[key] = c.execute("INSERT INTO Tag (Type, Name) VALUES (?, ?)",
                                     (key[0], ds.tags[key]["name"])).lastrowid

        mark_ids = {}
        for guid in sorted(ds.usermarks):
            m = ds.usermarks[guid]
            mid = c.execute("INSERT INTO UserMark (ColorIndex, LocationId, StyleIndex, UserMarkGuid, Version) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (m["color"], self.loc(m["loc"]), m["style"], guid, m["version"])).lastrowid
            mark_ids[guid] = mid
            for bt, ident, start, end in m["ranges"]:
                c.execute("INSERT INTO BlockRange (BlockType, Identifier, StartToken, EndToken, UserMarkId) "
                          "VALUES (?, ?, ?, ?, ?)", (bt, ident, start, end, mid))

        note_ids = {}
        for guid in sorted(ds.notes):
            n = ds.notes[guid]
            note_ids[guid] = c.execute(
                "INSERT INTO Note (Guid, UserMarkId, LocationId, Title, Content, LastModified, Created, "
                "BlockType, BlockIdentifier) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guid, mark_ids.get(n["usermark"]), self.loc(n["loc"]), n["title"], n["content"],
                 n["modified"], n["created"], n["block_type"], n["block_id"])).lastrowid

        by_tag: dict[tuple, list] = {}
        for (tkey, target), pos in ds.tagmaps.items():
            by_tag.setdefault(tkey, []).append((pos, repr(target), target))
        for tkey, entries in sorted(by_tag.items(), key=lambda kv: repr(kv[0])):
            for pos, (_, _, target) in enumerate(sorted(entries, key=lambda e: (e[0], e[1]))):
                kind, ref = target
                note_id = note_ids[ref] if kind == "n" else None
                loc_id = self.loc(ref) if kind == "l" else None
                self.insert("INSERT INTO TagMap (NoteId, LocationId, TagId, Position) VALUES (?, ?, ?, ?)",
                            (note_id, loc_id, tag_ids[tkey], pos))

        for (pub, slot), b in sorted(ds.bookmarks.items(), key=lambda kv: repr(kv[0])):
            self.insert("INSERT INTO Bookmark (LocationId, PublicationLocationId, Slot, Title, Snippet, "
                        "BlockType, BlockIdentifier) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (self.loc(b["loc"]), self.loc(pub), slot, b["title"], b["snippet"],
                         b["block_type"], b["block_id"]))

        for (lkey, tag), value in sorted(ds.inputfields.items(), key=lambda kv: repr(kv[0])):
            self.insert("INSERT INTO InputField (LocationId, TextTag, Value) VALUES (?, ?, ?)",
                        (self.loc(lkey), tag, value))

        self._write_playlists(tag_ids)

    def _write_playlists(self, tag_ids) -> None:
        ds, c = self.ds, self.conn
        media_ids = {}

        def media(h):
            if h not in media_ids:
                m = ds.media[h]
                media_ids[h] = c.execute("INSERT INTO IndependentMedia (OriginalFilename, FilePath, MimeType, Hash) "
                                         "VALUES (?, ?, ?, ?)", (m["filename"], m["path"], m["mime"], h)).lastrowid
            return media_ids[h]

        for tkey in sorted(ds.playlists, key=repr):
            for pos, it in enumerate(ds.playlists[tkey]):
                thumb = None
                if it["thumbnail"]:
                    media(it["thumbnail"])
                    thumb = ds.media[it["thumbnail"]]["path"]
                pid = c.execute("INSERT INTO PlaylistItem (Label, StartTrimOffsetTicks, EndTrimOffsetTicks, Accuracy, "
                                "EndAction, ThumbnailFilePath) VALUES (?, ?, ?, ?, ?, ?)",
                                (it["label"], it["start_trim"], it["end_trim"], it["accuracy"], it["end_action"],
                                 thumb)).lastrowid
                for lkey, mm_type, ticks in it["locations"]:
                    self.insert("INSERT INTO PlaylistItemLocationMap VALUES (?, ?, ?, ?)",
                                (pid, self.loc(lkey), mm_type, ticks))
                for h, ticks in it["media"]:
                    self.insert("INSERT INTO PlaylistItemIndependentMediaMap VALUES (?, ?, ?)", (pid, media(h), ticks))
                for label, start, dur, end_tr, verses, paras in it["markers"]:
                    mid = self.insert("INSERT INTO PlaylistItemMarker (PlaylistItemId, Label, StartTimeTicks, "
                                      "DurationTicks, EndTransitionDurationTicks) VALUES (?, ?, ?, ?, ?)",
                                      (pid, label, start, dur, end_tr))
                    if mid is None:
                        continue
                    for v in verses:
                        self.insert("INSERT INTO PlaylistItemMarkerBibleVerseMap VALUES (?, ?)", (mid, v))
                    for p in paras:
                        self.insert("INSERT INTO PlaylistItemMarkerParagraphMap VALUES (?, ?, ?, ?)", (mid, *p))
                c.execute("INSERT INTO TagMap (PlaylistItemId, TagId, Position) VALUES (?, ?, ?)",
                          (pid, tag_ids[tkey], pos))


def write_dataset(ds: Dataset, db_path: Path) -> tuple[dict[str, Path], int]:
    """Write ds into a new database at db_path.

    Returns the media files the archive must carry (member name -> source file) and the number of
    rows skipped because they collided with a UNIQUE constraint.
    """
    conn = create_empty_db(db_path)
    try:
        w = _Writer(conn, ds)
        w.write()
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise ValueError(f"foreign key violations in written database: {bad[:5]}")
        conn.commit()
        used = {h for h in ds.media if h in _used_media(ds)}
        files = {ds.media[h]["path"]: ds.media_files[h] for h in used if h in ds.media_files}
        return files, w.skipped
    finally:
        conn.close()


def _used_media(ds: Dataset) -> set[str]:
    used = set()
    for items in ds.playlists.values():
        for it in items:
            if it["thumbnail"]:
                used.add(it["thumbnail"])
            used.update(h for h, _ in it["media"])
    return used
