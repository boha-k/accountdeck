"""The dashboard in a window of the app itself: one NSWindow holding a WKWebView (main thread only).

Opening the dashboard shows this window (made on first use, brought forward later) instead of a browser
tab. While it is open AccountDeck 2 is a regular app, so it has a Dock icon, takes part in ⌘Tab and has an
app menu (⌘W closes the window, ⌘Q quits, Edit shortcuts work in the page); closing it makes AccountDeck 2 a
menu bar app again and frees the web view. The window remembers its size and place (frame autosave name).

The page may only show AccountDeck's own loopback address. Any other navigation, and every link that asks
for a new window (the status pages the banners link to), opens in the default browser instead. If the web
content process dies, the page is loaded again.

Anything that goes wrong here raises, and the caller opens the browser instead (statusbar_mac).
"""
from __future__ import annotations

import ctypes
from urllib.parse import urlsplit

from .appkit import MacApp, NSPoint, NSRect, NSSize, ObjC

WEBKIT = "/System/Library/Frameworks/WebKit.framework/WebKit"
TITLE = "AccountDeck 2"
AUTOSAVE_NAME = "AccountDeck2Dashboard"
DEFAULT_SIZE = NSSize(1180, 820)
MIN_SIZE = NSSize(720, 520)
STYLE = 1 | 2 | 4 | 8                   # titled, closable, miniaturizable, resizable
BACKING_BUFFERED = 2
RESIZE_WIDTH_HEIGHT = 2 | 16            # NSViewWidthSizable | NSViewHeightSizable
FULL_SCREEN_PRIMARY = 1 << 7            # NSWindowCollectionBehaviorFullScreenPrimary
TABBING_DISALLOWED = 2                  # NSWindowTabbingModeDisallowed
POLICY_CANCEL, POLICY_ALLOW = 0, 1      # WKNavigationActionPolicy
DELEGATE_CLASS = "AD2DashboardWebDelegate"
USER_AGENT_NAME = "AccountDeck2App/2"      # appended to the user agent: what the page checks for

_DECIDE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_long)                      # ^(WKNavigationActionPolicy)
_DECIDE_IMP = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p)                                         # v@:@@@?
_CREATE_IMP = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)       # @@:@@@@
_TERMINATED_IMP = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)    # v@:@


def is_dashboard(url: str) -> bool:
    """Whether `url` is AccountDeck's own dashboard (the loopback service), the only page the window shows."""
    parts = urlsplit(url)
    return parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port is not None


def _same_service(url: str, origin: str) -> bool:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}:{parts.port}" == origin or parts.scheme == "about"


