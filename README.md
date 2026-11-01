# JWLSync

Cloud sync for JW Library, using a Google Drive folder.

JW Library can only back up and restore. JWLSync watches a Drive folder, merges the backups from
all your devices (notes, highlights, tags, bookmarks, playlists) and leaves one up to date backup
that any device can restore.

## How it works

1. **Back up** from any device into your folder's `JW Library Save` folder.
2. **Wait** a few seconds while JWLSync merges it.
3. **Restore** the merged backup on any device.

Merges are 3-way: JWLSync knows which merged file each device last restored, so it can tell what
you actually deleted on a device instead of bringing it back. The merge engine is
[`jwlkit`](jwlkit/), a standalone package you can use on its own.

Your Drive folder ends up looking like this:

```
JW Library Save/    back up here, restore from here (holds only the current merged backup)
History/Merged/     previous merged backups
History/Uploads/    every backup you've uploaded
User/               JWLSync.txt (your page link + how-to) and merge-log.txt
```

## Using it

Get an invite code (or link) from whoever runs the server, open the site, enter the code, and
paste a Drive folder link shared as **Anyone with the link → Editor**. That's it. Your page shows
the sync status and recent merges, and lets you pause or stop syncing.

## Running the server

**Needs:** Python 3.10+, and a Google account for the bot (files it creates live in its Drive).

1. **Google Cloud:** create a project, enable the Google Drive API, set the OAuth consent screen
   to *In production*, and create a *Desktop app* OAuth client. Save its JSON as
   `secrets/client_secret.json`.
2. **Install:**
   ```
   python -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. **Run:**
   ```
   .venv/bin/python main.py
   ```
   The first run creates `config.ini` from `config.ini.template` and opens a browser to log in as
   the bot (once). Set `admin_code` and `site_url` in `config.ini`, then restart.
4. **Invite people** from `/admin` on the site, or with `python admin.py`.

The site listens on `127.0.0.1:5080`. Put a reverse proxy with HTTPS in front of it.

## Files

| | |
|---|---|
| `main.py` | starts the website and watches every active folder |
| `admin.py` | manage invites and folders from the terminal |
| `web.py`, `templates/`, `static/` | the website |
| `service.py` | turns uploads into merged backups |
| `drive.py`, `storage.py` | Google Drive and folder layout |
| `db.py` | invites and folders (SQLite) |
| `jwlkit/` | the merge engine |

## Tests

```
PYTHONPATH=jwlkit/src:jwlkit/tests:. python -m unittest discover -s tests
```
(On Windows, separate the paths with `;` instead of `:`.)
