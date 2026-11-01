"""Manage invites and folders: python admin.py"""

from quick_ini import QuickIni

import db
from drive import DriveError, folder_key, open_storage

# INI file =============================================================
if(not QuickIni.load_file("config.ini", template=True)):
    print(f"Could not load config: {QuickIni.get_last_error()}\nFalling back to default settings")
db_path = QuickIni.get_value("db_path", "./jwlsync.db")
client_secret_path = QuickIni.get_value("client_secret_path", "./secrets/client_secret.json")
token_path = QuickIni.get_value("token_path", "./secrets/token.json")
# ======================================================================

db.set_db(db_path)
db.setup_db()


def show_invites():
    rows = db.list_invites()
    if not rows:
        print("No invites yet")
    for r in rows:
        state = "REVOKED" if r["revoked"] else (r["status"] or "no folder yet")
        print(f"  #{r['id']}  {r['code']}  {r['name']:<16} {state}")
        if r["link"]:
            print(f"        {r['link']}")
            print(f"        last check {r['last_check'] or '-'}, last merge {r['last_merge'] or '-'}")
            if r["last_error"]:
                print(f"        error: {r['last_error']}")


def pick_invite():
    show_invites()
    choice = input("Invite # or code: ").strip()
    for r in db.list_invites():
        if choice == str(r["id"]) or db.clean_code(choice) == r["code"]:
            return r
    print("No such invite")
    return None


def create_invite():
    name = input("Name (who it's for): ").strip()
    if name:
        print(f"Invite code for {name}: {db.create_invite(name)}")


def add_folder():
    code = input("Invite code: ")
    invite = db.get_invite(code)
    if not invite or invite["revoked"]:
        print("Not a valid invite code")
        return
    if db.get_folder(invite["id"]):
        print("That invite already has a folder")
        return
    link = input("Drive folder link: ").strip()
    try:
        storage = open_storage(link, token_path, client_secret_path)
        storage.setup()
        db.add_folder(invite["id"], link, folder_key(link))
    except (DriveError, ValueError) as e:
        print(f"Couldn't add it: {e}")
        return
    print(f"Tracking {invite['name']}'s folder; the watcher picks it up on its next pass")


def change_status():
    r = pick_invite()
    if not r:
        return
    if not r["folder_pk"]:
        print("That invite has no folder")
        return
    status = input(f"New status ({', '.join(db.STATUSES)}): ").strip().lower()
    try:
        db.set_status(r["folder_pk"], status)
        print(f"{r['name']}'s folder is now {status}")
    except ValueError as e:
        print(e)


def regenerate_code():
    r = pick_invite()
    if r:
        print(f"New code for {r['name']}: {db.regenerate_code(r['id'])} (the old one no longer works)")


def toggle_revoked():
    r = pick_invite()
    if not r:
        return
    db.set_revoked(r["id"], not r["revoked"])
    print(f"{r['name']}'s invite is now {'active' if r['revoked'] else 'revoked (folder stopped)'}")


MENU = {
    "1": ("Create invite", create_invite),
    "2": ("List invites and folders", show_invites),
    "3": ("Add folder for an invite", add_folder),
    "4": ("Pause / resume / stop a folder", change_status),
    "5": ("Regenerate an invite code", regenerate_code),
    "6": ("Revoke / unrevoke an invite", toggle_revoked),
}

while True:
    print()
    for key, (label, _) in MENU.items():
        print(f"{key}) {label}")
    choice = input("Choose (q to quit): ").strip().lower()
    if choice == "q":
        break
    if choice in MENU:
        MENU[choice][1]()
