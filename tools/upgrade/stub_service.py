"""A stand-in for the AccountDeck 2 service, for tools/upgrade_test.sh only: it answers install.sh's SERVICE_TOOL the
way a real service of the installed version would, without accounts, Keychain or network.

    python3.14 -I -B stub_service.py <data folder> <app folder>/run.py

The second argument is there for install.sh's SERVICE_MATCH (`python3[^ ]* .*<app folder>/run\\.py`): pgrep and pkill
find this process by it, as they find the real service. It writes runtime.json in the data folder, answers
/_health, /, /api/session, /api/state and /api/stop on a free loopback port, and goes on SIGTERM.

It serves the version in <app folder>/accountdeck/__init__.py and the build in <app folder>/build.json, in the shape
of that version: before 2.1, /_health has no `build` and no `engine.pending` (install.sh then asks /api/state behind a
session). What it does comes from <app folder>/upgrade-stub.json ({"mode": ...}) or UPGRADE_STUB_MODE:
  healthy      (the default) ticks move, nothing stalls
  unhealthy    it reports stalled, ticks still
  busy         a switch is running: /_health or /api/state says so, /api/stop answers 409
  lose-record  at start it moves the first saved sign-in record (vault/<32 hex>.dpapi) to <root>/lost-records/, as a
               new version that loses an account would; healthy otherwise
  lose-antigravity
               at start it takes the first Antigravity account out, its row in antigravity_accounts and every copy of
               its record (vault/agy-<id>*.dpapi), as a new version that loses one would; the records and the database
               as it was go to <root>/lost-antigravity/; healthy otherwise
It refuses to run on the real data folder.
"""
import http.server
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import sqlite3
import sys
import threading
import time

RECORD = re.compile(r"[0-9a-f]{32}\.dpapi")


def version_of(app):
    text = (app / "accountdeck" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]*)"', text, re.M).group(1)


def build_of(app):
    """As accountdeck.__init__._read_build reads it."""
    try:
        value = json.loads((app / "build.json").read_text(encoding="utf-8"))
        commit, dirty, at = value.get("commit"), value.get("dirty"), value.get("installed_at")
    except (OSError, ValueError, AttributeError):
        return None
    return {"commit": commit if isinstance(commit, str) else None, "dirty": dirty if isinstance(dirty, bool) else None,
            "installed_at": at if type(at) in (int, float) else None}


def numbers(version):
    return tuple(int(re.match(r"\d*", part).group() or 0) for part in version.split("."))


