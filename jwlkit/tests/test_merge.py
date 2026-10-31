import unittest
from datetime import datetime, timezone

from helpers import base_dataset, clone, loc, mark, note, roundtrip

from jwlkit import combine, merge
from jwlkit.merge import find_renames


class ThreeWayMerge(unittest.TestCase):
    def setUp(self):
        self.base = roundtrip(base_dataset())
        self.g1 = loc()[0]
        self.g2 = loc(chapter=2)[0]

    def test_unchanged_upload_keeps_master(self):
        master = clone(self.base)
        master.notes["n9"] = note(self.g1, "from other device")
        out, rep = merge(master, clone(self.base), self.base)
        self.assertIn("n9", out.notes)
        self.assertEqual(rep.summary(), "no changes")

    def test_deletion_on_device_is_applied(self):
        upload = clone(self.base)
        del upload.usermarks["m2"]
        out, rep = merge(clone(self.base), upload, self.base)
        self.assertNotIn("m2", out.usermarks)
        self.assertEqual(rep.tables["usermarks"].removed, 1)

    def test_deleted_highlight_detaches_its_note(self):
        upload = clone(self.base)
        del upload.usermarks["m1"]
        out, _ = merge(clone(self.base), upload, self.base)
        self.assertIsNone(out.notes["n1"]["usermark"])

    def test_additions_from_both_sides_survive(self):
        master, upload = clone(self.base), clone(self.base)
        master.notes["nm"] = note(self.g1, "master")
        upload.notes["nu"] = note(self.g2, "upload")
        out, _ = merge(master, upload, self.base)
        self.assertTrue({"nm", "nu"} <= set(out.notes))

    def test_edit_on_device_is_applied(self):
        upload = clone(self.base)
        upload.usermarks["m3"] = {**upload.usermarks["m3"], "color": 5}
        out, rep = merge(clone(self.base), upload, self.base)
        self.assertEqual(out.usermarks["m3"]["color"], 5)
        self.assertEqual(rep.tables["usermarks"].changed, 1)

    def test_conflicting_note_edits_newer_wins_across_timezones(self):
        master, upload = clone(self.base), clone(self.base)
        # 14:00-0400 is 18:00Z, newer than the upload's 17:00Z.
        master.notes["n2"] = {**master.notes["n2"], "content": "master", "modified": "2026-02-01T14:00:00-0400"}
        upload.notes["n2"] = {**upload.notes["n2"], "content": "upload", "modified": "2026-02-01T17:00:00Z"}
        out, rep = merge(master, upload, self.base)
        self.assertEqual(out.notes["n2"]["content"], "master")
        self.assertEqual(rep.tables["notes"].conflicts, 1)

    def test_edit_beats_delete(self):
        master, upload = clone(self.base), clone(self.base)
        master.notes["n2"] = {**master.notes["n2"], "content": "edited", "modified": "2026-03-01T00:00:00Z"}
        del upload.notes["n2"]
        out, _ = merge(master, upload, self.base)
        self.assertEqual(out.notes["n2"]["content"], "edited")

        master, upload = clone(self.base), clone(self.base)
        del master.notes["n2"]
        upload.notes["n2"] = {**upload.notes["n2"], "content": "edited", "modified": "2026-03-01T00:00:00Z"}
        out, _ = merge(master, upload, self.base)
        self.assertEqual(out.notes["n2"]["content"], "edited")

    def test_deleted_tag_drops_its_tagmaps(self):
        upload = clone(self.base)
        del upload.tags[(1, "Study")]
        upload.tagmaps = {k: v for k, v in upload.tagmaps.items() if k[0] != (1, "Study")}
        master = clone(self.base)
        master.notes["n3"] = note(self.g1, "tagged elsewhere")
        master.tagmaps[((1, "Study"), ("n", "n3"))] = 2
        out, _ = merge(master, upload, self.base)
        self.assertFalse(any(k[0] == (1, "Study") for k in out.tagmaps))
        roundtrip(out)  # still writes a valid database

    def test_overlapping_new_highlight_replaces_other_devices_mark(self):
        master, upload = clone(self.base), clone(self.base)
        master.usermarks["other"] = mark(self.g2, 5, 0, 10)
        upload.usermarks["mine"] = mark(self.g2, 5, 5, 15)
        out, rep = merge(master, upload, self.base)
        self.assertIn("mine", out.usermarks)
        self.assertNotIn("other", out.usermarks)
        self.assertTrue(rep.notes)

    def test_overlapping_mark_with_note_is_kept(self):
        master, upload = clone(self.base), clone(self.base)
        master.usermarks["other"] = mark(self.g2, 5, 0, 10)
        master.notes["on"] = note(self.g2, "keep me", usermark="other")
        upload.usermarks["mine"] = mark(self.g2, 5, 5, 15)
        out, _ = merge(master, upload, self.base)
        self.assertTrue({"mine", "other"} <= set(out.usermarks))

    def test_merged_result_writes_and_reads_back_identically(self):
        master, upload = clone(self.base), clone(self.base)
        master.notes["nm"] = note(self.g1, "master")
        del upload.usermarks["m2"]
        upload.inputfields[(self.g2, "tt9")] = "new"
        out, _ = merge(master, upload, self.base)
        again = roundtrip(out)
        for t in ("usermarks", "notes", "tags", "tagmaps", "bookmarks", "inputfields"):
            self.assertEqual(getattr(out, t), getattr(again, t), t)


