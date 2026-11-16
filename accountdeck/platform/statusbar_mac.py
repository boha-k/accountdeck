"""The menu bar item on screen (docs/mac/PLAN.md D8, §1.10): one NSStatusItem, driven by platform/menubar.py.

`AppKitStatusBar` is the StatusBar seam `menubar.MenuBar` talks to. Its setters queue their AppKit work on
the main thread (`MacApp.call`) and return at once; `alert` waits for the answer. The item keeps its place
when the owner ⌘-drags it (autosave name), stays narrow (a 16-pt ring plus a short title, so it is not the
first item macOS hides behind the notch) and draws its ring at 1× and 2×, as a template image whenever the
ring is in the menu bar's own colour.

`start` builds the item in the running app (run.py --app) and starts the menu bar thread. Tests pass their
own `bar` (and sink); without one, `start` refuses in safe mode and outside the app process.
"""
from __future__ import annotations

from collections import deque
import ctypes
import threading

from .. import config, diagnostics
from ..config import SafetyViolation
from .appkit import COMMAND, OPTION, MacApp, NSSize
from .menubar import QUIT, MenuBar, MenuBarHandle

__all__ = ["AppKitStatusBar", "MacApp", "MenuBarHandle", "start"]

STARTUP_WAIT = 10.0
VARIABLE_LENGTH = -1.0          # NSVariableStatusItemLength
IMAGE_LEFT = 2                  # NSImageLeft
AUTOSAVE_NAME = "local.accountdeck2.status"
ALERT_FIRST = 1000              # NSAlertFirstButtonReturn
ALERT_STYLES = {"warning": 0, "informational": 1}
ALERT_CRITICAL = 2              # NSAlertStyleCritical
LABEL_SHRINK = 2.0              # points smaller than the menu bar font for the "5h"/"wk" labels
NO_APP = "The menu bar item needs the AccountDeck 2 app (run.py --app)."
ACCESSIBILITY_LABEL = "AccountDeck 2"


