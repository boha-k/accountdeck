"""AppKit from Python through ctypes alone (no PyObjC): the Objective-C runtime bridge and the application object.

`ObjC` sends messages with an exact `objc_msgSend` prototype per call signature (on arm64 the variadic
calling convention is wrong for objc_msgSend), creates NSStrings, reads constants, defines classes whose
methods call back into Python, and builds the global blocks that completion handlers need.

`MacApp` owns NSApplication on the process's main thread (`run` blocks there until `stop`). Other threads
hand work to it with `call` (fire and forget) or `call_wait` (for a result, e.g. an alert's answer); both
queue a Python callable and wake the main thread with `performSelectorOnMainThread:`, which also runs while
a menu is open or an alert is modal. Every callback from Objective-C into Python goes through `_guarded`,
so no Python exception can unwind into AppKit, and every Python object handed to Objective-C (callbacks,
blocks) is kept for the life of the process: a collected callback would crash the next click.

Memory: objects this module creates with alloc/init are released by their owner; autoreleased objects are
created inside an autorelease pool (`ObjC.pool()`), which `call` jobs, callbacks and background-thread
sends all use, so nothing piles up in a thread's implicit pool.

Nothing here runs at import: libobjc and AppKit load when the first `ObjC` is made, which only the app
process does (run.py --app, outside safe mode).
"""
from __future__ import annotations

from collections import deque
import ctypes
import itertools
import threading

from .. import diagnostics

LIBOBJC = "/usr/lib/libobjc.A.dylib"
FOUNDATION = "/System/Library/Frameworks/Foundation.framework/Foundation"
APPKIT = "/System/Library/Frameworks/AppKit.framework/AppKit"
ACCESSORY = 1                   # NSApplicationActivationPolicyAccessory: no Dock icon, no app menu
REGULAR = 0                     # NSApplicationActivationPolicyRegular: while a window of ours is open
TICK = 1.0                      # seconds between main-thread ticks (appearance changes, signals)
APPLICATION_DEFINED = 15        # NSEventTypeApplicationDefined
TARGET_CLASS = "AD2AppTarget"


class NSPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class NSSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class NSRect(ctypes.Structure):
    _fields_ = [("origin", NSPoint), ("size", NSSize)]


class _BlockDescriptor(ctypes.Structure):
    _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong)]


class _BlockLiteral(ctypes.Structure):
    _fields_ = [("isa", ctypes.c_void_p), ("flags", ctypes.c_int), ("reserved", ctypes.c_int),
                ("invoke", ctypes.c_void_p), ("descriptor", ctypes.POINTER(_BlockDescriptor))]


BLOCK_IS_GLOBAL = 1 << 28


