import unittest

from helpers import base_dataset, clone, loc, note, roundtrip

from jwlkit import find_base


class Stamp(unittest.TestCase):
    def test_stamp_survives_write_and_read(self):
        ds = base_dataset()
        ds.stamp = {"merge_id": "abc123", "seq": "7"}
        self.assertEqual(roundtrip(ds).stamp, {"merge_id": "abc123", "seq": "7"})

    def test_unstamped_file_has_empty_stamp(self):
        self.assertEqual(roundtrip(base_dataset()).stamp, {})


class FindBase(unittest.TestCase):
    def setUp(self):
        self.m1 = roundtrip(base_dataset())
        self.m2 = clone(self.m1)
        for i in range(10):
            self.m2.notes[f"x{i}"] = note(loc()[0], f"later {i}")

    def test_stamp_wins(self):
        upload = clone(self.m2)
        upload.stamp = {"merge_id": "m1"}
        self.assertEqual(find_base(upload, {"m1": self.m1, "m2": self.m2}).method, "stamp")
        self.assertEqual(find_base(upload, {"m1": self.m1, "m2": self.m2}).merge_id, "m1")

    def test_unknown_stamp_falls_back_to_content(self):
        upload = clone(self.m2)
        upload.stamp = {"merge_id": "pruned"}
        match = find_base(upload, {"m1": self.m1, "m2": self.m2})
        self.assertEqual((match.merge_id, match.method), ("m2", "content"))

    def test_content_prefers_the_later_file_it_covers(self):
        upload = clone(self.m2)
        upload.notes["new"] = note(loc()[0], "edit after restore")
        self.assertEqual(find_base(upload, {"m1": self.m1, "m2": self.m2}).merge_id, "m2")

    def test_no_base_when_upload_lacks_much_of_the_candidate(self):
        upload = clone(self.m1)
        del upload.notes["n1"], upload.usermarks["m1"], upload.usermarks["m2"]
        self.assertIsNone(find_base(upload, {"m1": self.m1}))

    def test_empty_candidate_never_matches(self):
        upload = clone(self.m1)
        empty = clone(self.m1)
        for t in ("notes", "usermarks", "bookmarks", "tagmaps", "inputfields"):
            setattr(empty, t, {})
        self.assertIsNone(find_base(upload, {"e": empty}))


if __name__ == "__main__":
    unittest.main()
