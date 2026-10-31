"""Read, write and merge JW Library .jwlibrary backups.

    from jwlkit import open_backup, read, merge, write_backup

    with open_backup("base.jwlibrary") as b, open_backup("master.jwlibrary") as m, \\
         open_backup("upload.jwlibrary") as u:
        merged, report = merge(read(m), read(u), base=read(b))
        write_backup(merged, "merged.jwlibrary")
        print(report.summary())

Keep the Backup objects open until write_backup has run: playlist media files are read from
their extracted copies.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .archive import Backup, BackupError, open_backup, pack
from .base import BaseMatch, find_base
from .merge import MergeReport, combine, merge
from .model import Dataset, read_dataset, write_dataset

__all__ = ["Backup", "BackupError", "BaseMatch", "Dataset", "MergeReport", "open_backup", "read", "merge",
           "combine", "find_base", "write_backup"]
__version__ = "0.1.0"


def read(backup: Backup) -> Dataset:
    conn = backup.connect()
    try:
        ds = read_dataset(conn, backup.media)
    finally:
        conn.close()
    ds.last_modified = backup.last_modified
    return ds


def write_backup(ds: Dataset, out_path: str | Path, device_name: str = "JWLSync") -> dict:
    """Write ds as a .jwlibrary archive and return its manifest."""
    tmp = Path(tempfile.mkdtemp(prefix="jwlkit-out-"))
    try:
        db = tmp / "userData.db"
        media, _skipped = write_dataset(ds, db)
        return pack(db, out_path, media, device_name=device_name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