class ObjC:
    """libobjc through ctypes. One per process (`ObjC.shared()`)."""

    _shared = None
    _lock = threading.Lock()

    @classmethod
    def shared(cls) -> "ObjC":
        with cls._lock:
            if cls._shared is None:
                cls._shared = cls()
            return cls._shared

    def __init__(self):
        self.lib = ctypes.CDLL(LIBOBJC)
        self.foundation = ctypes.CDLL(FOUNDATION)
        self.appkit = ctypes.CDLL(APPKIT)
        for name, restype, argtypes in (
                ("objc_getClass", ctypes.c_void_p, [ctypes.c_char_p]),
                ("sel_registerName", ctypes.c_void_p, [ctypes.c_char_p]),
                ("objc_allocateClassPair", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]),
                ("class_addMethod", ctypes.c_bool, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                    ctypes.c_char_p]),
                ("objc_registerClassPair", None, [ctypes.c_void_p]),
                ("objc_getProtocol", ctypes.c_void_p, [ctypes.c_char_p]),
                ("class_addProtocol", ctypes.c_bool, [ctypes.c_void_p, ctypes.c_void_p]),
                ("objc_autoreleasePoolPush", ctypes.c_void_p, []),
                ("objc_autoreleasePoolPop", None, [ctypes.c_void_p])):
            function = getattr(self.lib, name)
            function.restype, function.argtypes = restype, argtypes
        self._send = ctypes.cast(self.lib.objc_msgSend, ctypes.c_void_p).value
        self._prototypes = {}
        self._selectors = {}
        self.keep = []              # every callback and block handed to Objective-C, for the process's life
        self._global_block = ctypes.c_void_p.in_dll(ctypes.CDLL(None), "_NSConcreteGlobalBlock")

    def send(self, obj, selector: str, *args, restype=ctypes.c_void_p, argtypes=None):
        key = (restype, tuple(argtypes or [ctypes.c_void_p] * len(args)))
        function = self._prototypes.get(key)
        if function is None:
            function = self._prototypes[key] = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p,
                                                               *key[1])(self._send)
        return function(obj, self.sel(selector), *args)

    def sel(self, name: str):
        selector = self._selectors.get(name)
        if selector is None:
            selector = self._selectors[name] = self.lib.sel_registerName(name.encode())
        return selector

    def cls(self, name: str):
        return self.lib.objc_getClass(name.encode())

    def responds(self, obj, selector: str) -> bool:
        return bool(obj) and bool(self.send(obj, "respondsToSelector:", self.sel(selector), restype=ctypes.c_bool))

    def string(self, text: str):
        """An autoreleased NSString (a NUL would end it early, so none is passed)."""
        return self.send(self.cls("NSString"), "stringWithUTF8String:", str(text).replace("\0", "").encode(),
                         argtypes=[ctypes.c_char_p])

    def text(self, nsstring) -> str | None:
        if not nsstring:
            return None
        raw = self.send(nsstring, "UTF8String", restype=ctypes.c_char_p)
        return raw.decode("utf-8", "replace") if raw is not None else None

    def url(self, text: str):
        """An autoreleased NSURL, or None when `text` is not a URL."""
        return self.send(self.cls("NSURL"), "URLWithString:", self.string(text)) or None

    def constant(self, library, name: str):
        return ctypes.c_void_p.in_dll(library, name).value

    def block(self, cfunc):
        """A global block calling `cfunc` (whose first argument is the block). Kept forever."""
        descriptor = _BlockDescriptor(0, ctypes.sizeof(_BlockLiteral))
        block = _BlockLiteral(ctypes.addressof(self._global_block), BLOCK_IS_GLOBAL, 0,
                              ctypes.cast(cfunc, ctypes.c_void_p), ctypes.pointer(descriptor))
        self.keep.append((cfunc, descriptor, block))
        return block

    @staticmethod
    def call_block(block_pointer, prototype, *args):
        """Invokes a block Objective-C handed us (a completion handler), e.g. with the presentation options.
        `prototype` is a CFUNCTYPE whose first argument is the block itself."""
        if not block_pointer:
            return None
        literal = _BlockLiteral.from_address(block_pointer)
        return prototype(literal.invoke)(block_pointer, *args)

    def define_class(self, name: str, methods: dict, *, superclass: str = "NSObject", protocols=()):
        """The class `name` (made once per process): `methods` maps a selector to (CFUNCTYPE callback,
        type encoding). Callbacks are kept forever."""
        existing = self.cls(name)
        if existing:
            return existing
        new = self.lib.objc_allocateClassPair(self.cls(superclass), name.encode(), 0)
        if not new:
            raise OSError(f"The Objective-C class {name} could not be created.")
        for selector, (callback, types) in methods.items():
            self.keep.append(callback)
            self.lib.class_addMethod(new, self.sel(selector), ctypes.cast(callback, ctypes.c_void_p), types)
        for protocol in protocols:
            found = self.lib.objc_getProtocol(protocol.encode())
            if found:
                self.lib.class_addProtocol(new, found)
        self.lib.objc_registerClassPair(new)
        return new

    def pool(self):
        return _Pool(self)


class _Pool:
    def __init__(self, rt):
        self.rt, self.token = rt, None

    def __enter__(self):
        self.token = self.rt.lib.objc_autoreleasePoolPush()

    def __exit__(self, *exc):
        self.rt.lib.objc_autoreleasePoolPop(self.token)


def guarded(owner, method: str, default=None):
    """An Objective-C method body that calls `getattr(owner(), method)(*args)` inside an autorelease pool.
    `owner` is a function returning the current Python object (or None once it is gone). Nothing it raises
    reaches Objective-C: failures are logged by type and `default` is returned."""
    def body(_self, _cmd, *args):
        target = owner()
        if target is None:
            return default
        try:
            with ObjC.shared().pool():
                result = getattr(target, method)(*args)
        except BaseException as exc:
            diagnostics.log("appkit.callback_failed", "warning", method=method, error_type=type(exc).__name__)
            return default
        return default if result is None else result
    return body


_IMP = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)                     # v@:@
_BOOL_IMP = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)       # B@:@
_REOPEN_IMP = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_bool)                                                        # B@:@B
_REPLY_IMP = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)     # Q@:@
TERMINATE_CANCEL, TERMINATE_NOW = 0, 1          # NSApplicationTerminateReply
# Why the system asks the app to quit (the quit Apple event's 'why?' attribute): log out, restart, shut down.
QUIT_REASON = 0x7768793F                        # 'why?'
SYSTEM_QUIT_REASONS = frozenset(int.from_bytes(code, "big") for code in (
    b"logo", b"rlgo", b"rrst", b"rsdn", b"rest", b"shut"))
