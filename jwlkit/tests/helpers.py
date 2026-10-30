import copy
import shutil
import tempfile
from pathlib import Path

from jwlkit import open_backup, read, write_backup
from jwlkit.model import Dataset


def loc(book=1, chapter=1, doc=0, key="nwtsty", lang=0, typ=0, title="Genesis 1"):
    row = {"BookNumber": book, "ChapterNumber": chapter, "DocumentId": doc or None, "Track": None,
           "IssueTagNumber": 0, "KeySymbol": key, "MepsLanguage": lang, "Type": typ, "Title": title,
           "Specialty": None, "Edition": None}
    lkey = (book, chapter, doc, 0, 0, key, lang, typ, "", "")
    return lkey, row


def mark(lkey, ident=1, start=0, end=5, color=1):
    return {"color": color, "style": 0, "version": 1, "loc": lkey, "ranges": ((2, ident, start, end),)}


def note(lkey, content, modified="2026-01-01T00:00:00Z", usermark=None, title="t"):
    return {"usermark": usermark, "loc": lkey, "title": title, "content": content, "modified": modified,
            "created": "2026-01-01T00:00:00Z", "block_type": 2, "block_id": 1}


def base_dataset() -> Dataset:
    g1, g1row = loc()
    g2, g2row = loc(chapter=2, title="Genesis 2")
    ds = Dataset()
    ds.locations = {g1: g1row, g2: g2row}
    ds.tags = {(0, ""): {"name": "Favorite"}, (1, "Study"): {"name": "Study"}}
    ds.usermarks = {"m1": mark(g1, 1, 0, 5), "m2": mark(g1, 2, 0, 5, color=2), "m3": mark(g2, 1, 0, 9)}
    ds.notes = {"n1": note(g1, "first", usermark="m1"), "n2": note(g2, "second")}
    ds.tagmaps = {((1, "Study"), ("n", "n1")): 0, ((1, "Study"), ("n", "n2")): 1, ((0, ""), ("l", g2)): 0}
    ds.bookmarks = {(g1, 0): {"loc": g2, "title": "bm", "snippet": None, "block_type": 0, "block_id": None}}
    ds.inputfields = {(g1, "tt1"): "answer"}
    return ds


def clone(ds: Dataset) -> Dataset:
    return copy.deepcopy(ds)


def roundtrip(ds: Dataset) -> Dataset:
    """Write ds to a real archive and read it back, so tests exercise the full file path."""
    tmp = Path(tempfile.mkdtemp())
    try:
        out = tmp / "x.jwlibrary"
        write_backup(ds, out)
        with open_backup(out) as b:
            return read(b)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
