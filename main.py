import os
import time
import traceback

from quick_ini import QuickIni

from service import process
from storage import LocalStorage

# INI file =============================================================
if(not QuickIni.load_file("config.ini", template=True)):
    print(f"Could not load config: {QuickIni.get_last_error()}\nFalling back to default settings")
folders = QuickIni.get_value("folders") or []
if(not isinstance(folders, list)):
    folders = [folders]
client_secret_path = QuickIni.get_value("client_secret_path", "./secrets/client_secret.json")
token_path = QuickIni.get_value("token_path", "./secrets/token.json")
interval = QuickIni.get_value("interval", 10, (int, float))
settle = QuickIni.get_value("settle", 5, (int, float))
# ======================================================================

if(not folders):
    raise SystemExit("No folders in config.ini, add them under folders=")


def open_storage(folder):
    if(not folder.startswith("https://")):
        return LocalStorage(folder)
    from drive import GoogleDrive, authorize, load_credentials, open_folder
    if(not os.path.exists(token_path)):
        print("No bot token yet, log in as the bot account in the browser")
        authorize(client_secret_path, token_path)
    return open_folder(GoogleDrive(load_credentials(token_path)), folder)


storages = []
for folder in folders:
    storage = open_storage(str(folder))
    storage.setup()
    storages.append(storage)
    print(f"Watching {folder}")

while True:
    for storage in storages:
        try:
            for r in process(storage, settle=settle):
                base = f" (base {r.base.method}:{r.base.merge_id})" if r.base else ""
                print(f"{r.upload}: {r.outcome}{base}" + (f" -> {r.merged}" if r.merged else ""))
                for line in r.summary.splitlines():
                    print(f"    {line}")
        except Exception:
            # A failed pass (network, Drive) is retried next time; an interrupted run cleans up after itself
            traceback.print_exc()
    time.sleep(interval)
