"""Website: invite code entry (/), each person's page (/me) and the admin page (/admin).

Runs in a thread inside main.py on a local port; QuickRoute puts HTTPS in front of it.
"""

import hmac
import os
import re
import threading
import time
from datetime import datetime, timezone

from flask import Flask, abort, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import make_server

import db
from drive import DriveError, folder_key, open_storage
from storage import INFO, page_link, write_info

app = Flask(__name__)
# Behind QuickRoute every request comes from 127.0.0.1; use the visitor's address it passes on
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
settings = {}


def configure(secret_key:str, admin_code:str, token_path:str, client_secret_path:str, secure_cookie:bool = True,
              site_url:str | None = None):
    app.secret_key = secret_key
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=secure_cookie)
    settings.update(admin_code=admin_code, token_path=token_path, client_secret_path=client_secret_path,
                    site_url=site_url)


def update_info(folder, name:str, code:str) -> None:
    """Rewrite the folder's User/JWLSync.txt; a failure here never blocks the action that triggered it."""
    try:
        storage = open_storage(folder["link"], settings["token_path"], settings["client_secret_path"])
        write_info(storage, name, invite_link(code))
    except Exception as e:
        print(f"Couldn't update {name}'s {INFO}: {e}")


def start(host:str, port:int):
    server = make_server(host, port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Website on http://{host}:{port}")


# Guessing codes ========================================================
MAX_FAILURES = 10       # wrong codes allowed per IP...
FAILURE_WINDOW = 600    # ...within this many seconds
failures = {}
failures_lock = threading.Lock()

def _recent_failures(ip:str) -> list[float]:
    now = time.time()
    recent = [t for t in failures.get(ip, []) if now - t < FAILURE_WINDOW]
    failures[ip] = recent
    return recent

def blocked() -> bool:
    with failures_lock:
        return len(_recent_failures(request.remote_addr)) >= MAX_FAILURES

def failed() -> None:
    with failures_lock:
        _recent_failures(request.remote_addr).append(time.time())


# Helpers ==============================================================
def current_invite():
    """The logged in person's invite, or None. Regenerating or revoking a code logs them out."""
    code = session.get("code")
    invite = db.get_invite(code) if code else None
    if invite is None or invite["revoked"]:
        session.pop("code", None)
        return None
    return invite


LOG_HEADER = re.compile(r"^(\S+ \S+)Z\s+(.*) \((.*)\)\s+(\w+)$")
LOG_CHANGE = re.compile(r"^(\w+)\s+\+(\d+) -(\d+) ~(\d+)")
FRIENDLY = {"usermarks": "Highlights", "tagmaps": "Tagged items", "inputfields": "Input fields"}

def parse_entry(text:str) -> dict:
    """A merge-log entry as {when, device, outcome, changes} for the user page."""
    lines = [l.strip() for l in text.splitlines()]
    m = LOG_HEADER.match(lines[0])
    if not m:
        return {"when": None, "device": lines[0], "outcome": "", "changes": lines[1:]}
    when = datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
    changes = []
    for line in lines[1:]:
        c = LOG_CHANGE.match(line)
        if c:
            counts = [f"{sign}{n}" for sign, n in (("+", c[2]), ("−", c[3]), ("~", c[4])) if n != "0"]
            changes.append(f"{FRIENDLY.get(c[1], c[1].capitalize())} {' '.join(counts)}")
        elif not line.startswith(("base:", "wrote:")) and line != "no changes":
            changes.append(line)
    return {"when": when, "device": m[3].replace("_", " "), "outcome": m[4], "changes": changes}


recent_cache = {}  # folder row id -> (time, entries); keeps page loads from hitting Drive every time
RECENT_SECONDS = 30

def recent_merges(folder, count:int = 5) -> list[dict] | None:
    """The last few merge-log entries from the folder's User/merge-log.txt, newest first."""
    cached = recent_cache.get(folder["id"])
    if cached and time.time() - cached[0] < RECENT_SECONDS:
        return cached[1]
    try:
        storage = open_storage(folder["link"], settings["token_path"], settings["client_secret_path"])
        text = storage.read_text("merge-log.txt") or ""
    except Exception:
        return None
    # Only what happened to their backups; leftovers and notes are housekeeping
    entries = [parse_entry(e.strip()) for e in text.split("\n\n") if e.strip()]
    entries = [e for e in entries if e["outcome"] in ("merged", "unchanged", "superseded", "rejected")][-count:][::-1]
    recent_cache[folder["id"]] = (time.time(), entries)
    return entries


@app.context_processor
def static_version():
    # Changes whenever style.css does, so browsers fetch the new one instead of a cached copy
    return {"css_version": int(os.path.getmtime(os.path.join(app.static_folder, "style.css")))}


@app.template_filter("ago")
def ago(stamp:str | None) -> str:
    if not stamp:
        return "never"
    seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


# Main page ============================================================
@app.get("/")
def index():
    # An invite link (/?code=XXXXX-XXXXX) signs in like typing the code
    if request.args.get("code"):
        return sign_in(request.args["code"])
    invite = current_invite()
    folder = db.get_folder(invite["id"]) if invite else None
    return render_template("index.html", invite=invite, folder=folder)


@app.post("/code")
def enter_code():
    return sign_in(request.form.get("code", ""))


def sign_in(code:str):
    if blocked():
        return render_template("index.html", error="Too many wrong codes, try again in a few minutes"), 429
    invite = db.get_invite(code)
    if invite is None or invite["revoked"]:
        failed()
        return render_template("index.html", error="That code isn't valid"), 400
    session["code"] = invite["code"]
    # Redirect either way, so a code from a link doesn't stay in the address bar
    return redirect(url_for("me") if db.get_folder(invite["id"]) else url_for("index"))


@app.template_global()
def invite_link(code:str) -> str:
    # https://jwlsync.antinoid.com/?code=...: site_url from config.ini, else the address this request came in on
    return page_link(settings.get("site_url"), code) or url_for("index", code=code, _external=True)


@app.post("/folder")
def add_folder():
    invite = current_invite()
    if invite is None:
        return redirect(url_for("index"))
    if db.get_folder(invite["id"]):
        return redirect(url_for("me"))
    link = request.form.get("link", "").strip()
    error = None
    if not link.startswith("https://drive.google.com/"):
        error = "Paste the link to a Google Drive folder (https://drive.google.com/...)"
    else:
        try:
            storage = open_storage(link, settings["token_path"], settings["client_secret_path"])
            storage.setup()
            folder_pk = db.add_folder(invite["id"], link, folder_key(link))
            update_info({"id": folder_pk, "link": link}, invite["name"], invite["code"])
        except (DriveError, ValueError) as e:
            error = str(e)
        except Exception:
            error = "Couldn't set up that folder, check the link and its sharing and try again"
    if error:
        return render_template("index.html", invite=invite, link=link, error=error), 400
    return redirect(url_for("me"))


# User page ============================================================
@app.get("/me")
def me():
    invite = current_invite()
    folder = db.get_folder(invite["id"]) if invite else None
    if folder is None:
        return redirect(url_for("index"))
    return render_template("me.html", invite=invite, folder=folder, recent=recent_merges(folder))


@app.post("/me/status")
def me_status():
    invite = current_invite()
    folder = db.get_folder(invite["id"]) if invite else None
    if folder is None:
        return redirect(url_for("index"))
    status = {"pause": "paused", "resume": "active", "stop": "stopped"}.get(request.form.get("action"))
    if status:
        db.set_status(folder["id"], status)
    return redirect(url_for("me"))


@app.post("/logout")
def logout():
    session.pop("code", None)
    return redirect(url_for("index"))


# Admin page ===========================================================
def admin_page(**extra):
    return render_template("admin.html", invites=db.list_invites(), **extra)


@app.get("/admin")
def admin():
    if not settings.get("admin_code"):
        abort(404)
    if not session.get("admin"):
        return render_template("admin_login.html")
    return admin_page()


@app.post("/admin/login")
def admin_login():
    if not settings.get("admin_code"):
        abort(404)
    if blocked():
        return render_template("admin_login.html", error="Too many wrong codes, try again in a few minutes"), 429
    if not hmac.compare_digest(request.form.get("admin_code", ""), settings["admin_code"]):
        failed()
        return render_template("admin_login.html", error="Wrong admin code"), 400
    session["admin"] = True
    return redirect(url_for("admin"))


@app.post("/admin/invite")
def admin_invite():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    name = request.form.get("name", "").strip()
    if not name:
        return admin_page(error="Give the invite a name")
    return admin_page(new_code=db.create_invite(name), new_name=name)


@app.post("/admin/action")
def admin_action():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    invite_id = request.form.get("invite_id", type=int)
    action = request.form.get("action")
    folder = db.get_folder(invite_id)
    if action in ("pause", "resume", "stop") and folder:
        db.set_status(folder["id"], {"pause": "paused", "resume": "active", "stop": "stopped"}[action])
    elif action == "regenerate":
        code = db.regenerate_code(invite_id)
        name = next(r["name"] for r in db.list_invites() if r["id"] == invite_id)
        if folder:
            update_info(folder, name, code)  # the link in their Drive folder would be dead otherwise
        return admin_page(new_code=code, new_name=name)
    elif action in ("revoke", "unrevoke"):
        db.set_revoked(invite_id, action == "revoke")
    return redirect(url_for("admin"))


@app.post("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin"))
