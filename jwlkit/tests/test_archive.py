import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from helpers import base_dataset, roundtrip

from jwlkit import open_backup, read, write_backup
from jwlkit.archive import create_empty_db, parse_time, sha256_file
from jwlkit.model import TABLES


class Archive(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_hash_matches_database(self):
        out = self.tmp / "x.jwlibrary"
        manifest = write_backup(base_dataset(), out)
        with zipfile.ZipFile(out) as z:
            z.extract("userData.db", self.tmp)
            self.assertEqual(json.loads(z.read("manifest.json")), manifest)
        self.assertEqual(manifest["userDataBackup"]["hash"], sha256_file(self.tmp / "userData.db"))
        self.assertEqual(manifest["userDataBackup"]["schemaVersion"], 16)

    def test_last_modified_is_read_and_timezone_aware(self):
        out = self.tmp / "x.jwlibrary"
        manifest = write_backup(base_dataset(), out)
        with open_backup(out) as b:
            self.assertEqual(b.last_modified, parse_time(manifest["userDataBackup"]["lastModifiedDate"]))
            self.assertEqual(read(b).last_modified, b.last_modified)
        self.assertEqual(parse_time("2026-02-01T14:00:00-0400"), parse_time("2026-02-01T18:00:00Z"))
        self.assertEqual(parse_time("2026-02-01T18:00:00+00:00"), parse_time("2026-02-01T18:00:00Z"))
        self.assertIsNone(parse_time("garbage"))

    def test_synthetic_roundtrip(self):
        ds = base_dataset()
        again = roundtrip(ds)
        for t in TABLES:
            self.assertEqual(getattr(ds, t), getattr(again, t), t)

    def test_rows_only_in_wal_are_read(self):
        """Newer Android backups ship userData.db-wal; its rows must not be lost."""
        src = self.tmp / "src"
        src.mkdir()
        db = src / "userData.db"
        conn = create_empty_db(db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO Tag (Type, Name) VALUES (1, 'only-in-wal')")
        conn.commit()
        # Copy while the connection is open so the WAL hasn't been folded into the db.
        archive = self.tmp / "wal.jwlibrary"
        manifest = {"name": "wal", "creationDate": "2026-01-01", "version": 1, "type": 0,
                    "userDataBackup": {"lastModifiedDate": "2026-01-01T00:00:00Z", "deviceName": "test",
                                       "databaseName": "userData.db", "hash": "x", "schemaVersion": 16}}
        with zipfile.ZipFile(archive, "w") as z:
            for name in ("userData.db", "userData.db-wal", "userData.db-shm"):
                if (src / name).exists():
                    z.write(src / name, name)
            z.writestr("manifest.json", json.dumps(manifest))
        self.assertTrue((src / "userData.db-wal").stat().st_size > 0)
        conn.close()
        with open_backup(archive) as b:
            self.assertIn((1, "only-in-wal"), read(b).tags)

    def test_rejects_path_traversal_members(self):
        archive = self.tmp / "evil.jwlibrary"
        good = self.tmp / "good.jwlibrary"
        write_backup(base_dataset(), good)
        with zipfile.ZipFile(good) as zin, zipfile.ZipFile(archive, "w") as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item))
            zout.writestr("../escape.txt", "x")
        with open_backup(archive) as b:
            self.assertFalse((b.workdir.parent / "escape.txt").exists())
            self.assertNotIn("../escape.txt", b.media)


@unittest.skipUnless(os.environ.get("JWLKIT_TESTDATA"), "set JWLKIT_TESTDATA to a folder of .jwlibrary files")
class RealBackups(unittest.TestCase):
    def test_every_backup_roundtrips(self):
        folder = Path(os.environ["JWLKIT_TESTDATA"])
        files = sorted(folder.glob("*.jwlibrary"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(path.name), open_backup(path) as b:
                ds = read(b)
                media_before = len(b.media)
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "rt.jwlibrary"
                    write_backup(ds, out)
                    with open_backup(out) as b2:
                        again = read(b2)
                        conn = sqlite3.connect(b2.db_path)
                        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                        conn.close()
                        self.assertEqual(len(b2.media), media_before)
                for t in TABLES:
                    self.assertEqual(getattr(ds, t), getattr(again, t), t)


if __name__ == "__main__":
    unittest.main()