class AppKitStatusBar:
    """The StatusBar seam on a real NSStatusItem (see menubar.MenuBar for the calls)."""

    def __init__(self, app: MacApp):
        self.app, self.rt = app, app.rt
        self.item = self.button = self.menu = None
        self.is_dark = False
        self.open = False
        self.on_open = None         # called on the main thread as the menu opens (start: the menu bar's "look")
        self.waiting = None         # a menu to put in place once the open one closes
        self.builds = deque()       # tags of the menus still on screen, oldest first
        self.removed = False
        app.call_wait(self._create)
        app.tickers.append(self)

    def _create(self) -> None:
        rt = self.rt
        self.item = rt.send(rt.send(rt.cls("NSStatusBar"), "systemStatusBar"), "statusItemWithLength:",
                            VARIABLE_LENGTH, argtypes=[ctypes.c_double])
        rt.send(self.item, "retain")
        rt.send(self.item, "setAutosaveName:", rt.string(AUTOSAVE_NAME), restype=None)
        self.button = rt.send(self.item, "button")
        rt.send(self.button, "setImagePosition:", IMAGE_LEFT, restype=None, argtypes=[ctypes.c_long])
        self._tick()

    def _live(self) -> bool:
        return bool(self.item) and not self.removed

    def _tick(self) -> None:
        """Main thread, every second: notes whether the menu bar is dark (for the coloured rings)."""
        if not self._live():
            return
        rt = self.rt
        appearance = rt.send(self.button, "effectiveAppearance")
        name = rt.text(rt.send(appearance, "name")) if appearance else None
        self.is_dark = bool(name and "Dark" in name)

    # -- the seam --

    def dark(self) -> bool:
        return self.is_dark

    def set_title(self, runs) -> None:
        self.app.call(lambda: self._set_title(runs))

    def _set_title(self, runs) -> None:
        if not self._live():
            return
        rt = self.rt
        title = rt.send(rt.send(rt.cls("NSMutableAttributedString"), "alloc"), "init")
        try:
            size = rt.send(rt.cls("NSFont"), "systemFontSize", restype=ctypes.c_double)
            value_font = rt.send(rt.cls("NSFont"), "monospacedDigitSystemFontOfSize:weight:", size, 0.0,
                                 argtypes=[ctypes.c_double, ctypes.c_double])
            label_font = rt.send(rt.cls("NSFont"), "systemFontOfSize:", size - LABEL_SHRINK,
                                 argtypes=[ctypes.c_double])
            font_key = rt.constant(rt.appkit, "NSFontAttributeName")
            colour_key = rt.constant(rt.appkit, "NSForegroundColorAttributeName")
            label_colour = rt.send(rt.cls("NSColor"), "secondaryLabelColor")
            for text, role in runs:
                attributes = rt.send(rt.cls("NSMutableDictionary"), "dictionary")
                setter = "setObject:forKey:"
                if role == "label":
                    rt.send(attributes, setter, label_font, font_key, restype=None)
                    rt.send(attributes, setter, label_colour, colour_key, restype=None)
                else:
                    rt.send(attributes, setter, value_font, font_key, restype=None)
                run = rt.send(rt.send(rt.cls("NSAttributedString"), "alloc"), "initWithString:attributes:",
                              rt.string(text), attributes)
                rt.send(title, "appendAttributedString:", run, restype=None)
                rt.send(run, "release", restype=None)
            rt.send(self.button, "setAttributedTitle:", title, restype=None)
        finally:
            rt.send(title, "release", restype=None)

    def set_tooltip(self, text: str) -> None:
        self.app.call(lambda: self._live() and self.rt.send(self.button, "setToolTip:", self.rt.string(text),
                                                              restype=None))

    def set_image(self, images, points: int, template: bool) -> None:
        self.app.call(lambda: self._set_image(images, points, template))

    def _set_image(self, images, points: int, template: bool) -> None:
        """One NSImage with a bitmap representation per display scale, so the ring is drawn pixel for pixel
        on Retina and on 1× displays alike."""
        if not self._live():
            return
        rt = self.rt
        size = NSSize(points, points)
        image = rt.send(rt.send(rt.cls("NSImage"), "alloc"), "initWithSize:", size, argtypes=[NSSize])
        try:
            for data, _scale in images:
                blob = rt.send(rt.cls("NSData"), "dataWithBytes:length:", data, len(data),
                               argtypes=[ctypes.c_char_p, ctypes.c_ulong])
                rep = rt.send(rt.cls("NSBitmapImageRep"), "imageRepWithData:", blob)
                if rep:
                    rt.send(rep, "setSize:", size, restype=None, argtypes=[NSSize])
                    rt.send(image, "addRepresentation:", rep, restype=None)
            rt.send(image, "setTemplate:", bool(template), restype=None, argtypes=[ctypes.c_bool])
            rt.send(image, "setAccessibilityDescription:", rt.string(ACCESSIBILITY_LABEL), restype=None)
            rt.send(self.button, "setImage:", image, restype=None)
        finally:
            rt.send(image, "release", restype=None)

    def set_menu(self, items, choose) -> None:
        self.app.call(lambda: self._set_menu(items, choose))

    def _set_menu(self, items, choose) -> None:
        if not self._live():
            return
        if self.open:                   # never swap a menu while it is open: once it closes
            self.waiting = (items, choose)
            return
        rt, tags = self.rt, []
        menu = self._build(items, choose, tags)
        rt.send(menu, "setDelegate:", self.app.target, restype=None)
        rt.send(self.item, "setMenu:", menu, restype=None)
        if self.menu:
            rt.send(self.menu, "release", restype=None)
        self.menu = menu
        self.builds.append(tags)
        while len(self.builds) > 2:     # a click on the menu just replaced can still arrive
            for tag in self.builds.popleft():
                self.app.handlers.pop(tag, None)

    def _build(self, items, choose, tags):
        rt = self.rt
        menu = rt.send(rt.send(rt.cls("NSMenu"), "alloc"), "initWithTitle:", rt.string(""))
        rt.send(menu, "setAutoenablesItems:", False, restype=None, argtypes=[ctypes.c_bool])
        for item in items:
            if item.separator:
                rt.send(menu, "addItem:", rt.send(rt.cls("NSMenuItem"), "separatorItem"), restype=None)
                continue
            action = rt.sel("onMenu:") if item.command and not item.submenu else None
            entry = rt.send(rt.send(rt.cls("NSMenuItem"), "alloc"), "initWithTitle:action:keyEquivalent:",
                            rt.string(item.text), action, rt.string(item.key))
            if action:
                tag = self.app.menu_tag(lambda command=item.command: choose(command))
                tags.append(tag)
                rt.send(entry, "setTarget:", self.app.target, restype=None)
                rt.send(entry, "setTag:", tag, restype=None, argtypes=[ctypes.c_long])
            rt.send(entry, "setEnabled:", bool(item.enabled), restype=None, argtypes=[ctypes.c_bool])
            if item.checked:
                rt.send(entry, "setState:", 1, restype=None, argtypes=[ctypes.c_long])
            if item.alternate:
                rt.send(entry, "setKeyEquivalentModifierMask:", COMMAND | OPTION, restype=None,
                        argtypes=[ctypes.c_ulong])
                rt.send(entry, "setAlternate:", True, restype=None, argtypes=[ctypes.c_bool])
            if item.submenu:
                submenu = self._build(item.submenu, choose, tags)
                rt.send(entry, "setSubmenu:", submenu, restype=None)
                rt.send(submenu, "release", restype=None)
            rt.send(menu, "addItem:", entry, restype=None)
            rt.send(entry, "release", restype=None)
        return menu

    def _menu_open(self, menu, is_open: bool) -> None:
        if menu != self.menu:
            return
        self.open = is_open
        if is_open and self.on_open is not None:
            self.on_open()
        if not is_open and self.waiting is not None:
            waiting, self.waiting = self.waiting, None
            # Queued, so it runs after the click (if any) that closed the menu has been sent.
            with self.app.lock:
                self.app.work.append(lambda: self._set_menu(*waiting))
            self.app.wake()

    def alert(self, prompt) -> int:
        return self.app.call_wait(lambda: self._alert(prompt), default=-1)

    def _alert(self, prompt) -> int:
        """Shows an NSAlert and returns the index of the pressed button (prompt.buttons order)."""
        rt = self.rt
        alert = rt.send(rt.send(rt.cls("NSAlert"), "alloc"), "init")
        try:
            rt.send(alert, "setMessageText:", rt.string(prompt.title), restype=None)
            rt.send(alert, "setInformativeText:", rt.string(prompt.text), restype=None)
            rt.send(alert, "setAlertStyle:", ALERT_STYLES.get(prompt.style, 1), restype=None,
                    argtypes=[ctypes.c_ulong])
            for index, (answer, label) in enumerate(prompt.buttons):
                button = rt.send(alert, "addButtonWithTitle:", rt.string(label))
                key = "\r" if index == prompt.default else "\x1b" if answer is None else ""
                rt.send(button, "setKeyEquivalent:", rt.string(key), restype=None)
            self.app.activate()         # an accessory app's alert would open behind other windows
            response = rt.send(alert, "runModal", restype=ctypes.c_long)
        finally:
            rt.send(alert, "release", restype=None)
        index = response - ALERT_FIRST
        return index if 0 <= index < len(prompt.buttons) else -1

    def remove(self) -> None:
        self.app.call(self._remove)

    def _remove(self) -> None:
        if self.removed or not self.item:
            return
        self.removed = True
        rt = self.rt
        rt.send(rt.send(rt.cls("NSStatusBar"), "systemStatusBar"), "removeStatusItem:", self.item, restype=None)
        rt.send(self.item, "release", restype=None)
        if self.menu:
            rt.send(self.menu, "release", restype=None)
            self.menu = None
        for tags in self.builds:
            for tag in tags:
                self.app.handlers.pop(tag, None)
        self.builds.clear()
        if self in self.app.tickers:
            self.app.tickers.remove(self)


