"""Finding the base for a 3-way merge: the merged file a device last restored.

The best evidence is the stamp: a merged file written with `Dataset.stamp = {"merge_id": ...}`
keeps that table through a restore, so the device's next backup still names it. A backup without
a known stamp falls back to content: the candidate whose entries (note and highlight GUIDs,
bookmarks, tag links, input fields) the upload still holds almost entirely. When neither works the
upload has no base, and merging it deletes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping

from .model import Dataset

# Share of a candidate's entries the upload must still hold for a content match. Kept high: a base
# that wrongly includes another device's entries makes them look deleted on this one.
CONTENT_THRESHOLD = 0.98

# Clock skew allowed between the device and the machine that wrote the candidate.
CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class BaseMatch:
    merge_id: str
    method: str     # "stamp" or "content"
    score: float    # share of the base's entries found in the upload; 1.0 for a stamp match


def stamp_id(ds: Dataset) -> str | None:
    return ds.stamp.get("merge_id") or None


def _entries(ds: Dataset) -> set:
    return ({("n", k) for k in ds.notes} | {("u", k) for k in ds.usermarks} | {("b", k) for k in ds.bookmarks}
            | {("t", k) for k in ds.tagmaps} | {("i", k) for k in ds.inputfields})


def coverage(candidate: Dataset, upload: Dataset) -> tuple[float, int]:
    """Share and count of candidate's entries still present in upload."""
    cand = _entries(candidate)
    if not cand:
        return 0.0, 0
    shared = len(cand & _entries(upload))
    return shared / len(cand), shared


def find_base(upload: Dataset, candidates: Mapping[str, Dataset],
              threshold: float = CONTENT_THRESHOLD) -> BaseMatch | None:
    """Pick the base for upload among candidates ({merge_id: dataset}).

    A stamp naming a candidate wins outright. Otherwise, of the candidates the upload covers to at
    least `threshold`, the one sharing the most entries: a device that restored a later merged file
    also covers the earlier ones, and the later file is the right base. A candidate written after
    the upload last changed can't have been restored by that device, so it's skipped.
    """
    sid = stamp_id(upload)
    if sid in candidates:
        return BaseMatch(sid, "stamp", 1.0)
    best = None
    for merge_id, cand in candidates.items():
        if upload.last_modified and cand.last_modified and upload.last_modified < cand.last_modified - CLOCK_SKEW:
            continue
        share, shared = coverage(cand, upload)
        if share >= threshold and (best is None or shared > best[1]):
            best = (merge_id, shared, share)
    return BaseMatch(best[0], "content", best[2]) if best else None