def main():
    data, run_py = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    app = run_py.parent
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() / "Library/Application Support/AccountDeck2"
    if data == real or data.parent == real.parent:
        raise SystemExit("stub_service.py never runs on the real data folder")
    version, build = version_of(app), build_of(app)
    old_shape = numbers(version) < (2, 1)
    mode = os.environ.get("UPGRADE_STUB_MODE", "")
    if not mode:
        try:
            mode = json.loads((app / "upgrade-stub.json").read_text(encoding="utf-8")).get("mode", "healthy")
        except OSError:
            mode = "healthy"
    if mode == "lose-record":
        records = sorted(path for path in (data / "vault").glob("*.dpapi") if RECORD.fullmatch(path.name))
        if records:
            lost = data.parent.parent.parent / "lost-records"      # <root>/lost-records, outside the data folder
            lost.mkdir(exist_ok=True)
            shutil.move(records[0], lost / records[0].name)
    if mode == "lose-antigravity":
        lost = data.parent.parent.parent / "lost-antigravity"      # <root>/lost-antigravity, outside the data folder
        lost.mkdir(exist_ok=True)
        shutil.copy2(data / "accounts.sqlite3", lost / "accounts.sqlite3")
        db = sqlite3.connect(data / "accounts.sqlite3")
        try:
            (key,) = db.execute("SELECT id FROM antigravity_accounts ORDER BY id LIMIT 1").fetchone()
            db.execute("DELETE FROM antigravity_accounts WHERE id = ?", (key,))
            db.commit()
        finally:
            db.close()
        for path in (data / "vault").glob("agy-%s*.dpapi" % key):
            shutil.move(path, lost / path.name)
    token, session, cookie = secrets.token_urlsafe(24), secrets.token_urlsafe(24), "accountdeck2_stub"
    started = time.monotonic()
    pending = {"kind": "switch", "provider": "claude_cli", "phase": "applying"} if mode == "busy" else None

    def ticks():
        return 7 if mode == "unhealthy" else 1 + int((time.monotonic() - started) * 4)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, cookie_header=None):
            raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
            self.send_response(status)
            self.send_header("Content-Type", "application/json" if not isinstance(body, bytes) else "text/html")
            self.send_header("Content-Length", str(len(raw)))
            if cookie_header:
                self.send_header("Set-Cookie", cookie_header)
            self.end_headers()
            self.wfile.write(raw)

        def host_ok(self):
            return self.headers.get("Host") == "127.0.0.1:%d" % server.server_port

        def signed_in(self):
            return any(pair.strip() == "%s=%s" % (cookie, session)
                       for header in self.headers.get_all("Cookie", ()) for pair in header.split(";"))

        def do_GET(self):
            if not self.host_ok():
                return self.reply(403, {"error": "Local access only."})
            path = self.path.partition("?")[0]
            if path == "/_health":
                engine = {"ticks": ticks(), "tick_age_s": 0.2, "failing_phases": {},
                          "stalled": mode == "unhealthy", "stall_reasons": ["tick"] if mode == "unhealthy" else []}
                body = {"app": "accountdeck2", "version": version, "mode": "live", "safe_mode": True,
                        "stalled": mode == "unhealthy", "engine": engine}
                if not old_shape:
                    body["build"] = build
                    engine["pending"] = pending
                return self.reply(200, body)
            if path == "/":
                return self.reply(200, b"<!doctype html><title>AccountDeck 2 (stub)</title>")
            if path == "/api/state":
                if not self.signed_in():
                    return self.reply(401, {"error": "Sign in."})
                return self.reply(200, {"pending": pending, "accounts": []})
            return self.reply(404, {"error": "Not found."})

        def do_POST(self):
            origin = "http://127.0.0.1:%d" % server.server_port
            if not self.host_ok() or self.headers.get("Origin") != origin or self.headers.get("X-AccountDeck") != "1" \
                    or self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return self.reply(403, {"error": "Invalid request."})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            path = self.path.partition("?")[0]
            if path == "/api/session":
                if body.get("token") != token:
                    return self.reply(401, {"error": "Sign in."})
                return self.reply(200, {"ok": True}, "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (cookie, session))
            if not self.signed_in():
                return self.reply(401, {"error": "Sign in."})
            if path == "/api/stop":
                if pending:
                    return self.reply(409, {"error": "Finish or cancel the current action first."})
                self.reply(200, {"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.3), finish()), daemon=True).start()
                return None
            return self.reply(404, {"error": "Not found."})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    runtime = data / "runtime.json"
    pid_dir = os.environ.get("UPGRADE_STUB_PIDS")
    pid_file = Path(pid_dir) / str(os.getpid()) if pid_dir else None

    def finish(*_):
        try:
            if json.loads(runtime.read_text(encoding="utf-8")).get("pid") == os.getpid():
                runtime.unlink()
        except (OSError, ValueError):
            pass
        if pid_file is not None:
            pid_file.unlink(missing_ok=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, finish)
    if pid_file is not None:
        pid_file.write_text("%s\n" % sys.argv[0])
    staged = data / ".runtime.json.stub"
    staged.write_text(json.dumps({"app": "accountdeck2", "host": "127.0.0.1", "port": server.server_port,
                                  "token": token, "pid": os.getpid()}), encoding="utf-8")
    os.chmod(staged, 0o600)
    os.replace(staged, runtime)
    print("stub %s (%s, %s shape) on port %d" % (version, mode, "2.0" if old_shape else "2.1+", server.server_port),
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
