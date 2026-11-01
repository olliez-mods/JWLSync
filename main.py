import secrets
import time
import traceback

from quick_ini import QuickIni

import db
import web
from drive import open_storage
from service import process
from storage import page_link, write_info

# INI file =============================================================
if(not QuickIni.load_file("config.ini", template=True)):
    print(f"Could not load config: {QuickIni.get_last_error()}\nFalling back to default settings")
db_path = QuickIni.get_value("db_path", "./jwlsync.db")
client_secret_path = QuickIni.get_value("client_secret_path", "./secrets/client_secret.json")
token_path = QuickIni.get_value("token_path", "./secrets/token.json")
interval = QuickIni.get_value("interval", 10, (int, float))
settle = QuickIni.get_value("settle", 5, (int, float))
web_host = QuickIni.get_value("web_host", "127.0.0.1")
web_port = QuickIni.get_value("web_port", 5080, int)
secure_cookie = QuickIni.get_value("secure_cookie", True, bool)
site_url = QuickIni.get_value("site_url")
admin_code = str(QuickIni.get_value("admin_code") or "")
secret_key = QuickIni.get_value("secret_key")
if(not secret_key):
    # Made once and saved, so logins survive restarts
    secret_key = QuickIni.write_value("secret_key", secrets.token_urlsafe(32), add_if_not_found=True)
# ======================================================================

db.set_db(db_path)
db.setup_db()

web.configure(str(secret_key), admin_code, token_path, client_secret_path, secure_cookie, site_url)
web.start(web_host, web_port)

# Folders are read from the database every pass, so ones added, paused or stopped (admin.py,
# the website) take effect without a restart
storages = {}  # folder row id -> storage
print("Watching folders in the database (add them with admin.py)")

while True:
    active = db.active_folders()
    for gone in set(storages) - {row["id"] for row in active}:
        del storages[gone]
    for row in active:
        try:
            storage = storages.get(row["id"])
            if storage is None:
                storage = open_storage(row["link"], token_path, client_secret_path)
                storage.setup()
                write_info(storage, row["name"], page_link(site_url, row["code"]))
                storages[row["id"]] = storage
                print(f"Watching {row['name']}'s folder")
            results = process(storage, settle=settle)
            for r in results:
                base = f" (base {r.base.method}:{r.base.merge_id})" if r.base else ""
                print(f"[{row['name']}] {r.upload}: {r.outcome}{base}" + (f" -> {r.merged}" if r.merged else ""))
                for line in r.summary.splitlines():
                    print(f"    {line}")
            db.record_check(row["id"], merged=any(r.merged for r in results))
        except Exception as e:
            # A failed pass (network, Drive, sharing changed) is retried next time with a fresh connection;
            # an interrupted run cleans up after itself
            traceback.print_exc()
            storages.pop(row["id"], None)
            db.record_check(row["id"], error=f"{type(e).__name__}: {e}")
    time.sleep(interval)
