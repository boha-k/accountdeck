"""Open AccountDeck 2, run its local service, or run the synthetic demo (PLAN §8.1).

    run.py [--app] [--no-browser]   the app (what AccountDeck 2.app and its LaunchAgent run): open the
                                    dashboard of the running service, or become the service with the
                                    menu bar item in this process
    run.py --serve                  the service without the menu bar (development, a terminal session)
    run.py --demo [SCENARIO]        synthetic accounts in a temporary folder (safe mode)
    run.py --set-autostart on|off   open AccountDeck 2 at login (a LaunchAgent)
    run.py --import-from-pc BUNDLE --passphrase-file FILE
    run.py --move-status            the PC-to-Mac import, through the running service
    run.py --doctor [--json]        is it healthy, and does it fit the Claude Code just installed? (read-only;
                                    exit 0 healthy, 1 problems)
    run.py --version                the version and the build install.sh stamped
"""
from __future__ import annotations

import argparse
from datetime import datetime
import errno
import http.cookiejar
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
STARTUP_WAIT = 30.0        # a starting copy may wait on a Keychain prompt before it serves
PORT_WAIT = 20.0           # a busy port is tried this long: it may belong to a copy that is going away
# Loopback requests must never be routed through a system or environment proxy.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="run.py", description="AccountDeck 2 local dashboard")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--app", action="store_true",
                      help="open the running app, or become it (menu bar + service); the default")
    mode.add_argument("--serve", action="store_true", help="run the service without the menu bar item")
    mode.add_argument("--demo", nargs="?", const="typical", metavar="SCENARIO",
                      help="run the demo with synthetic accounts only (default scenario: typical)")
    mode.add_argument("--set-autostart", choices=("on", "off"), help="open AccountDeck 2 when you log in")
    mode.add_argument("--import-from-pc", type=Path, metavar="BUNDLE",
                      help="preview and import a bundle from the PC through the running service")
    mode.add_argument("--move-status", action="store_true", help="print the import status from the running service")
    mode.add_argument("--doctor", action="store_true", help="the read-only health report; exit 0 healthy, 1 problems")
    mode.add_argument("--version", action="store_true", help="print the version and build")
    parser.add_argument("--json", action="store_true", help="with --doctor: the report as JSON")
    parser.add_argument("--synthetic-root", type=Path, metavar="DIR",
                        help="with --doctor, in safe mode only: report on this synthetic home (tools/check.sh)")
    parser.add_argument("--passphrase-file", type=Path, metavar="FILE",
                        help="read the bundle passphrase from this file (never printed)")
    parser.add_argument("--now", type=_iso_time, metavar="ISO", help="demo clock start, e.g. 2026-09-29T15:00:00")
    parser.add_argument("--no-browser", action="store_true", help="do not open the dashboard")
    parser.add_argument("--data", type=Path,
                        help="data folder (default: ~/Library/Application Support/AccountDeck2)")
    parser.add_argument("--port", type=_port, help="port (default 53157; the demo picks a free port)")
    args = parser.parse_args(argv)
    if args.now is not None and args.demo is None:
        parser.error("--now works only with --demo")
    if (args.import_from_pc is not None) != (args.passphrase_file is not None):
        parser.error("--import-from-pc and --passphrase-file go together")
    if (args.json or args.synthetic_root is not None) and not args.doctor:
        parser.error("--json and --synthetic-root work only with --doctor")
    return args


def _iso_time(text: str) -> float:
    try:
        return datetime.fromisoformat(text.strip()).timestamp()
    except ValueError:
        raise argparse.ArgumentTypeError("use an ISO-8601 time such as 2026-09-29T15:00:00") from None


def _port(text: str) -> int:
    if not text.isdigit() or int(text) > 65535:
        raise argparse.ArgumentTypeError("use a port number from 0 to 65535")
    return int(text)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.demo is not None:
        return run_demo(args)
    if args.doctor:
        return run_doctor(args)
    if args.version:
        from accountdeck import BUILD, __version__, build_text
        print(f"AccountDeck 2 {__version__} ({build_text(BUILD)})")
        return 0
    from accountdeck.config import Paths, SafetyViolation
    from accountdeck.core import DatabaseDamaged
    paths = None
    try:
        if args.set_autostart:
            return set_autostart(args.set_autostart == "on")
        paths = Paths.live(args.data)
        if args.import_from_pc is not None or args.move_status:
            return run_move_cli(args, paths)
        if args.serve:
            serve(paths, args.port)
            return 0
        return run_app(paths, port=args.port, open_browser=not args.no_browser)
    except SafetyViolation as exc:
        print(f"AccountDeck 2 is in safe mode: {exc} Only --demo runs here.", file=sys.stderr)
        return 3
    except DatabaseDamaged as exc:      # --serve; the app says so itself (`run_app`)
        report_database_damaged(paths, exc)
        return 1
    except Exception as exc:
        report_startup_failure(paths, exc)
        return 1


