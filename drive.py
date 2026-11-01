"""Storage on a user's Google Drive folder, accessed as the JWLSync bot account.

The user shares a folder by link ("anyone with the link can edit"); the bot opens it by id. Files
the bot creates are owned by the bot and tagged with appProperties {"jwlsync": "1"}.

`DriveStorage` talks to Drive through a small client interface (`DriveClient`). `GoogleDrive`
implements it with google-api-python-client; tests use an in-memory fake. Only `GoogleDrive`,
`authorize` and `load_credentials` need the Google libraries, and they import them lazily.
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from storage import FOLDERS, HISTORY, SUBFOLDERS, USER, Entry, LocalStorage

FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPES = ["https://www.googleapis.com/auth/drive"]
APP_PROPERTIES = {"jwlsync": "1"}
FIELDS = "id,name,mimeType,parents,modifiedTime,size,appProperties,capabilities(canAddChildren,canEdit)"

_ID = re.compile(r"^[-\w]{10,}$")


class DriveError(Exception):
    pass


def parse_folder_link(link: str) -> tuple[str, str | None]:
    """Folder id and resource key (older links) from a Drive folder link or a bare id."""
    link = link.strip()
    if _ID.match(link):
        return link, None
    url = urlparse(link)
    query = parse_qs(url.query)
    m = re.search(r"/folders/([-\w]+)", url.path)
    folder_id = m.group(1) if m else (query.get("id") or [None])[0]
    if not folder_id or not _ID.match(folder_id) or not url.netloc.endswith("google.com"):
        raise DriveError(f"not a Google Drive folder link: {link}")
    return folder_id, (query.get("resourcekey") or [None])[0]


class DriveClient(Protocol):
    """The Drive operations DriveStorage needs. File metadata dicts carry the FIELDS keys."""

    resource_keys: dict[str, str]

    def get(self, file_id: str) -> dict: ...

    def children(self, folder_id: str) -> list[dict]:
        """Non-trashed children of a folder."""

    def create_folder(self, parent_id: str, name: str) -> dict: ...

    def upload(self, parent_id: str, src: Path, name: str) -> dict: ...

    def update_content(self, file_id: str, src: Path) -> dict: ...

    def download(self, file_id: str, dest: Path) -> None: ...

    def move(self, file_id: str, add_parent: str, remove_parent: str, name: str | None = None) -> dict: ...


def _entry(meta: dict) -> Entry:
    modified = datetime.fromisoformat(meta["modifiedTime"].replace("Z", "+00:00")).timestamp()
    ours = (meta.get("appProperties") or {}).get("jwlsync") == APP_PROPERTIES["jwlsync"]
    return Entry(meta["id"], meta["name"], modified, int(meta.get("size") or 0), ours)


class DriveStorage:
    """The Storage interface on a Drive folder. Use open_folder() to build one from a link."""

    def __init__(self, client: DriveClient, root_id: str):
        self.client, self.root_id = client, root_id
        self._folders: dict[str, str] = {}
        self._user_files: dict[str, str] = {}

    def _folder(self, name: str) -> str:
        if "/" in name and name not in self._folders:
            # A subfolder: found under its parent, or created (folders set up before it existed).
            parent_name, child = name.rsplit("/", 1)
            parent = self._folder(parent_name)
            found = [f["id"] for f in self.client.children(parent)
                     if f["mimeType"] == FOLDER_MIME and f["name"] == child]
            self._folders[name] = found[0] if found else self.client.create_folder(parent, child)["id"]
        if name not in self._folders:
            for f in self.client.children(self.root_id):
                if f["mimeType"] == FOLDER_MIME and f["name"] in FOLDERS:
                    self._folders.setdefault(f["name"], f["id"])
            if name not in self._folders:
                raise DriveError(f"folder {name!r} missing; run setup first")
        return self._folders[name]

    def setup(self) -> None:
        existing = self.client.children(self.root_id)
        for f in existing:
            if f["mimeType"] == FOLDER_MIME and f["name"] in FOLDERS:
                self._folders.setdefault(f["name"], f["id"])
        for name in FOLDERS:
            if name not in self._folders:
                self._folders[name] = self.client.create_folder(self.root_id, name)["id"]
        for f in existing:
            if f["id"] not in self._folders.values():
                self.client.move(f["id"], self._folders[HISTORY], self.root_id)
        for name in SUBFOLDERS:
            self._folder(name)

    def list(self, folder: str) -> list[Entry]:
        files = [f for f in self.client.children(self._folder(folder)) if f["mimeType"] != FOLDER_MIME]
        return sorted((_entry(f) for f in files), key=lambda e: (e.modified, e.name))

    def download(self, file_id: str, dest: Path) -> None:
        self.client.download(file_id, dest)

    def upload(self, folder: str, src: Path, name: str) -> Entry:
        return _entry(self.client.upload(self._folder(folder), src, name))

    def move(self, file_id: str, folder: str, name: str | None = None) -> Entry:
        parents = self.client.get(file_id).get("parents") or [self.root_id]
        return _entry(self.client.move(file_id, self._folder(folder), ",".join(parents), name))

    def _user_file(self, name: str) -> str | None:
        if name not in self._user_files:
            for f in self.client.children(self._folder(USER)):
                if f["mimeType"] != FOLDER_MIME:
                    self._user_files.setdefault(f["name"], f["id"])
        return self._user_files.get(name)

    def read_text(self, name: str) -> str | None:
        file_id = self._user_file(name)
        if not file_id:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "f"
            self.client.download(file_id, dest)
            return dest.read_text(encoding="utf-8")

    def write_text(self, name: str, text: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / name
            src.write_text(text, encoding="utf-8")
            file_id = self._user_file(name)
            if file_id:
                self.client.update_content(file_id, src)
            else:
                self._user_files[name] = self.client.upload(self._folder(USER), src, name)["id"]


def open_folder(client: DriveClient, link: str) -> DriveStorage:
    """Check that link is a folder the bot can edit and return storage on it."""
    folder_id, resource_key = parse_folder_link(link)
    if resource_key:
        client.resource_keys[folder_id] = resource_key
    try:
        meta = client.get(folder_id)
    except Exception as e:
        raise DriveError("can't open the folder; is it shared as 'Anyone with the link' + Editor?") from e
    if meta.get("mimeType") != FOLDER_MIME:
        raise DriveError("that link is a file, not a folder")
    caps = meta.get("capabilities") or {}
    if not (caps.get("canAddChildren") and caps.get("canEdit")):
        raise DriveError("the folder is shared read-only; set link sharing to Editor")
    return DriveStorage(client, folder_id)


class GoogleDrive:
    """DriveClient on the real Drive v3 API."""

    def __init__(self, credentials=None, resource_keys: dict[str, str] | None = None, http=None):
        """Pass credentials, or (tests) an httplib2-compatible http object instead."""
        from googleapiclient.discovery import build

        self.svc = build("drive", "v3", credentials=credentials, http=http, cache_discovery=False,
                         static_discovery=True)
        self.resource_keys = dict(resource_keys or {})

    def _keyed(self, request):
        # Older link-shared items need their resource key on every request that touches them.
        if self.resource_keys:
            request.headers["X-Goog-Drive-Resource-Keys"] = ",".join(
                f"{fid}/{key}" for fid, key in self.resource_keys.items())
        return request

    def _run(self, request):
        return self._keyed(request).execute(num_retries=3)

    def get(self, file_id: str) -> dict:
        return self._run(self.svc.files().get(fileId=file_id, fields=FIELDS, supportsAllDrives=True))

    def children(self, folder_id: str) -> list[dict]:
        out, token = [], None
        while True:
            page = self._run(self.svc.files().list(
                q=f"'{folder_id}' in parents and trashed = false", fields=f"nextPageToken,files({FIELDS})",
                pageSize=1000, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True))
            out += page.get("files", [])
            token = page.get("nextPageToken")
            if not token:
                return out

    def create_folder(self, parent_id: str, name: str) -> dict:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id], "appProperties": APP_PROPERTIES}
        return self._run(self.svc.files().create(body=body, fields=FIELDS, supportsAllDrives=True))

    def upload(self, parent_id: str, src: Path, name: str) -> dict:
        from googleapiclient.http import MediaFileUpload

        body = {"name": name, "parents": [parent_id], "appProperties": APP_PROPERTIES}
        media = MediaFileUpload(str(src), mimetype="application/octet-stream", resumable=True)
        return self._run(self.svc.files().create(body=body, media_body=media, fields=FIELDS,
                                                 supportsAllDrives=True))

    def update_content(self, file_id: str, src: Path) -> dict:
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(src), mimetype="application/octet-stream", resumable=True)
        return self._run(self.svc.files().update(fileId=file_id, media_body=media, fields=FIELDS,
                                                 supportsAllDrives=True))

    def download(self, file_id: str, dest: Path) -> None:
        from googleapiclient.http import MediaIoBaseDownload

        request = self._keyed(self.svc.files().get_media(fileId=file_id, supportsAllDrives=True))
        with open(dest, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=3)

    def move(self, file_id: str, add_parent: str, remove_parent: str, name: str | None = None) -> dict:
        return self._run(self.svc.files().update(
            fileId=file_id, addParents=add_parent, removeParents=remove_parent,
            body={"name": name} if name else {}, fields=FIELDS, supportsAllDrives=True))


def authorize(client_secrets: str | Path, token_path: str | Path) -> None:
    """One-time login as the bot account; saves a refresh token to token_path."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES).run_local_server(port=0)
    Path(token_path).write_text(creds.to_json(), encoding="utf-8")


def load_credentials(token_path: str | Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        Path(token_path).write_text(creds.to_json(), encoding="utf-8")
    return creds


def open_storage(folder: str, token_path: str | Path, client_secret_path: str | Path):
    """Storage for a Drive folder link, or a local folder (for testing). Logs in as the bot first
    if there's no token yet."""
    if not folder.startswith("https://"):
        return LocalStorage(folder)
    if not Path(token_path).exists():
        print("No bot token yet, log in as the bot account in the browser")
        authorize(client_secret_path, token_path)
    return open_folder(GoogleDrive(load_credentials(token_path)), folder)


def folder_key(folder: str) -> str:
    """What makes a folder unique: the Drive folder id, or the full path of a local folder."""
    return parse_folder_link(folder)[0] if folder.startswith("https://") else str(Path(folder).resolve())
