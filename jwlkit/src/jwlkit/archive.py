"""Opening and writing .jwlibrary archives.

An archive is a zip holding manifest.json, userData.db (SQLite), default_thumbnail.png and any
playlist media files. Newer Android backups also include userData.db-wal / -shm; the WAL can hold
data that is not in userData.db yet, so all userData.db* members are extracted together and the
WAL is checkpointed into the main file before anything reads it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

MANIFEST = "manifest.json"
DB_NAME = "userData.db"
THUMBNAIL = "default_thumbnail.png"
SCHEMA_VERSION = 16
MIN_SCHEMA_VERSION = 13


class BackupError(Exception):
    pass


def parse_time(value: str | None) -> datetime | None:
    """Parse a JW Library timestamp to a UTC datetime.

    Platforms write ...Z, ...+00:00 or ...-0400, so the strings can't be compared directly."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Backup:
    """An extracted archive. Use as a context manager so the temp dir is removed."""

    path: Path
    manifest: dict
    workdir: Path
    db_path: Path
    media: dict[str, Path] = field(default_factory=dict)  # archive member name -> extracted file

    @property
    def device_name(self) -> str:
        return self.manifest.get("userDataBackup", {}).get("deviceName", "")

    @property
    def schema_version(self) -> int:
        return int(self.manifest.get("userDataBackup", {}).get("schemaVersion", 0))

    @property
    def last_modified(self) -> datetime | None:
        """When this backup's data last changed, in UTC: the later of the database's LastModified
        row and the manifest's lastModifiedDate. Some devices don't keep these current, so treat
        it as a hint."""
        times = [parse_time(self.manifest.get("userDataBackup", {}).get("lastModifiedDate"))]
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT LastModified FROM LastModified").fetchone()
            times.append(parse_time(row[0]) if row else None)
        except sqlite3.Error:
            pass
        finally:
            conn.close()
        times = [t for t in times if t]
        return max(times) if times else None

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self) -> "Backup":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _safe_member(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")


def open_backup(path: str | Path) -> Backup:
    path = Path(path)
    if not zipfile.is_zipfile(path):
        raise BackupError(f"{path.name}: not a zip archive")
    workdir = Path(tempfile.mkdtemp(prefix="jwlkit-"))
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if MANIFEST not in names:
                raise BackupError(f"{path.name}: no manifest.json")
            manifest = json.loads(z.read(MANIFEST))
            udb = manifest.get("userDataBackup")
            if manifest.get("version") != 1 or not isinstance(udb, dict):
                raise BackupError(f"{path.name}: unsupported manifest")
            schema = int(udb.get("schemaVersion", 0))
            if not MIN_SCHEMA_VERSION <= schema <= SCHEMA_VERSION:
                raise BackupError(f"{path.name}: schema version {schema} not supported "
                                  f"({MIN_SCHEMA_VERSION}-{SCHEMA_VERSION})")
            db_name = udb.get("databaseName") or DB_NAME
            if db_name not in names:
                raise BackupError(f"{path.name}: {db_name} missing")

            media: dict[str, Path] = {}
            for name in names:
                if not _safe_member(name):
                    continue
                if name.startswith(db_name):
                    target = workdir / name.replace(db_name, DB_NAME, 1)
                elif name in (MANIFEST, THUMBNAIL):
                    continue
                else:
                    target = workdir / "media" / name
                    media[name] = target
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

        db_path = workdir / DB_NAME
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError(f"{path.name}: database failed integrity check")
        finally:
            conn.close()
        return Backup(path=path, manifest=manifest, workdir=workdir, db_path=db_path, media=media)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def create_empty_db(db_path: Path) -> sqlite3.Connection:
    """Create a fresh schema-16 userData.db and return a connection to it."""
    sql = resources.files("jwlkit").joinpath("schema16.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.executescript(sql)
    return conn


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pack(db_path: Path, out_path: str | Path, media: dict[str, Path] | None = None,
         device_name: str = "JWLSync") -> dict:
    """Finalize db_path and write it, with media, as a .jwlibrary archive. Returns the manifest."""
    out_path = Path(out_path)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE LastModified SET LastModified = ?", (stamp,))
        conn.commit()
        conn.execute("VACUUM")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()
    manifest = {
        "name": out_path.name,
        "creationDate": now.strftime("%Y-%m-%d"),
        "version": 1,
        "type": 0,
        "userDataBackup": {
            "lastModifiedDate": stamp,
            "deviceName": device_name,
            "databaseName": DB_NAME,
            "hash": sha256_file(db_path),
            "schemaVersion": SCHEMA_VERSION,
        },
    }
    tmp_out = out_path.with_name(out_path.name + ".tmp")
    with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db_path, DB_NAME)
        z.writestr(THUMBNAIL, resources.files("jwlkit").joinpath(THUMBNAIL).read_bytes())
        for name, src in sorted((media or {}).items()):
            z.write(src, name)
        z.writestr(MANIFEST, json.dumps(manifest, separators=(",", ":")))
    tmp_out.replace(out_path)
    return manifest