class DashboardWindow:
    """See the module docstring. `open_external(url)` opens a URL in the default browser."""

    _current = None

    def __init__(self, app: MacApp, open_external):
        self.app, self.rt = app, app.rt
        self.open_external = open_external
        self.window = self.web = None
        self.origin = None
        ctypes.CDLL(WEBKIT)
        if not self.rt.cls("WKWebView"):
            raise OSError("WebKit is not available.")
        current = lambda: DashboardWindow._current          # noqa: E731
        self.delegate_class = self.rt.define_class(DELEGATE_CLASS, {
            "webView:decidePolicyForNavigationAction:decisionHandler:":
                (_DECIDE_IMP(lambda _s, _c, _web, action, decide: _call(current, "_decide", action, decide)),
                 b"v@:@@@?"),
            "webView:createWebViewWithConfiguration:forNavigationAction:windowFeatures:":
                (_CREATE_IMP(lambda _s, _c, _web, _config, action, _features: _call(current, "_new_window", action)),
                 b"@@:@@@@"),
            "webViewWebContentProcessDidTerminate:":
                (_TERMINATED_IMP(lambda _s, _c, web: _call(current, "_reload", web)), b"v@:@"),
        }, protocols=("WKNavigationDelegate", "WKUIDelegate"))
        self.delegate = self.rt.send(self.rt.send(self.delegate_class, "alloc"), "init")   # kept for the process's life
        DashboardWindow._current = self
        app.on_window_closed = self._closed

    # ---- showing ----
    def show(self, url: str) -> None:
        """Shows the dashboard at `url` (main thread)."""
        if not is_dashboard(url):
            raise ValueError("The dashboard window shows only AccountDeck's own address.")
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.hostname}:{parts.port}"
        if self.window is None:
            self._make_window()
        if self.web is None or origin != self.origin:
            self._make_web_view()
            self.origin = origin
            self._load(url)
        self.app.install_main_menu(TITLE)
        self.app.set_regular(True)
        self.app.activate()
        self.rt.send(self.window, "makeKeyAndOrderFront:", None, restype=None)

    def _make_window(self) -> None:
        rt = self.rt
        frame = NSRect(NSPoint(0, 0), DEFAULT_SIZE)
        window = rt.send(rt.send(rt.cls("NSWindow"), "alloc"), "initWithContentRect:styleMask:backing:defer:", frame,
                         STYLE, BACKING_BUFFERED, False,
                         argtypes=[NSRect, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool])
        if not window:
            raise OSError("The dashboard window could not be made.")
        rt.send(window, "setReleasedWhenClosed:", False, restype=None, argtypes=[ctypes.c_bool])
        rt.send(window, "setTitle:", rt.string(TITLE), restype=None)
        rt.send(window, "setContentMinSize:", MIN_SIZE, restype=None, argtypes=[NSSize])
        rt.send(window, "setCollectionBehavior:", FULL_SCREEN_PRIMARY, restype=None, argtypes=[ctypes.c_ulong])
        rt.send(window, "setTabbingMode:", TABBING_DISALLOWED, restype=None, argtypes=[ctypes.c_long])
        rt.send(window, "setDelegate:", self.app.target, restype=None)
        rt.send(window, "center", restype=None)
        # After centring: a saved frame (from an earlier run) wins.
        rt.send(window, "setFrameAutosaveName:", rt.string(AUTOSAVE_NAME), restype=ctypes.c_bool)
        self.window = window

    def _make_web_view(self) -> None:
        rt = self.rt
        self._drop_web_view()
        configuration = rt.send(rt.send(rt.cls("WKWebViewConfiguration"), "alloc"), "init")
        try:
            # The page can tell it runs in the app's window (no browser notifications there; the menu bar's
            # desktop notifications take their place).
            rt.send(configuration, "setApplicationNameForUserAgent:", rt.string(USER_AGENT_NAME), restype=None)
            content = rt.send(self.window, "contentView")
            bounds = rt.send(content, "bounds", restype=NSRect)
            web = rt.send(rt.send(rt.cls("WKWebView"), "alloc"), "initWithFrame:configuration:", bounds, configuration,
                          argtypes=[NSRect, ctypes.c_void_p])
        finally:
            rt.send(configuration, "release", restype=None)
        if not web:
            raise OSError("The dashboard view could not be made.")
        rt.send(web, "setAutoresizingMask:", RESIZE_WIDTH_HEIGHT, restype=None, argtypes=[ctypes.c_ulong])
        rt.send(web, "setNavigationDelegate:", self.delegate, restype=None)
        rt.send(web, "setUIDelegate:", self.delegate, restype=None)
        rt.send(content, "addSubview:", web, restype=None)
        self.web = web

    def _load(self, url: str) -> None:
        rt = self.rt
        address = rt.url(url)
        if not address:
            raise ValueError("Not a URL.")
        rt.send(self.web, "loadRequest:", rt.send(rt.cls("NSURLRequest"), "requestWithURL:", address), restype=None)

    def _drop_web_view(self) -> None:
        if self.web is not None:
            rt = self.rt
            rt.send(self.web, "stopLoading", restype=None)
            rt.send(self.web, "setNavigationDelegate:", None, restype=None)
            rt.send(self.web, "setUIDelegate:", None, restype=None)
            rt.send(self.web, "removeFromSuperview", restype=None)
            rt.send(self.web, "release", restype=None)
            self.web = None

    def _closed(self, notification) -> None:
        """windowWillClose: back to a menu bar app, and the web content process is let go."""
        if notification and self.window and self.rt.send(notification, "object") != self.window:
            return
        self._drop_web_view()
        self.origin = None
        self.app.set_regular(False)

    # ---- the web view's delegate (main thread) ----
    def _decide(self, action, decide) -> None:
        """Answers the decision handler exactly once (WebKit aborts the process on a second answer)."""
        policy = POLICY_ALLOW
        try:
            url = self._action_url(action)
            if url is not None and self.origin is not None and not _same_service(url, self.origin):
                policy = POLICY_CANCEL
                self.open_external(url)
        except Exception:
            policy = POLICY_CANCEL
        ObjC.call_block(decide, _DECIDE, policy)

    def _new_window(self, action):
        url = self._action_url(action)
        if url:
            self.open_external(url)
        return None                             # never a second web view

    def _reload(self, web) -> None:
        if web and web == self.web:
            self.rt.send(web, "reload", restype=None)

    def _action_url(self, action) -> str | None:
        rt = self.rt
        request = rt.send(action, "request") if action else None
        url = rt.send(request, "URL") if request else None
        return rt.text(rt.send(url, "absoluteString")) if url else None


def _call(current, method, *args):
    """A delegate callback: never lets a Python error reach WebKit. With no window left a navigation is
    refused (its decision handler must still be answered)."""
    window = current()
    try:
        if window is None:
            if method == "_decide":
                ObjC.call_block(args[1], _DECIDE, POLICY_CANCEL)
            return None
        with ObjC.shared().pool():
            return getattr(window, method)(*args)
    except BaseException:
        return None
