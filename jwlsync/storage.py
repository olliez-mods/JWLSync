"""Where a user's files live.

The service only needs a few file operations on the three folders of a user's sync folder, so it
talks to a `Storage`. `LocalStorage` keeps them in a directory on disk (for development and tests);
`drive.DriveStorage` maps the same calls onto the user's shared Drive folder.

File ids are opaque strings owned by the storage. Moving a file may give it a new id.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SAVE = "JW Library Save"
HISTORY = "History"
USER = "User"
FOLDERS = (SAVE, HISTORY, USER)
# Inside History: previous merged files (kept as bases) and raw uploads (kept for recovery).
MERGED = f"{HISTORY}/Merged"
UPLOADS = f"{HISTORY}/Uploads"
SUBFOLDERS = (MERGED, UPLOADS)


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    modified: float  # seconds since the epoch
    size: int
    ours: bool = False  # written by JWLSync (Drive appProperties); LocalStorage can't tell


class Storage(Protocol):
    def setup(self) -> None:
        """Create the three folders and History's subfolders. Anything else already in the root is
        moved into History."""

    def list(self, folder: str) -> list[Entry]:
        """Files (not folders) in folder: one of FOLDERS or SUBFOLDERS."""

    def download(self, file_id: str, dest: Path) -> None: ...

    def upload(self, folder: str, src: Path, name: str) -> Entry: ...

    def move(self, file_id: str, folder: str, name: str | None = None) -> Entry:
        """Move a file into folder, optionally renaming it. Whether a taken name gets a suffix is up
        to the storage (Drive allows duplicate names)."""

    def read_text(self, name: str) -> str | None:
        """A text file in the User folder, or None if it doesn't exist."""

    def write_text(self, name: str, text: str) -> None: ...


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _entry(self, path: Path) -> Entry:
        st = path.stat()
        return Entry(path.relative_to(self.root).as_posix(), path.name, st.st_mtime, st.st_size)

    def _free_name(self, folder: Path, name: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)  # subfolders of a folder set up before they existed
        target = folder / name
        stem, suffix, n = target.stem, target.suffix, 2
        while target.exists():
            target = folder / f"{stem} ({n}){suffix}"
            n += 1
        return target

    def setup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        strays = [p for p in self.root.iterdir() if p.name not in FOLDERS]
        for name in FOLDERS + SUBFOLDERS:
            (self.root / name).mkdir(exist_ok=True)
        for p in strays:
            shutil.move(p, self._free_name(self.root / HISTORY, p.name))

    def list(self, folder: str) -> list[Entry]:
        path = self.root / folder
        if not path.exists():
            return []
        return sorted((self._entry(p) for p in path.iterdir() if p.is_file()),
                      key=lambda e: (e.modified, e.name))

    def download(self, file_id: str, dest: Path) -> None:
        shutil.copyfile(self.root / file_id, dest)

    def upload(self, folder: str, src: Path, name: str) -> Entry:
        target = self._free_name(self.root / folder, name)
        tmp = target.with_name(target.name + ".part")
        shutil.copyfile(src, tmp)
        tmp.replace(target)
        return self._entry(target)

    def move(self, file_id: str, folder: str, name: str | None = None) -> Entry:
        src = self.root / file_id
        target = self._free_name(self.root / folder, name or src.name)
        src.replace(target)
        return self._entry(target)

    def read_text(self, name: str) -> str | None:
        path = self.root / USER / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write_text(self, name: str, text: str) -> None:
        path = self.root / USER / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