STUDY, BIBLE = (1, "Study"), (1, "Bible study")


def rename(ds, old, new):
    del ds.tags[old]
    ds.tags[new] = {"name": new[1]}
    ds.tagmaps = {((new if t == old else t), target): pos for (t, target), pos in ds.tagmaps.items()}
    if old in ds.playlists:
        ds.playlists[new] = ds.playlists.pop(old)


def tagged(ds, tkey):
    return {target for t, target in ds.tagmaps if t == tkey}


class TagRenames(unittest.TestCase):
    def setUp(self):
        self.base = roundtrip(base_dataset())
        self.g1 = loc()[0]

    def test_rename_on_device_is_applied(self):
        upload = clone(self.base)
        rename(upload, STUDY, BIBLE)
        out, rep = merge(clone(self.base), upload, self.base)
        self.assertNotIn(STUDY, out.tags)
        self.assertEqual(tagged(out, BIBLE), {("n", "n1"), ("n", "n2")})
        self.assertEqual(rep.tables["tags"].changed, 1)
        self.assertIn("renamed", rep.summary())
        roundtrip(out)

    def test_other_devices_links_follow_the_rename(self):
        master, upload = clone(self.base), clone(self.base)
        master.notes["n3"] = note(self.g1, "tagged elsewhere")
        master.tagmaps[(STUDY, ("n", "n3"))] = 2
        rename(upload, STUDY, BIBLE)
        out, _ = merge(master, upload, self.base)
        self.assertNotIn(STUDY, out.tags)
        self.assertEqual(tagged(out, BIBLE), {("n", "n1"), ("n", "n2"), ("n", "n3")})
        roundtrip(out)

    def test_device_unaware_of_rename_tags_under_new_name(self):
        # Another device renamed the tag earlier; this device still uses the old name.
        master, upload = clone(self.base), clone(self.base)
        rename(master, STUDY, BIBLE)
        upload.tagmaps[(STUDY, ("l", self.g1))] = 2
        out, rep = merge(master, upload, self.base)
        self.assertNotIn(STUDY, out.tags)
        self.assertEqual(tagged(out, BIBLE), {("n", "n1"), ("n", "n2"), ("l", self.g1)})
        self.assertEqual(rep.tables["tags"].changed, 0)

    def test_rename_with_added_item_is_still_a_rename(self):
        master, upload = clone(self.base), clone(self.base)
        master.tagmaps[(STUDY, ("l", self.g1))] = 2
        rename(upload, STUDY, BIBLE)
        upload.tagmaps[(BIBLE, ("l", loc(chapter=2)[0]))] = 2
        out, _ = merge(master, upload, self.base)
        self.assertNotIn(STUDY, out.tags)
        self.assertEqual(tagged(out, BIBLE),
                         {("n", "n1"), ("n", "n2"), ("l", self.g1), ("l", loc(chapter=2)[0])})

    def test_conflicting_renames_fold_into_winner(self):
        master, upload = clone(self.base), clone(self.base)
        rename(master, STUDY, (1, "Research"))
        rename(upload, STUDY, BIBLE)
        master.tagmaps[((1, "Research"), ("l", self.g1))] = 2
        out, rep = merge(master, upload, self.base)
        self.assertEqual({k for k in out.tags if k[0] == 1}, {BIBLE})
        self.assertIn(("l", self.g1), tagged(out, BIBLE))
        self.assertEqual(rep.tables["tags"].conflicts, 1)
        out, _ = merge(master, upload, self.base, prefer="master")
        self.assertEqual({k for k in out.tags if k[0] == 1}, {(1, "Research")})
        self.assertEqual(len(tagged(out, (1, "Research"))), 3)

    def test_delete_and_unrelated_new_tag_is_not_a_rename(self):
        master, upload = clone(self.base), clone(self.base)
        master.tagmaps[(STUDY, ("l", self.g1))] = 2
        del upload.tags[STUDY]
        upload.tagmaps = {k: v for k, v in upload.tagmaps.items() if k[0] != STUDY}
        upload.tags[(1, "Other")] = {"name": "Other"}
        upload.tagmaps[((1, "Other"), ("l", loc(chapter=2)[0]))] = 0
        out, _ = merge(master, upload, self.base)
        self.assertNotIn(STUDY, out.tags)
        self.assertEqual(tagged(out, (1, "Other")), {("l", loc(chapter=2)[0])})

    def test_empty_tag_rename(self):
        base = clone(self.base)
        base.tags[(1, "Empty")] = {"name": "Empty"}
        upload = clone(base)
        rename(upload, (1, "Empty"), (1, "Still empty"))
        self.assertEqual(find_renames(base, upload), {(1, "Empty"): (1, "Still empty")})

    def test_playlist_rename_keeps_other_devices_edits(self):
        old, new = (2, "Morning"), (2, "Mornings")

        def item(label):
            return {"label": label, "locations": (), "media": (), "markers": ()}

        base = clone(self.base)
        base.tags[old] = {"name": old[1]}
        base.playlists[old] = (item("a"), item("b"))
        master, upload = clone(base), clone(base)
        master.playlists[old] = (item("a"), item("b"), item("c"))
        rename(upload, old, new)
        out, _ = merge(master, upload, base)
        self.assertNotIn(old, out.playlists)
        self.assertEqual([i["label"] for i in out.playlists[new]], ["a", "b", "c"])


