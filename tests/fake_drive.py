"""An in-memory stand-in for the Drive API, implementing jwlsync.drive.DriveClient."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from pathlib import Path

from jwlsync.drive import APP_PROPERTIES, FOLDER_MIME

EDITABLE = {"canAddChildren": True, "canEdit": True}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.content: dict[str, bytes] = {}
        self.resource_keys: dict[str, str] = {}
        self.required_keys: dict[str, str] = {}  # file id -> resource key its link needs
        self._ids = itertools.count(1)

    def add(self, name: str, parent: str | None = None, data: bytes | None = None, folder: bool = False,
            capabilities: dict | None = None, app_properties: dict | None = None) -> dict:
        fid = f"id{next(self._ids):08d}"
        self.files[fid] = {"id": fid, "name": name, "mimeType": FOLDER_MIME if folder else "application/octet-stream",
                           "parents": [parent] if parent else [], "modifiedTime": _now(),
                           "size": None if folder else str(len(data or b"")),
                           "appProperties": dict(app_properties or {}), "capabilities": dict(capabilities or EDITABLE)}
        if not folder:
            self.content[fid] = data or b""
        return dict(self.files[fid])

    def get(self, file_id: str) -> dict:
        if file_id not in self.files:
            raise LookupError(f"404 {file_id}")
        needed = self.required_keys.get(file_id)
        if needed and self.resource_keys.get(file_id) != needed:
            raise LookupError(f"404 {file_id} (resource key)")
        return dict(self.files[file_id])

    def children(self, folder_id: str) -> list[dict]:
        return [dict(f) for f in self.files.values() if folder_id in f["parents"]]

    def create_folder(self, parent_id: str, name: str) -> dict:
        return self.add(name, parent_id, folder=True, app_properties=APP_PROPERTIES)

    def upload(self, parent_id: str, src: Path, name: str) -> dict:
        return self.add(name, parent_id, Path(src).read_bytes(), app_properties=APP_PROPERTIES)

    def update_content(self, file_id: str, src: Path) -> dict:
        self.content[file_id] = Path(src).read_bytes()
        self.files[file_id].update(modifiedTime=_now(), size=str(len(self.content[file_id])))
        return dict(self.files[file_id])

    def download(self, file_id: str, dest: Path) -> None:
        Path(dest).write_bytes(self.content[file_id])

    def move(self, file_id: str, add_parent: str, remove_parent: str, name: str | None = None) -> dict:
        f = self.files[file_id]
        removed = set(remove_parent.split(","))
        f["parents"] = [p for p in f["parents"] if p not in removed] + [add_parent]
        if name:
            f["name"] = name
        return dict(f)

    # Test helpers
    def path_of(self, file_id: str) -> list[str]:
        names, f = [], self.files[file_id]
        while f["parents"]:
            names.append(f["name"])
            f = self.files[f["parents"][0]]
        return list(reversed(names))
