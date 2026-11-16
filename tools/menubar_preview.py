"""Shows the real menu bar item and dashboard window on the synthetic demo, for looking at them on this Mac.

    "$PY" -E -s tools/menubar_preview.py [--scenario typical] [--seconds 60] [--report]

Everything runs in safe mode on the demo's synthetic accounts and in-memory process table (as
`run.py --demo`): no real account, process, Keychain item or the live service is touched. The menu works,
switches run against the demo, Open Dashboard shows the app's own window. Quit ends the preview, and so does
--seconds. Run it from Terminal (no bundle identity, so no notifications). --report opens the dashboard and
prints what AppKit shows (the item's place and size, its title and menu, and the window) after a few seconds.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
os.environ["ACCOUNTDECK_DEMO"] = "1"


def report(app, bar, holder) -> str:
    """What AppKit shows: the status item (visibility, place, width, title, image, menu) and the window."""
    from accountdeck.platform.appkit import NSRect

    def job():
        rt = bar.rt
        window = rt.send(bar.button, "window")
        frame = rt.send(window, "frame", restype=NSRect) if window else NSRect()
        screen = rt.send(window, "screen") if window else None
        screen_frame = rt.send(screen, "frame", restype=NSRect) if screen else NSRect()
        image = rt.send(bar.button, "image")
        reps = int(rt.send(rt.send(image, "representations"), "count", restype=ctypes.c_ulong)) if image else 0
        lines = [f"item visible={bool(rt.send(bar.item, 'isVisible', restype=ctypes.c_bool))} "
                 f"x={frame.origin.x:.0f} width={frame.size.width:.0f} of screen {screen_frame.size.width:.0f}",
                 f"title={rt.text(rt.send(bar.button, 'title'))!r} "
                 f"template={bool(rt.send(image, 'isTemplate', restype=ctypes.c_bool)) if image else None} "
                 f"image representations={reps}",
                 f"appearance={rt.text(rt.send(rt.send(bar.button, 'effectiveAppearance'), 'name'))}"]
        menu = rt.send(bar.item, "menu")
        for index in range(int(rt.send(menu, "numberOfItems", restype=ctypes.c_long)) if menu else 0):
            entry = rt.send(menu, "itemAtIndex:", index, argtypes=[ctypes.c_long])
            if rt.send(entry, "isSeparatorItem", restype=ctypes.c_bool):
                lines.append("  ----")
                continue
            key = rt.text(rt.send(entry, "keyEquivalent")) or ""
            alternate = rt.send(entry, "isAlternate", restype=ctypes.c_bool)
            lines.append(f"  {rt.text(rt.send(entry, 'title'))}" + (f"   ⌘{key.upper()}" if key else "")
                         + (" (⌥ alternate)" if alternate else "")
                         + ("   ▸" if rt.send(entry, "hasSubmenu", restype=ctypes.c_bool) else ""))
        dashboard = holder.get("window")
        if dashboard is not None and dashboard.window:
            frame = rt.send(dashboard.window, "frame", restype=NSRect)
            lines.append(f"dashboard window visible={bool(rt.send(dashboard.window, 'isVisible', restype=ctypes.c_bool))}"
                         f" size={frame.size.width:.0f}x{frame.size.height:.0f}"
                         f" activation policy={int(rt.send(app.ns_app, 'activationPolicy', restype=ctypes.c_long))}")
            if dashboard.web:
                url = rt.send(dashboard.web, "URL")
                lines.append(f"  page title={rt.text(rt.send(dashboard.web, 'title'))!r} "
                             f"loading={bool(rt.send(dashboard.web, 'isLoading', restype=ctypes.c_bool))} "
                             f"host={(rt.text(rt.send(url, 'host')) if url else None)!r} "
                             f"token still in the address={bool(url and rt.send(url, 'fragment'))}")
        else:
            lines.append("dashboard window: not open")
        return "\n".join(lines)

    def safe_job():
        try:
            return job()
        except Exception:
            import traceback
            return traceback.format_exc()
    return app.call_wait(safe_job, default="(the app stopped)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", default="typical")
    parser.add_argument("--seconds", type=float, default=0, help="quit by itself after this long (0: never)")
    parser.add_argument("--report", action="store_true", help="open the dashboard and print what AppKit shows")
    args = parser.parse_args()
    from accountdeck import demo
    from accountdeck.platform import dashboard_window, statusbar_mac
    from accountdeck.platform.appkit import MacApp
    from accountdeck.server import Server
    root = Path(tempfile.mkdtemp(prefix="accountdeck-menubar-preview-"))
    engine = demo.build_demo(root, args.scenario)
    server = Server(("127.0.0.1", 0), engine)
    (Path(engine.paths.data_dir) / "runtime.json").write_text(json.dumps(
        {"app": "accountdeck2", "host": "127.0.0.1", "port": server.server_port, "token": server.token}))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    app = MacApp()
    bar = statusbar_mac.AppKitStatusBar(app)
    holder = {}

    def open_url(url):
        def show():
            holder.setdefault("window", dashboard_window.DashboardWindow(app, app.open_url)).show(url)
            return True
        if not app.call_wait(show, default=False):
            print("(the dashboard window failed; the app would open the browser)", flush=True)
    handle = statusbar_mac.start(engine, app=app, bar=bar, sink=None, autostart_api=None, open_url=open_url,
                                 quit=app.stop)
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: app.stop())
    if args.seconds:
        threading.Timer(args.seconds, app.stop).start()
    if args.report:
        threading.Timer(1.0, handle.open_dashboard).start()
        threading.Timer(6.0, lambda: print(report(app, bar, holder), flush=True)).start()
    print(f"Menu bar preview ({args.scenario}, synthetic) is showing; choose Quit in its menu or press Ctrl+C.",
          flush=True)
    try:
        app.run()
    finally:
        handle.stop()
        server.shutdown()
        engine.stop()
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
