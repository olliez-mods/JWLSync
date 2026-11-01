import json
import tempfile
import unittest
from pathlib import Path

from fake_drive import FakeDrive
from helpers import base_dataset, loc, note

from jwlkit import Dataset, open_backup, read, write_backup
from jwlsync import HISTORY, MERGED, SAVE, UPLOADS, USER, process
from jwlsync.drive import DriveError, DriveStorage, open_folder, parse_folder_link

ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


class Links(unittest.TestCase):
    def test_link_forms(self):
        for link in (f"https://drive.google.com/drive/folders/{ID}?usp=sharing",
                     f"https://drive.google.com/drive/u/1/folders/{ID}",
                     f"https://drive.google.com/open?id={ID}",
                     f"https://drive.google.com/folderview?id={ID}",
                     f"  {ID} "):
            self.assertEqual(parse_folder_link(link), (ID, None), link)

    def test_resource_key(self):
        link = f"https://drive.google.com/drive/folders/{ID}?resourcekey=0-abcDEF&usp=sharing"
        self.assertEqual(parse_folder_link(link), (ID, "0-abcDEF"))

    def test_bad_links(self):
        for link in ("https://example.com/drive/folders/" + ID, "https://drive.google.com/drive/my-drive", "hello"):
            with self.assertRaises(DriveError, msg=link):
                parse_folder_link(link)


class OpenFolder(unittest.TestCase):
    def setUp(self):
        self.drive = FakeDrive()
        self.root = self.drive.add("JW sync", folder=True)["id"]

    def test_opens_editable_folder(self):
        self.assertEqual(open_folder(self.drive, self.root).root_id, self.root)

    def test_old_link_needs_its_resource_key(self):
        self.drive.required_keys[self.root] = "0-key"
        with self.assertRaises(DriveError):
            open_folder(self.drive, self.root)
        link = f"https://drive.google.com/drive/folders/{self.root}?resourcekey=0-key"
        self.assertEqual(open_folder(self.drive, link).root_id, self.root)

    def test_rejects_files_and_read_only_folders(self):
        file_id = self.drive.add("x.jwlibrary", self.root, b"x")["id"]
        with self.assertRaisesRegex(DriveError, "not a folder"):
            open_folder(self.drive, file_id)
        ro = self.drive.add("ro", folder=True, capabilities={"canAddChildren": False, "canEdit": False})["id"]
        with self.assertRaisesRegex(DriveError, "read-only"):
            open_folder(self.drive, ro)

    def test_missing_folder(self):
        with self.assertRaisesRegex(DriveError, "Anyone with the link"):
            open_folder(self.drive, "1NoSuchFolderxxxxxxxx")


class Storage(unittest.TestCase):
    def setUp(self):
        self.drive = FakeDrive()
        self.root = self.drive.add("JW sync", folder=True)["id"]

    def test_setup_creates_folders_and_moves_existing_files(self):
        old = self.drive.add("old backup.jwlibrary", self.root, b"x")["id"]
        DriveStorage(self.drive, self.root).setup()
        names = sorted(f["name"] for f in self.drive.children(self.root))
        self.assertEqual(names, sorted([SAVE, HISTORY, USER]))
        self.assertEqual(self.drive.path_of(old), [HISTORY, "old backup.jwlibrary"])
        DriveStorage(self.drive, self.root).setup()  # again: nothing new
        self.assertEqual(len(self.drive.children(self.root)), 3)

    def test_needs_setup(self):
        with self.assertRaisesRegex(DriveError, "setup"):
            DriveStorage(self.drive, self.root).list(SAVE)

    def test_text_files_are_updated_in_place(self):
        s = DriveStorage(self.drive, self.root)
        s.setup()
        self.assertIsNone(s.read_text("state.json"))
        s.write_text("state.json", "one")
        s.write_text("state.json", "two")
        fresh = DriveStorage(self.drive, self.root)
        self.assertEqual(fresh.read_text("state.json"), "two")
        fresh.write_text("state.json", "three")
        self.assertEqual(len(self.drive.children(fresh._folder(USER))), 1)

    def test_list_move_keep_ids(self):
        s = DriveStorage(self.drive, self.root)
        s.setup()
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a"
            src.write_bytes(b"abc")
            e = s.upload(SAVE, src, "a.jwlibrary")
        self.assertEqual([(x.id, x.size) for x in s.list(SAVE)], [(e.id, 3)])
        moved = s.move(e.id, HISTORY, "renamed.jwlibrary")
        self.assertEqual(moved.id, e.id)
        self.assertEqual(s.list(SAVE), [])
        self.assertEqual(self.drive.path_of(e.id), [HISTORY, "renamed.jwlibrary"])


class ServiceOnDrive(unittest.TestCase):
    """The same merge flow as test_service, against the fake Drive."""

    def setUp(self):
        self.drive = FakeDrive()
        root = self.drive.add("JW sync", folder=True)["id"]
        self.storage = open_folder(self.drive, f"https://drive.google.com/drive/folders/{root}")
        self.storage.setup()
        self.tmp = tempfile.TemporaryDirectory()
        self.g1 = loc()[0]

    def tearDown(self):
        self.tmp.cleanup()

    def backup(self, ds: Dataset, name: str) -> None:
        path = Path(self.tmp.name) / name
        write_backup(ds, path, device_name=name)
        self.drive.add(name, self.storage._folder(SAVE), path.read_bytes())  # untagged, like a device upload

    def restore(self) -> Dataset:
        state = json.loads(self.storage.read_text("state.json"))
        path = Path(self.tmp.name) / "restore.jwlibrary"
        self.drive.download(state["merges"][state["current"]]["id"], path)
        with open_backup(path) as b:
            return read(b)

    def test_deletion_applies_without_touching_other_devices_work(self):
        self.backup(base_dataset(), "pc1.jwlibrary")
        process(self.storage, settle=0)
        pc, phone = self.restore(), self.restore()
        phone.notes["from-phone"] = note(self.g1, "phone")
        self.backup(phone, "phone1.jwlibrary")
        process(self.storage, settle=0)
        del pc.notes["n2"]
        pc.tagmaps = {k: v for k, v in pc.tagmaps.items() if k[1] != ("n", "n2")}
        self.backup(pc, "pc2.jwlibrary")
        [r] = process(self.storage, settle=0)
        self.assertEqual(r.base.method, "stamp")
        out = self.restore()
        self.assertNotIn("n2", out.notes)
        self.assertIn("from-phone", out.notes)
        self.assertEqual(len(self.storage.list(SAVE)), 1)
        self.assertEqual(len(self.storage.list(UPLOADS)), 3)
        self.assertEqual(len(self.storage.list(MERGED)), 2)
        self.assertIn("pc2.jwlibrary", self.storage.read_text("merge-log.txt"))


if __name__ == "__main__":
    unittest.main()
