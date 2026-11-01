"""App database: invite codes (one per person) and the folder each one tracks.

Each folder's merge state lives in its own Drive folder (User/state.json, merge log); this only
holds who's who and what to watch. An invite code is also that person's access key.
"""

import secrets
import sqlite3
import string
from contextlib import contextmanager
from datetime import datetime, timezone

DATABASE = "jwlsync.db"
STATUSES = ("active", "paused", "stopped")


def set_db(name:str) -> None:
    global DATABASE
    DATABASE = name


@contextmanager
def get_db():
    """A connection that commits (or rolls back on error) and closes when the block ends."""
    conn = sqlite3.connect(DATABASE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def setup_db() -> None:
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")  # the watch loop and the website both use it
        conn.execute('''
            CREATE TABLE IF NOT EXISTS invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invite_id INTEGER NOT NULL UNIQUE REFERENCES invites(id),
                link TEXT NOT NULL,
                folder_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                last_check TEXT,
                last_merge TEXT,
                last_error TEXT
            )
        ''')


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_code() -> str:
    """XXXXX-XXXXX, capital letters only."""
    return "-".join("".join(secrets.choice(string.ascii_uppercase) for _ in range(5)) for _ in range(2))


def clean_code(code:str) -> str:
    return code.strip().upper()


# Invites ==============================================================
def create_invite(name:str) -> str:
    """Make an invite for name and return its code."""
    with get_db() as conn:
        while True:
            code = new_code()
            try:
                conn.execute("INSERT INTO invites (code, name, created_at) VALUES (?, ?, ?)", (code, name, now()))
                return code
            except sqlite3.IntegrityError:
                continue  # code already taken, try another


def get_invite(code:str) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute("SELECT * FROM invites WHERE code = ?", (clean_code(code),)).fetchone()


def list_invites() -> list[sqlite3.Row]:
    """Every invite with its folder (folder columns are None if it hasn't been used)."""
    with get_db() as conn:
        return conn.execute('''
            SELECT invites.*, folders.id AS folder_pk, folders.link, folders.status, folders.last_check,
                   folders.last_merge, folders.last_error
            FROM invites LEFT JOIN folders ON folders.invite_id = invites.id
            ORDER BY invites.id
        ''').fetchall()


def regenerate_code(invite_id:int) -> str:
    """Give an invite a new code (e.g. the old one leaked); its folder stays attached."""
    with get_db() as conn:
        while True:
            code = new_code()
            try:
                conn.execute("UPDATE invites SET code = ? WHERE id = ?", (code, invite_id))
                return code
            except sqlite3.IntegrityError:
                continue


def set_revoked(invite_id:int, revoked:bool) -> None:
    """Revoking an invite also stops its folder."""
    with get_db() as conn:
        conn.execute("UPDATE invites SET revoked = ? WHERE id = ?", (int(revoked), invite_id))
        if revoked:
            conn.execute("UPDATE folders SET status = 'stopped' WHERE invite_id = ?", (invite_id,))


# Folders ==============================================================
def add_folder(invite_id:int, link:str, folder_id:str) -> int:
    """Start tracking a folder for an invite. Raises ValueError if the invite already has a folder
    or the folder is already tracked."""
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM folders WHERE invite_id = ?", (invite_id,)).fetchone():
            raise ValueError("this invite already has a folder")
        if conn.execute("SELECT 1 FROM folders WHERE folder_id = ?", (folder_id,)).fetchone():
            raise ValueError("this folder is already being tracked")
        cur = conn.execute("INSERT INTO folders (invite_id, link, folder_id, created_at) VALUES (?, ?, ?, ?)",
                           (invite_id, link, folder_id, now()))
        return cur.lastrowid


def get_folder(invite_id:int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute("SELECT * FROM folders WHERE invite_id = ?", (invite_id,)).fetchone()


def set_status(folder_pk:int, status:str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    with get_db() as conn:
        conn.execute("UPDATE folders SET status = ? WHERE id = ?", (status, folder_pk))


def active_folders() -> list[sqlite3.Row]:
    """Folders the watch loop should process, with the invite's name."""
    with get_db() as conn:
        return conn.execute('''
            SELECT folders.*, invites.name FROM folders JOIN invites ON invites.id = folders.invite_id
            WHERE folders.status = 'active' AND invites.revoked = 0
        ''').fetchall()


def record_check(folder_pk:int, merged:bool = False, error:str | None = None) -> None:
    """Save the result of a pass over a folder."""
    with get_db() as conn:
        stamp = now()
        conn.execute("UPDATE folders SET last_check = ?, last_error = ? WHERE id = ?", (stamp, error, folder_pk))
        if merged:
            conn.execute("UPDATE folders SET last_merge = ? WHERE id = ?", (stamp, folder_pk))
