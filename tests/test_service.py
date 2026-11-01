import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import base_dataset, clone, loc, note

from jwlkit import Dataset, open_backup, read, write_backup
from jwlsync import HISTORY, MERGED, SAVE, UPLOADS, USER, LocalStorage, process


class Device:
    """A JW Library install: backs up into the Save folder and restores the current merged file."""

    def __init__(self, root: Path, name: str, ds: Dataset | None = None):
        self.root, self.name, self.ds, self.n = root, name, ds or Dataset(), 0

    def backup(self) -> None:
        self.n += 1
        write_backup(self.ds, self.root / SAVE / f"UserdataBackup_{self.name}_{self.n}.jwlibrary",
                     device_name=self.name)

    def restore(self) -> None:
        with open_backup(current_file(self.root)) as b:
            self.ds = read(b)


def state(root: Path) -> dict:
    return json.loads((root / USER / "state.json").read_text(encoding="utf-8"))


def current_file(root: Path) -> Path:
    s = state(root)
    return root / s["merges"][s["current"]]["id"]


def merged(root: Path) -> Dataset:
    with open_backup(current_file(root)) as b:
        return read(b)


class Service(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.storage = LocalStorage(self.root)
        self.storage.setup()
        self.g1 = loc()[0]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_once(self):
        return process(self.storage, settle=0)

    def saves(self):
        return sorted(p.name for p in (self.root / SAVE).iterdir())

    def test_first_upload_becomes_the_stamped_merged_file(self):
        Device(self.root, "PC", base_dataset()).backup()
        [r] = self.run_once()
        self.assertEqual((r.outcome, r.base, r.device), ("merged", None, "PC"))
        self.assertEqual(self.saves(), [r.merged])
        out = merged(self.root)
        self.assertEqual(out.notes, base_dataset().notes)
        self.assertEqual(out.stamp["merge_id"], state(self.root)["current"])
        self.assertEqual(len(list((self.root / UPLOADS).iterdir())), 1)
        self.assertEqual(self.run_once(), [])

    def test_deletion_applies_without_touching_other_devices_work(self):
        pc, phone = Device(self.root, "PC", base_dataset()), Device(self.root, "Phone")
        pc.backup()
        self.run_once()
        pc.restore()
        phone.restore()
        phone.ds.notes["from-phone"] = note(self.g1, "phone")
        phone.backup()
        self.run_once()
        del pc.ds.notes["n2"]
        pc.ds.tagmaps = {k: v for k, v in pc.ds.tagmaps.items() if k[1] != ("n", "n2")}
        pc.backup()
        [r] = self.run_once()
        self.assertEqual(r.base.method, "stamp")
        out = merged(self.root)
        self.assertNotIn("n2", out.notes)
        self.assertIn("from-phone", out.notes)
        self.assertEqual(len(self.saves()), 1)

    def test_device_without_base_adds_but_never_deletes(self):
        Device(self.root, "PC", base_dataset()).backup()
        self.run_once()
        tablet = Dataset()
        tablet.locations = {self.g1: loc()[1]}
        tablet.notes = {"t1": note(self.g1, "tablet")}
        Device(self.root, "Tablet", tablet).backup()
        [r] = self.run_once()
        self.assertIsNone(r.base)
        out = merged(self.root)
        self.assertTrue({"n1", "n2", "t1"} <= set(out.notes))

    def test_backup_right_after_restore_changes_nothing(self):
        pc = Device(self.root, "PC", base_dataset())
        pc.backup()
        [first] = self.run_once()
        pc.restore()
        pc.backup()
        [r] = self.run_once()
        self.assertEqual(r.outcome, "unchanged")
        self.assertEqual(self.saves(), [first.merged])

    def test_missing_stamp_falls_back_to_content(self):
        pc = Device(self.root, "PC", base_dataset())
        pc.backup()
        self.run_once()
        pc.restore()
        pc.ds.stamp = {}
        pc.ds.notes["n9"] = note(self.g1, "new")
        pc.backup()
        [r] = self.run_once()
        self.assertEqual(r.base.method, "content")

    def test_several_uploads_in_one_pass_merge_in_order(self):
        pc, phone = Device(self.root, "PC", base_dataset()), Device(self.root, "Phone", clone(base_dataset()))
        phone.ds.notes["p1"] = note(self.g1, "phone")
        pc.backup()
        phone.backup()
        results = self.run_once()
        self.assertEqual([r.outcome for r in results], ["merged", "merged"])
        self.assertEqual(len(self.saves()), 1)
        self.assertIn("p1", merged(self.root).notes)
        self.assertEqual(state(self.root)["seq"], 1)  # one merged file per batch

    def test_unreadable_file_is_rejected_and_kept(self):
        Device(self.root, "PC", base_dataset()).backup()
        self.run_once()
        (self.root / SAVE / "broken.jwlibrary").write_bytes(b"not a zip")
        [r] = self.run_once()
        self.assertEqual(r.outcome, "rejected")
        self.assertTrue(any("rejected broken" in p.name for p in (self.root / UPLOADS).iterdir()))
        self.assertEqual(len(self.saves()), 1)

    def test_files_still_changing_wait(self):
        Device(self.root, "PC", base_dataset()).backup()
        self.assertEqual(process(self.storage, settle=60), [])

    def test_history_and_log(self):
        pc = Device(self.root, "PC", base_dataset())
        pc.backup()
        [first] = self.run_once()
        pc.ds.notes["n9"] = note(self.g1, "more")
        pc.backup()
        self.run_once()
        self.assertIn(first.merged, {p.name for p in (self.root / MERGED).iterdir()})
        self.assertEqual(sum("UserdataBackup_PC" in p.name for p in (self.root / UPLOADS).iterdir()), 2)
        log = (self.root / USER / "merge-log.txt").read_text(encoding="utf-8")
        self.assertEqual(log.count("UserdataBackup_PC"), 2)

    def test_setup_moves_existing_files_to_history(self):
        root = Path(tempfile.mkdtemp())
        try:
            (root / "old.jwlibrary").write_bytes(b"x")
            LocalStorage(root).setup()
            self.assertEqual(sorted(p.name for p in root.iterdir()), sorted([SAVE, HISTORY, USER]))
            self.assertTrue((root / HISTORY / "old.jwlibrary").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)


@unittest.skipUnless(os.environ.get("JWLKIT_TESTDATA"), "set JWLKIT_TESTDATA to a folder of .jwlibrary files")
class RealReplay(unittest.TestCase):
    """Feed the real backups through the service in the order they were made."""

    def test_replay(self):
        folder = Path(os.environ["JWLKIT_TESTDATA"])
        dated = []
        for path in folder.glob("*.jwlibrary"):
            with open_backup(path) as b:
                dated.append((b.last_modified, path.name, path))
        root = Path(tempfile.mkdtemp())
        try:
            storage = LocalStorage(root)
            storage.setup()
            results = []
            for _, _, path in sorted(dated):
                shutil.copyfile(path, root / SAVE / path.name)
                results += process(storage, settle=0)
            self.assertEqual(len(results), len(dated))
            self.assertFalse([r for r in results if r.outcome == "rejected"])
            out = merged(root)
            self.assertTrue(out.notes)
            if os.environ.get("JWLSYNC_VERBOSE"):
                for r in results:
                    base = f"{r.base.method} {r.base.score:.2f}" if r.base else "-"
                    print(f"\n{r.upload:58} {r.outcome:9} base={base}  {r.summary.splitlines()[0] if r.summary else ''}",
                          end="")
                print("\nfinal:", out.counts())
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
