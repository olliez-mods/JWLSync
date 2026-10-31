"""GoogleDrive (the real API adapter) against googleapiclient's HTTP mock: checks the requests it
sends without a network or an account. Skipped when the Google libraries aren't installed."""

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from googleapiclient.http import HttpMockSequence
except ImportError:
    HttpMockSequence = None

from jwlsync.drive import FOLDER_MIME, GoogleDrive

OK = {"status": "200"}
META = {"id": "f1", "name": "x", "mimeType": "application/octet-stream", "parents": ["p"],
        "modifiedTime": "2026-10-31T12:00:00.000Z", "size": "3"}


@unittest.skipUnless(HttpMockSequence, "google-api-python-client not installed")
class GoogleDriveRequests(unittest.TestCase):
    def client(self, *responses, keys=None):
        self.http = HttpMockSequence([(OK, json.dumps(r) if isinstance(r, dict) else r) for r in responses])
        return GoogleDrive(http=self.http, resource_keys=keys)

    def sent(self, i=0):
        uri, method, body, headers = self.http.request_sequence[i]
        url = urlparse(uri)
        return method, url.path, {k: v[0] for k, v in parse_qs(url.query).items()}, body, headers

    def test_get_sends_resource_keys(self):
        drive = self.client(META, keys={"root": "0-k"})
        self.assertEqual(drive.get("f1")["id"], "f1")
        method, path, query, _, headers = self.sent()
        self.assertEqual((method, path), ("GET", "/drive/v3/files/f1"))
        self.assertEqual(query["supportsAllDrives"], "true")
        self.assertIn("capabilities", query["fields"])
        self.assertEqual(headers["X-Goog-Drive-Resource-Keys"], "root/0-k")

    def test_children_follows_pages(self):
        drive = self.client({"files": [META], "nextPageToken": "t2"}, {"files": [{**META, "id": "f2"}]})
        self.assertEqual([f["id"] for f in drive.children("p")], ["f1", "f2"])
        _, path, query, _, _ = self.sent(0)
        self.assertEqual(path, "/drive/v3/files")
        self.assertEqual(query["q"], "'p' in parents and trashed = false")
        self.assertEqual(self.sent(1)[2]["pageToken"], "t2")

    def test_create_folder_and_move(self):
        drive = self.client({**META, "mimeType": FOLDER_MIME}, META)
        drive.create_folder("root", "History")
        method, _, _, body, _ = self.sent(0)
        self.assertEqual(method, "POST")
        sent = json.loads(body)
        self.assertEqual((sent["name"], sent["mimeType"], sent["parents"]), ("History", FOLDER_MIME, ["root"]))
        self.assertEqual(sent["appProperties"], {"jwlsync": "1"})
        drive.move("f1", "hist", "save", "new name")
        method, path, query, body, _ = self.sent(1)
        self.assertEqual((method, path), ("PATCH", "/drive/v3/files/f1"))
        self.assertEqual((query["addParents"], query["removeParents"]), ("hist", "save"))
        self.assertEqual(json.loads(body), {"name": "new name"})

    def test_upload_and_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.jwlibrary"
            src.write_bytes(b"abc")
            drive = self.client(META)  # small files go up in one resumable chunk: session, then data
            self.http._iterable[:0] = [({"status": "200", "location": "https://upload.example/s"}, "")]
            self.assertEqual(drive.upload("save", src, "a.jwlibrary")["id"], "f1")
            uri, method, body, _ = self.http.request_sequence[0]
            self.assertIn("/upload/drive/v3/files", uri)
            self.assertEqual(json.loads(body)["parents"], ["save"])

            dest = Path(tmp) / "out"
            drive = self.client({}, keys={"f1": "0-k"})
            self.http._iterable[:] = [({"status": "200", "content-range": "bytes 0-2/3"}, "abc")]
            drive.download("f1", dest)
            self.assertEqual(dest.read_bytes(), b"abc")
            uri, _, _, headers = self.http.request_sequence[0]
            self.assertIn("alt=media", uri)
            self.assertEqual(headers["X-Goog-Drive-Resource-Keys"], "f1/0-k")


if __name__ == "__main__":
    unittest.main()