def run_doctor(args) -> int:
    """`--doctor [--json]`: accountdeck/doctor.py's report; exit 0 healthy, 1 problems. Outside main()'s handler,
    which would write startup-error.txt: this mode leaves nothing behind, not even .pyc files. In safe mode only
    a synthetic home is looked at."""
    sys.dont_write_bytecode = True
    from accountdeck import doctor
    from accountdeck.config import Paths, SafetyViolation
    try:
        paths = Paths.synthetic(args.synthetic_root) if args.synthetic_root else Paths.live(args.data)
        report = doctor.run(paths)
    except SafetyViolation as exc:
        print(f"AccountDeck 2 is in safe mode: {exc} Only --doctor --synthetic-root runs here.", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"The doctor could not finish: {type(exc).__name__}.", file=sys.stderr)
        return 1
    print(json.dumps(doctor.public(report), ensure_ascii=False, indent=2) if args.json else doctor.text(report))
    return 0 if report["ok"] else 1


def read_runtime(data: Path) -> dict:
    try:
        runtime = json.loads((Path(data) / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return runtime if isinstance(runtime, dict) else {}


def dashboard_address(runtime: dict) -> str:
    """The dashboard URL with its session token. Opened through webbrowser or NSWorkspace, never put in argv."""
    return f"http://127.0.0.1:{runtime['port']}/#token={runtime['token']}"


def healthy(runtime: dict) -> bool:
    from accountdeck.config import safe_mode
    try:
        if runtime.get("app") != "accountdeck2" or runtime.get("host") != "127.0.0.1":
            return False
        if safe_mode():   # refused here too: tests and demos never contact the live service (PLAN §4.1)
            return False
        with _LOCAL.open(f"http://127.0.0.1:{int(runtime['port'])}/_health", timeout=1) as response:
            return json.load(response).get("app") == "accountdeck2"
    except Exception:
        return False


# ---- the import from the PC, through the running service ----

class MoveCLIError(Exception):
    """A fixed, non-secret reason suitable for terminal output."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MoveClient:
    """Only the already-running loopback service; no engine, launcher, or provider access."""

    def __init__(self, paths):
        from accountdeck.config import SafetyViolation, safe_mode
        from accountdeck.procs import is_test_owned_path
        if safe_mode() and (paths.mode != "synthetic" or not is_test_owned_path(paths.data_dir)):
            raise SafetyViolation("Move commands cannot use live paths in safe mode.")
        runtime = read_runtime(paths.data_dir)
        port = runtime.get("port")
        if (runtime.get("app") != "accountdeck2" or runtime.get("host") != "127.0.0.1"
                or type(port) is not int or not 1 <= port <= 65535 or not runtime.get("token")):
            raise MoveCLIError("The service is not running for this data folder. Open AccountDeck 2 first.")
        if safe_mode() and (port == 53157 or runtime.get("mode") != "demo"):
            raise SafetyViolation("Move tests may contact only a synthetic service on a test port.")
        self.origin = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                                  urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request("/api/session", {"token": runtime["token"]})

    def request(self, path, body=None):
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.update({"Content-Type": "application/json", "X-AccountDeck": "1", "Origin": self.origin})
        try:
            with self.opener.open(urllib.request.Request(self.origin + path, data=data, headers=headers),
                                  timeout=660) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except urllib.error.HTTPError as exc:
            # Never echo server bodies: errors can contain submitted data.
            exc.close()
            if exc.code in (401, 403):
                raise MoveCLIError("The running service refused authentication. Reopen AccountDeck 2.") from None
            raise MoveCLIError("The service refused the import. Check the dashboard for details.") from None
        except (OSError, ValueError):
            raise MoveCLIError("The service did not answer. Check the dashboard before retrying.") from None

    def action(self, name, body=None):
        return self.request("/api/action", {**(body or {}), "action": name})


def _read_passphrase(path: Path) -> str:
    from accountdeck.config import safe_mode
    from accountdeck.procs import is_test_owned_path
    if safe_mode() and not is_test_owned_path(path):
        raise MoveCLIError("The passphrase file must be inside the synthetic test folder.")
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            passphrase = stream.read(4097).rstrip("\r\n")
        if not passphrase or len(passphrase) > 4096 or "\n" in passphrase or "\r" in passphrase:
            raise ValueError()
    except (OSError, ValueError):
        raise MoveCLIError("Cannot read a single passphrase from the passphrase file.") from None
    return passphrase


def run_move_cli(args, paths) -> int:
    """JSON summaries only; synthetic Paths are an in-process test seam, never a CLI bypass."""
    try:
        client = MoveClient(paths)
        if args.import_from_pc is not None:
            body = {"path": str(args.import_from_pc.resolve()), "passphrase": _read_passphrase(args.passphrase_file)}
            preview = client.action("import-bundle-preview", body).get("preview") or {}
            if preview.get("refused"):
                raise MoveCLIError("Import preview was refused. Check Import from PC in the dashboard.")
            result = client.action("import-bundle", body).get("result") or {}
            if not result.get("complete") or not result.get("code") or result.get("failures"):
                raise MoveCLIError("Import is incomplete; no confirmation code. Check the dashboard.")
            summary = {"code": result["code"], "counts": result["counts"]}
        else:
            imported = client.request("/api/state").get("move_import") or {}
            summary = {"deferred": (imported.get("deferred") or {}).get("count", 0), "on_pc": imported.get("on_pc", 0)}
        print(json.dumps(summary, ensure_ascii=True))
        return 0
    except MoveCLIError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (KeyError, TypeError):
        print("The service returned an incomplete import result. Check the dashboard.", file=sys.stderr)
        return 1


# ---- the app and the service ----

def run_app(paths, *, port=None, open_browser=True, make_app=None) -> int:
    """App mode, what the app bundle and its LaunchAgent run. A healthy service for this data folder already
    runs: open its dashboard (unless --no-browser) and exit 0. Otherwise this process becomes the service: HTTP
    server and engine on background threads, AppKit and the menu bar on the main thread, the dashboard opened
    once it serves. Quit (menu bar, /api/stop, SIGTERM) shuts it down in serve()'s order and exits 0. When
    another copy holds the service lock it is still starting: wait for it, then open its dashboard. A damaged
    database with no backup stops the start with a dialog and exits 0, so launchd does not start it over and over."""
    from accountdeck.config import SafetyViolation, safe_mode
    from accountdeck.core import DatabaseDamaged
    if safe_mode():
        raise SafetyViolation("The app is not started in safe mode.")
    runtime = read_runtime(paths.data_dir)
    if not healthy(runtime):
        app = (make_app or make_mac_app)()
        try:
            started = serve(paths, port, app=app, open_browser=open_browser)
        except DatabaseDamaged as exc:
            report_database_damaged(paths, exc)
            tell_database_damaged(app, exc)
            return 0
        if started:
            return 0
        deadline = time.monotonic() + STARTUP_WAIT
        runtime = read_runtime(paths.data_dir)
        while not healthy(runtime):
            if time.monotonic() > deadline:
                raise RuntimeError("AccountDeck 2 did not start in time.")
            time.sleep(0.2)
            runtime = read_runtime(paths.data_dir)
    if open_browser:
        webbrowser.open(dashboard_address(runtime))    # Python pipes the URL to osascript on stdin
    return 0


def make_mac_app():
    """The AppKit application object; built here, on the main thread."""
    from accountdeck.platform.appkit import MacApp
    return MacApp()


def serve_app(server, app) -> None:
    """Serves HTTP on a background thread while AppKit runs on this (the main) thread. Whichever ends first
    ends the other: the run loop when the server stops (/api/stop), the server when the run loop stops (Quit in
    the menu bar, SIGTERM, SIGINT)."""
    def http():
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            app.stop()
    thread = threading.Thread(target=http, name="ad2-http", daemon=True)
    previous = {number: signal.signal(number, lambda *_: app.stop()) for number in (signal.SIGTERM, signal.SIGINT)}
    thread.start()
    try:
        app.run()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        server.shutdown()
        thread.join(5.0)


def serve(paths, port=None, *, app=None, open_browser=False) -> bool | None:
    """The live service (PLAN §2.2-2.3, §8.1). A second instance on the same data folder returns None at once;
    one that cannot take the machine-wide owner lock runs read-only (§9.1). With `app` (app mode) the menu bar
    item shows and AppKit keeps the main thread (`serve_app`); `open_browser` then opens the dashboard once the
    server is up. Shutdown order: menu bar, engine (operations, scheduler), server, runtime.json, owner lock."""
    from accountdeck import __version__, diagnostics
    from accountdeck.config import env_log_level, env_port, owner_lock_path
    from accountdeck.core import DatabaseDamaged, OwnerLock
    from accountdeck.engine import build_engine
    from accountdeck.platform.facts_mac import secure_directory
    data = paths.data_dir
    secure_directory(data)
    service = OwnerLock(data / "service.lock")
    if not service.acquire():
        return None
    owner = OwnerLock(owner_lock_path(paths))
    try:
        try:
            owner.acquire()
        except OSError:
            pass                                    # not held: the engine runs read-only
        diagnostics.setup(paths, env_log_level() or "info")
        engine = build_engine(paths, owner_lock=owner)
        start_watchdog(engine, app)
        server = menubar = None
        try:
            server, moved = bind_server(engine, port if port is not None else env_port())
            runtime = {"app": "accountdeck2", "version": __version__, "host": "127.0.0.1", "port": server.server_port,
                       "token": server.token, "pid": os.getpid(), "started": time.time(), "mode": "live"}
            _write_json_atomic(data / "runtime.json", runtime)
            (data / "startup-error.txt").unlink(missing_ok=True)
            diagnostics.log("startup", version=__version__, port=server.server_port, mode="live",
                            owner_lock=owner.held, menubar=app is not None)
            if app is None:
                engine.announce(moved=moved)
                try:
                    server.serve_forever(poll_interval=0.5)
                except KeyboardInterrupt:
                    pass
                return True
            menubar = start_menubar(engine, app)        # the app's own window on the world: always shown here
            engine.attach_tray(menubar)
            engine.announce(moved=moved)                # after the notifier started: it delivers only what is newer
            app.on_system_quit = lambda: _system_quit(data)
            repair_autostart()
            if open_browser:
                if menubar is not None:
                    menubar.open_dashboard()            # in the app's own window, or the browser
                else:
                    app.open_url(dashboard_address(runtime))
            serve_app(server, app)
            return True
        finally:
            stop_menubar(menubar)
            engine.stop()
            if server is not None:
                server.server_close()
            (data / "runtime.json").unlink(missing_ok=True)
            diagnostics.log("shutdown")
    except DatabaseDamaged:
        diagnostics.log("db.unusable", lvl="error")
        raise
    finally:
        diagnostics.close()
        owner.release()
        service.release()


def bind_server(engine, wanted):
    """(the server, None), or (the server, (`wanted`, the port it got)) when another port had to do: the menu bar, the
    app window and a second launch all find the service by runtime.json. A port that stays busy for PORT_WAIT is
    someone else's. What a restart leaves in TIME_WAIT does not count as busy (server.Server)."""
    from accountdeck import diagnostics
    from accountdeck.config import DEFAULT_PORT
    from accountdeck.server import Server
    wanted = DEFAULT_PORT if wanted is None else wanted
    deadline = time.monotonic() + PORT_WAIT
    while True:
        try:
            return Server(("127.0.0.1", wanted), engine), None
        except OSError as exc:
            why = exc.errno
        if why != errno.EADDRINUSE or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    server = Server(("127.0.0.1", 0), engine)
    diagnostics.log("server.port_fallback", "warning", wanted=wanted, port=server.server_port, errno=why)
    return server, (wanted, server.server_port)


def start_watchdog(engine, app) -> None:
    """engine.Watchdog, not in safe mode. Exiting restarts only a copy launchd started; an app opened from Finder or
    Spotlight has nobody to restart it, so that one replaces itself in place (the kernel frees its port and locks)."""
    from accountdeck.config import safe_mode
    from accountdeck.platform.autostart_mac import LABEL
    if safe_mode():
        return
    engine.start_watchdog(relaunch=(lambda: os.execv(sys.executable, sys.orig_argv))
                          if app is not None and os.environ.get("XPC_SERVICE_NAME") != LABEL else None)


def report_database_damaged(paths, exc) -> None:
    """Its message is fixed text written to be read, unlike report_startup_failure's exceptions."""
    if paths is not None and paths.mode == "live":
        try:
            (paths.data_dir / "startup-error.txt").write_text(str(exc), encoding="utf-8")
        except OSError:
            pass
    print(str(exc), file=sys.stderr)


def tell_database_damaged(app, exc) -> None:
    """A notification, then a dialog: this copy has no menu bar item or dashboard to say it with."""
    from accountdeck import diagnostics
    try:
        from accountdeck.platform import notify_mac, statusbar_mac
        sink = notify_mac.make_sink(app)
        if sink is not None:
            sink.show(exc.TITLE, str(exc), "service", "critical")
        statusbar_mac.show_critical(app, exc.TITLE, str(exc))
    except Exception as exc2:
        diagnostics.log_exception("db.unusable_not_shown", exc2)


def _system_quit(data: Path) -> None:
    """macOS ends the app now (log out, restart, shut down): leave no runtime.json behind and flush the log.
    Everything else is crash-safe (the owner lock dies with the process; operations recover on next start)."""
    from accountdeck import diagnostics
    try:
        (data / "runtime.json").unlink(missing_ok=True)
        diagnostics.log("shutdown", reason="system")
        diagnostics.close()
    except Exception:
        pass


def start_menubar(engine, app):
    """The menu bar item (platform/statusbar_mac.py); the service runs on without it."""
    from accountdeck import diagnostics
    try:
        from accountdeck.platform import statusbar_mac
        return statusbar_mac.start(engine, app=app)
    except Exception as exc:
        diagnostics.log_exception("menubar.unavailable", exc)
        return None


def stop_menubar(handle) -> None:
    """Stops the menu bar first on shutdown; a failure there never skips the rest of the shutdown."""
    from accountdeck import diagnostics
    if handle is None:
        return
    try:
        handle.stop()
    except Exception as exc:
        diagnostics.log_exception("menubar.stop_failed", exc)


def repair_autostart() -> None:
    """A registered LaunchAgent that names a moved app is rewritten; any failure is only logged."""
    from accountdeck import diagnostics
    from accountdeck.config import safe_mode
    if safe_mode():
        return
    try:
        from accountdeck import autostart
        autostart.repair()
    except Exception as exc:
        diagnostics.log("autostart.repair_failed", error_type=type(exc).__name__, code=getattr(exc, "code", None))


def set_autostart(enabled: bool) -> int:
    """The LaunchAgent that opens AccountDeck 2 at login. A refusal (`blocked`: not written; `unsupported`:
    not installed) is printed with its reason."""
    from accountdeck import autostart
    from accountdeck.config import SafetyViolation, safe_mode
    from accountdeck.core import DeckError
    if safe_mode():
        raise SafetyViolation("Start at login is not changed in safe mode.")
    try:
        status = autostart.set_enabled(enabled)
    except DeckError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if enabled and status.approved is False:
        print("AccountDeck 2 is set to open at login, but macOS has it switched off in System Settings › General "
              "› Login Items. Switch it on there.")
    else:
        print("AccountDeck 2 will open when you log in to this Mac." if enabled
              else "AccountDeck 2 will no longer open when you log in to this Mac.")
    return 0


# ---- the demo ----

def run_demo(args) -> int:
    """Serves synthetic accounts from a temporary folder; safe mode forbids every real process, file and port
    for this process and its children (PLAN §11.4)."""
    os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
    os.environ["ACCOUNTDECK_DEMO"] = "1"
    demo = importlib.import_module("accountdeck.demo")
    from accountdeck import diagnostics
    from accountdeck.server import Server
    root = Path(tempfile.mkdtemp(prefix="accountdeck2-demo-"))
    try:
        engine = demo.build_demo(root, args.demo, now=args.now)
        server = None
        try:
            server = Server(("127.0.0.1", args.port or 0), engine)
            # The token only opens this synthetic demo, so printing the URL is safe.
            url = f"http://127.0.0.1:{server.server_port}/#token={server.token}"
            print(f"AccountDeck 2 demo ({args.demo}, synthetic data only): {url}", flush=True)
            print("Press Ctrl+C to stop.", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                pass
        finally:
            engine.stop()
            if server is not None:
                server.server_close()
    finally:
        diagnostics.close()
        shutil.rmtree(root, ignore_errors=True)
    return 0


def report_startup_failure(paths, exc: Exception) -> None:
    # Only the exception class is stored: messages can carry provider data or paths.
    message = f"AccountDeck 2 could not start: {type(exc).__name__}. Run run.py in a terminal for details."
    if paths is not None and paths.mode == "live":
        try:
            paths.data_dir.mkdir(parents=True, exist_ok=True)
            (paths.data_dir / "startup-error.txt").write_text(message, encoding="utf-8")
        except OSError:
            pass
    print(message, file=sys.stderr)


def _write_json_atomic(path: Path, value: dict) -> None:
    handle, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