class Prefer(unittest.TestCase):
    def setUp(self):
        self.base = roundtrip(base_dataset())
        self.master, self.upload = clone(self.base), clone(self.base)
        self.master.usermarks["m3"] = {**self.master.usermarks["m3"], "color": 4}
        self.upload.usermarks["m3"] = {**self.upload.usermarks["m3"], "color": 6}

    def color(self, **kw):
        out, _ = merge(self.master, self.upload, self.base, **kw)
        return out.usermarks["m3"]["color"]

    def test_upload_wins_by_default(self):
        self.assertEqual(self.color(), 6)

    def test_prefer_master(self):
        self.assertEqual(self.color(prefer="master"), 4)

    def test_prefer_newer_uses_backup_dates(self):
        self.master.last_modified = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self.upload.last_modified = datetime(2026, 4, 1, tzinfo=timezone.utc)
        self.assertEqual(self.color(prefer="newer"), 4)
        self.upload.last_modified = datetime(2026, 6, 1, tzinfo=timezone.utc)
        self.assertEqual(self.color(prefer="newer"), 6)

    def test_prefer_newer_without_dates_falls_back_to_upload(self):
        self.assertEqual(self.color(prefer="newer"), 6)

    def test_notes_still_go_to_newer_edit_when_master_preferred(self):
        self.master.notes["n2"] = {**self.master.notes["n2"], "content": "old", "modified": "2026-02-01T00:00:00Z"}
        self.upload.notes["n2"] = {**self.upload.notes["n2"], "content": "new", "modified": "2026-02-02T00:00:00Z"}
        out, _ = merge(self.master, self.upload, self.base, prefer="master")
        self.assertEqual(out.notes["n2"]["content"], "new")

    def test_bad_prefer_value(self):
        with self.assertRaises(ValueError):
            self.color(prefer="sideways")


class Combine(unittest.TestCase):
    def test_newest_backup_wins_regardless_of_order(self):
        old, new = roundtrip(base_dataset()), roundtrip(base_dataset())
        old.usermarks["m3"] = {**old.usermarks["m3"], "color": 4}
        new.usermarks["m3"] = {**new.usermarks["m3"], "color": 6}
        old.last_modified = datetime(2026, 1, 1, tzinfo=timezone.utc)
        new.last_modified = datetime(2026, 2, 1, tzinfo=timezone.utc)
        for order in ((old, new), (new, old)):
            out, _ = combine(*order)
            self.assertEqual(out.usermarks["m3"]["color"], 6)
        out, _ = combine(new, old, sort=False)
        self.assertEqual(out.usermarks["m3"]["color"], 4)
        self.assertEqual(out.last_modified, new.last_modified)

    def test_combine_never_deletes(self):
        a = roundtrip(base_dataset())
        b = clone(a)
        del b.usermarks["m2"]
        b.notes["nb"] = note(loc()[0], "b")
        out, rep = combine(a, b)
        self.assertIn("m2", out.usermarks)
        self.assertIn("nb", out.notes)
        self.assertEqual(rep.tables["usermarks"].removed, 0)


if __name__ == "__main__":
    unittest.main()