def show_critical(app, title, text) -> None:
    """A critical dialog for a start that cannot go on, when there is no menu bar item to ask (main thread)."""
    rt = app.rt
    with rt.pool():
        alert = rt.send(rt.send(rt.cls("NSAlert"), "alloc"), "init")
        try:
            rt.send(alert, "setMessageText:", rt.string(title), restype=None)
            rt.send(alert, "setInformativeText:", rt.string(text), restype=None)
            rt.send(alert, "setAlertStyle:", ALERT_CRITICAL, restype=None, argtypes=[ctypes.c_ulong])
            rt.send(alert, "addButtonWithTitle:", rt.string("Quit"))
            app.activate()
            rt.send(alert, "runModal", restype=ctypes.c_long)
        finally:
            rt.send(alert, "release", restype=None)


def _login_items():
    """The start-at-login backend (the LaunchAgent, platform/autostart_mac.py)."""
    from .. import autostart
    return autostart


def start(engine, *, app=None, bar=None, sink=None, autostart_api=None, open_url=None, quit=None) -> MenuBarHandle:
    """Starts the menu bar thread and returns once the item shows the first snapshot. With no `bar` the real
    item is created on `app` (the running `MacApp`, by default the current one), which refuses in safe mode
    and outside the app process. Raises when it cannot start."""
    holder = {}
    if bar is None:
        if config.safe_mode():
            raise SafetyViolation("The menu bar item is not shown in safe mode.")
        app = app or MacApp.current()
        if app is None:
            raise RuntimeError(NO_APP)
        bar = AppKitStatusBar(app)
        if sink is None:
            from . import notify_mac
            sink = notify_mac.make_sink(app, on_open=lambda: holder["menubar"].request("open"))
        if autostart_api is None:
            autostart_api = _login_items()
        open_url = open_url or _window_opener(app)
        quit = quit or app.stop
        app.on_quit = lambda: holder["menubar"].choose(QUIT, {})
    menubar = holder["menubar"] = MenuBar(engine, bar, sink=sink, autostart_api=autostart_api,
                                          open_url=open_url or _refuse_browser, quit=quit or (lambda: None),
                                          open_browser=app.open_url if app is not None else None)
    bar.on_open = lambda: menubar.request("look")      # the main thread only queues it: the menu never waits
    ready = threading.Event()
    thread = threading.Thread(target=menubar.run, args=(ready,), name="ad2-menubar", daemon=True)
    thread.start()
    if not ready.wait(STARTUP_WAIT):
        menubar.request_stop()
        raise TimeoutError("The menu bar item did not start in time.")
    handle = MenuBarHandle(menubar, thread)
    if app is not None:
        app.on_reopen = lambda: menubar.request("open")
    attach = getattr(engine, "attach_tray", None)
    if callable(attach):
        try:
            attach(handle)
        except Exception:
            handle.stop()
            raise
    return handle


def _window_opener(app):
    """open_url for the menu bar: the dashboard in the app's own window (dashboard_window.py), the browser
    when that window cannot be shown, and any other address (System Settings) with its own app."""
    from . import dashboard_window
    window = {}

    def show(url) -> bool:
        if "it" not in window:
            window["it"] = dashboard_window.DashboardWindow(app, app.open_url)
        window["it"].show(url)
        return True

    def open_url(url):
        if dashboard_window.is_dashboard(url) and app.call_wait(lambda: show(url), default=False):
            return
        if dashboard_window.is_dashboard(url):
            diagnostics.log("dashboard.window_unavailable", "warning")
        app.open_url(url)
    return open_url


def _refuse_browser(_url) -> None:
    raise SafetyViolation("The browser is not opened without the app.")