COMMAND, OPTION, CONTROL = 1 << 20, 1 << 19, 1 << 18     # NSEventModifierFlags


def _current():
    return MacApp._current


class MacApp:
    """NSApplication on the main thread, plus the hand-over of work to it. Build it and `run` it on the main
    thread; `call`, `call_wait` and `stop` are safe from any thread. Only run.py's app mode (which refuses
    safe mode) builds one."""

    _current = None

    def __init__(self, rt: ObjC | None = None):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("AppKit runs on the main thread only.")
        self.rt = rt or ObjC.shared()
        self.lock = threading.Lock()
        self.work = deque()
        self.stopped = self.running = False
        self.tickers = []            # objects whose _tick() runs on every main-thread tick
        self.handlers = {}           # menu item tag -> callable (main thread)
        self.tags = itertools.count(1)
        self.on_reopen = None        # called when the app is opened again (Finder, Spotlight, Dock)
        self.on_window_closed = None
        self.on_quit = None          # the orderly Quit (the menu bar's): ⌘Q, the Dock's Quit, a quit Apple event
        self.on_system_quit = None   # last words before macOS ends the app (log out, restart, shut down)
        self._main_menu = None
        target_class = self.rt.define_class(TARGET_CLASS, {
            "drain:": (_IMP(guarded(_current, "_drain")), b"v@:@"),
            "tick:": (_IMP(guarded(_current, "_tick")), b"v@:@"),
            "onMenu:": (_IMP(guarded(_current, "_on_menu")), b"v@:@"),
            "menuWillOpen:": (_IMP(guarded(_current, "_menu_will_open")), b"v@:@"),
            "menuDidClose:": (_IMP(guarded(_current, "_menu_did_close")), b"v@:@"),
            "applicationShouldHandleReopen:hasVisibleWindows:":
                (_REOPEN_IMP(guarded(_current, "_reopen", False)), b"B@:@B"),
            "applicationShouldTerminateAfterLastWindowClosed:":
                (_BOOL_IMP(guarded(_current, "_keep_running", False)), b"B@:@"),
            "applicationShouldTerminate:": (_REPLY_IMP(guarded(_current, "_should_terminate", TERMINATE_NOW)),
                                            b"Q@:@"),
            "windowWillClose:": (_IMP(guarded(_current, "_window_will_close")), b"v@:@"),
        }, protocols=("NSApplicationDelegate", "NSMenuDelegate", "NSWindowDelegate"))
        rt = self.rt
        with rt.pool():
            self.ns_app = rt.send(rt.cls("NSApplication"), "sharedApplication")
            rt.send(self.ns_app, "setActivationPolicy:", ACCESSORY, restype=ctypes.c_bool, argtypes=[ctypes.c_long])
            self.target = rt.send(rt.send(target_class, "alloc"), "init")
            rt.send(self.ns_app, "setDelegate:", self.target, restype=None)
            rt.send(rt.cls("NSTimer"), "scheduledTimerWithTimeInterval:target:selector:userInfo:repeats:",
                    TICK, self.target, rt.sel("tick:"), None, True,
                    argtypes=[ctypes.c_double, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool])
        MacApp._current = self

    @classmethod
    def current(cls) -> "MacApp | None":
        return cls._current

    @staticmethod
    def on_main() -> bool:
        return threading.current_thread() is threading.main_thread()

    # ---- handing work to the main thread ----
    def call(self, function) -> None:
        """Runs `function` on the main thread: now when already there, else as soon as it is free."""
        if self.on_main():
            with self.rt.pool():
                function()
            return
        with self.lock:
            self.work.append(function)
        self.wake()

    def wake(self) -> None:
        rt = self.rt
        with rt.pool():
            rt.send(self.target, "performSelectorOnMainThread:withObject:waitUntilDone:", rt.sel("drain:"), None,
                    False, restype=None, argtypes=[ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool])

    def call_wait(self, function, default=None):
        """`call` and wait for the result; `default` once the app has stopped."""
        if self.on_main():
            with self.rt.pool():
                return function()
        done, box = threading.Event(), {}

        def job():
            try:
                box["value"] = function()
            finally:
                done.set()
        self.call(job)
        while not done.wait(0.5):
            if self.stopped:
                return default
        return box.get("value", default)

    # ---- lifecycle ----
    def run(self) -> None:
        """The AppKit run loop; returns after `stop`."""
        if self.stopped:
            return
        self.running = True
        try:
            self.rt.send(self.ns_app, "run", restype=None)
        finally:
            self.running = False
            self.stopped = True

    def stop(self) -> None:
        self.call(self._stop_now)

    def _stop_now(self) -> None:
        self.stopped = True
        rt = self.rt
        if rt.send(self.ns_app, "modalWindow"):
            rt.send(self.ns_app, "abortModal", restype=None)     # an open alert would swallow the stop
        rt.send(self.ns_app, "stop:", None, restype=None)
        # stop: is noticed only after an event, so post one (a timer or a queued call is not one).
        event = rt.send(rt.cls("NSEvent"),
                        "otherEventWithType:location:modifierFlags:timestamp:windowNumber:context:subtype:data1:data2:",
                        APPLICATION_DEFINED, NSPoint(0, 0), 0, 0.0, 0, None, 0, 0, 0,
                        argtypes=[ctypes.c_ulong, NSPoint, ctypes.c_ulong, ctypes.c_double, ctypes.c_long,
                                  ctypes.c_void_p, ctypes.c_short, ctypes.c_long, ctypes.c_long])
        if event:
            rt.send(self.ns_app, "postEvent:atStart:", event, True, restype=None,
                    argtypes=[ctypes.c_void_p, ctypes.c_bool])

    # ---- small services (main thread unless noted) ----
    def activate(self) -> None:
        """Brings this (accessory) app forward, so an alert or window opens in front of other apps."""
        rt = self.rt
        if rt.responds(self.ns_app, "activate"):
            rt.send(self.ns_app, "activate", restype=None)          # macOS 14 and later
        else:
            rt.send(self.ns_app, "activateIgnoringOtherApps:", True, restype=None, argtypes=[ctypes.c_bool])

    def set_regular(self, regular: bool) -> None:
        """A Dock icon and app menu while a window of ours is open (⌘Tab finds it), none otherwise."""
        self.rt.send(self.ns_app, "setActivationPolicy:", REGULAR if regular else ACCESSORY, restype=ctypes.c_bool,
                     argtypes=[ctypes.c_long])

    def open_url(self, text: str) -> bool:
        """Opens a URL with its default app (NSWorkspace, no child process; the URL never reaches argv)."""
        def job():
            rt = self.rt
            url = rt.url(text)
            workspace = rt.send(rt.cls("NSWorkspace"), "sharedWorkspace")
            return bool(url) and bool(rt.send(workspace, "openURL:", url, restype=ctypes.c_bool))
        return bool(self.call_wait(job, default=False))

    def bundle_identifier(self) -> str | None:
        """This process's bundle id (None when run outside the app bundle); safe from any thread."""
        rt = self.rt
        with rt.pool():
            bundle = rt.send(rt.cls("NSBundle"), "mainBundle")
            return rt.text(rt.send(bundle, "bundleIdentifier")) if bundle else None

    def install_main_menu(self, app_name: str) -> None:
        """The app menu bar shown while a window of ours is in front: the app menu (About, Hide, Quit ⌘Q), Edit
        (⌘Z ⌘X ⌘C ⌘V ⌘A reach the web view through the responder chain), View (Reload ⌘R, Full Screen) and
        Window (Minimize ⌘M, Close ⌘W). Made once, on the main thread."""
        if self._main_menu is not None:
            return
        rt = self.rt
        bar = rt.send(rt.send(rt.cls("NSMenu"), "alloc"), "initWithTitle:", rt.string(""))
        quit_tag = self.menu_tag(lambda: (self.on_quit or self.stop)())
        sections = (
            (app_name, (("About " + app_name, "orderFrontStandardAboutPanel:", "", 0), None,
                        ("Hide " + app_name, "hide:", "h", COMMAND),
                        ("Hide Others", "hideOtherApplications:", "h", COMMAND | OPTION),
                        ("Show All", "unhideAllApplications:", "", 0), None,
                        ("Quit " + app_name, quit_tag, "q", COMMAND))),
            ("Edit", (("Undo", "undo:", "z", COMMAND), ("Redo", "redo:", "z", COMMAND | 1 << 17), None,
                      ("Cut", "cut:", "x", COMMAND), ("Copy", "copy:", "c", COMMAND), ("Paste", "paste:", "v", COMMAND),
                      ("Select All", "selectAll:", "a", COMMAND))),
            ("View", (("Reload", "reload:", "r", COMMAND), None,
                      ("Enter Full Screen", "toggleFullScreen:", "f", COMMAND | CONTROL))),
            ("Window", (("Minimize", "performMiniaturize:", "m", COMMAND), ("Zoom", "performZoom:", "", 0), None,
                        ("Close", "performClose:", "w", COMMAND))))
        for title, entries in sections:
            menu = rt.send(rt.send(rt.cls("NSMenu"), "alloc"), "initWithTitle:", rt.string(title))
            for entry in entries:
                if entry is None:
                    rt.send(menu, "addItem:", rt.send(rt.cls("NSMenuItem"), "separatorItem"), restype=None)
                    continue
                label, action, key, mask = entry
                ours = isinstance(action, int)
                item = rt.send(rt.send(rt.cls("NSMenuItem"), "alloc"), "initWithTitle:action:keyEquivalent:",
                               rt.string(label), rt.sel("onMenu:" if ours else action), rt.string(key))
                if ours:
                    rt.send(item, "setTarget:", self.target, restype=None)
                    rt.send(item, "setTag:", action, restype=None, argtypes=[ctypes.c_long])
                if key:
                    rt.send(item, "setKeyEquivalentModifierMask:", mask, restype=None, argtypes=[ctypes.c_ulong])
                rt.send(menu, "addItem:", item, restype=None)
                rt.send(item, "release", restype=None)
            holder = rt.send(rt.send(rt.cls("NSMenuItem"), "alloc"), "initWithTitle:action:keyEquivalent:",
                             rt.string(title), None, rt.string(""))
            rt.send(holder, "setSubmenu:", menu, restype=None)
            rt.send(bar, "addItem:", holder, restype=None)
            if title == "Window":
                rt.send(self.ns_app, "setWindowsMenu:", menu, restype=None)
            rt.send(holder, "release", restype=None)
            rt.send(menu, "release", restype=None)
        rt.send(self.ns_app, "setMainMenu:", bar, restype=None)
        self._main_menu = bar

    def menu_tag(self, handler) -> int:
        tag = next(self.tags)
        self.handlers[tag] = handler
        return tag

    # ---- main-thread callbacks (through the target class) ----
    def _drain(self, _arg) -> None:
        while True:
            with self.lock:
                if not self.work:
                    return
                function = self.work.popleft()
            try:
                with self.rt.pool():
                    function()
            except Exception as exc:
                diagnostics.log("appkit.main_failed", "warning", error_type=type(exc).__name__)

    def _tick(self, _arg) -> None:
        if self.stopped and self.running:
            self._stop_now()                 # a stop that reached a nested run loop: ask again
            return
        for ticker in list(self.tickers):
            ticker._tick()
        self._drain(None)

    def _on_menu(self, sender) -> None:
        handler = self.handlers.get(self.rt.send(sender, "tag", restype=ctypes.c_long))
        if handler is not None:
            handler()

    def _menu_will_open(self, menu) -> None:
        for ticker in list(self.tickers):
            getattr(ticker, "_menu_open", lambda *_: None)(menu, True)

    def _menu_did_close(self, menu) -> None:
        for ticker in list(self.tickers):
            getattr(ticker, "_menu_open", lambda *_: None)(menu, False)

    def _reopen(self, _app, _visible) -> bool:
        if self.on_reopen is not None:
            self.on_reopen()
        return False

    def _should_terminate(self, _app) -> int:
        """A quit from outside (the Dock's Quit, ⌘Q, Activity Monitor) goes through the orderly Quit, which
        refuses while an action runs. Logging out, restarting and shutting down are never held up: macOS ends
        the process right after `on_system_quit` (the service's state is crash-safe by design)."""
        if self._system_quit():
            if self.on_system_quit is not None:
                self.on_system_quit()
            return TERMINATE_NOW
        (self.on_quit or self.stop)()
        return TERMINATE_CANCEL

    def _system_quit(self) -> bool:
        rt = self.rt
        event = rt.send(rt.send(rt.cls("NSAppleEventManager"), "sharedAppleEventManager"), "currentAppleEvent")
        if not event:
            return False
        reason = rt.send(event, "attributeDescriptorForKeyword:", QUIT_REASON, argtypes=[ctypes.c_uint32])
        code = rt.send(reason, "enumCodeValue", restype=ctypes.c_uint32) if reason else 0
        return code in SYSTEM_QUIT_REASONS

    def _keep_running(self, _app) -> bool:
        return False                 # closing the dashboard window never quits: the menu bar item stays

    def _window_will_close(self, notification) -> None:
        if self.on_window_closed is not None:
            self.on_window_closed(notification)
