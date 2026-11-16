"""Desktop notifications: the menu bar item's `desktop` sink, through UNUserNotificationCenter (PLATFORM-MAC.md §3).

When it is delivered is `notify.Notifier`'s policy; this module only shows a delivery and tracks whether
macOS lets AccountDeck 2 notify. Banners carry AccountDeck 2's name and icon; the request identifier is the
delivery key, so a newer delivery for the same alert replaces the one shown; a click on a banner opens the
dashboard (`UNUserNotificationCenterDelegate`).

Permission. The current settings are read first (`getNotificationSettings`, which never asks the owner);
only when macOS has not asked yet is authorization requested, which shows the system prompt once.
`state()` is then one of:

    pending          the answer is not in yet (the prompt can wait on the owner for a while)
    not_determined   macOS has not asked (only before the first request)
    authorized / provisional
    denied           the user switched AccountDeck's notifications off (System Settings › Notifications)
    rejected         macOS refused this copy's identity (UNErrorDomain code 1 while still "not determined"):
                     the app bundle's executable must be signed with the bundle id (install.sh does that)

The state is re-read every RECHECK seconds, so a change made in System Settings shows up in the menu.

The center exists only in a process with the app bundle's identity: without one,
`currentNotificationCenter` aborts the whole process (bundleProxyForCurrentProcess is nil), so `make_sink`
checks `NSBundle.mainBundle.bundleIdentifier` first and gives no sink at all without it. Errors are logged
by domain and code (never a message or the notification text), once per kind.
"""
from __future__ import annotations

import ctypes
import threading
import time

from .. import diagnostics
from .appkit import ObjC

USER_NOTIFICATIONS_FRAMEWORK = "/System/Library/Frameworks/UserNotifications.framework/UserNotifications"
OPTION_SOUND, OPTION_ALERT = 1 << 1, 1 << 2                    # UNAuthorizationOptions
PRESENT_SOUND, PRESENT_LIST, PRESENT_BANNER = 1 << 1, 1 << 3, 1 << 4   # UNNotificationPresentationOptions
STATUS = {0: "not_determined", 1: "denied", 2: "authorized", 3: "provisional", 4: "authorized"}
UN_ERROR_DOMAIN = "UNErrorDomain"
NOT_ALLOWED = 1                     # UNErrorCodeNotificationsNotAllowed
RECHECK = 30.0                      # seconds between re-reads of the notification settings
DELIVERABLE = frozenset({"authorized", "provisional"})
DELEGATE_CLASS = "AD2NotificationDelegate"

_SETTINGS = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)                      # ^(UNNotificationSettings *)
_AUTH = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p)          # ^(BOOL, NSError *)
_ADDED = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)                         # ^(NSError *)
_PRESENT_DONE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong)                   # ^(UNNotificationPresentationOptions)
_RESPONSE_DONE = ctypes.CFUNCTYPE(None, ctypes.c_void_p)                                  # ^(void)
_DELEGATE_IMP = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p)                                          # v@:@@@?


def _error_fields(rt, error) -> tuple[str | None, int | None]:
    """An NSError's domain and code (both safe to log), or (None, None) for no error."""
    if not error:
        return None, None
    with rt.pool():
        return rt.text(rt.send(error, "domain")), int(rt.send(error, "code", restype=ctypes.c_long))


class UNCenter:
    """UNUserNotificationCenter through ctypes. Build it only in a process with a bundle identity. Completion
    handlers run on a background queue: they only call back into Python, never into AppKit."""

    def __init__(self, rt: ObjC):
        self.rt = rt
        ctypes.CDLL(USER_NOTIFICATIONS_FRAMEWORK)
        if not rt.cls("UNUserNotificationCenter"):
            raise OSError("UserNotifications is not available.")
        with rt.pool():
            self.center = rt.send(rt.cls("UNUserNotificationCenter"), "currentNotificationCenter")
            if not self.center:
                raise OSError("No notification center for this process.")
            rt.send(self.center, "retain")
        self.on_settings = self.on_authorized = self.on_added = self.on_open = None
        # One block of each kind for the process's life (global blocks; nothing frees them).
        self._settings_block = rt.block(_SETTINGS(self._settings))
        self._auth_block = rt.block(_AUTH(self._authorized))
        self._add_block = rt.block(_ADDED(self._added))
        self._delegate = None

    # -- completion handlers (background queue) --
    def _settings(self, _block, settings) -> None:
        status = int(self.rt.send(settings, "authorizationStatus", restype=ctypes.c_long)) if settings else -1
        if self.on_settings is not None:
            self.on_settings(status)

    def _authorized(self, _block, granted, error) -> None:
        if self.on_authorized is not None:
            self.on_authorized(bool(granted), *_error_fields(self.rt, error))

    def _added(self, _block, error) -> None:
        if self.on_added is not None:
            self.on_added(*_error_fields(self.rt, error))

    # -- requests --
    def read_settings(self) -> None:
        rt = self.rt
        with rt.pool():
            rt.send(self.center, "getNotificationSettingsWithCompletionHandler:", ctypes.byref(self._settings_block),
                    restype=None, argtypes=[ctypes.c_void_p])

    def request_authorization(self) -> None:
        rt = self.rt
        with rt.pool():
            rt.send(self.center, "requestAuthorizationWithOptions:completionHandler:", OPTION_ALERT | OPTION_SOUND,
                    ctypes.byref(self._auth_block), restype=None, argtypes=[ctypes.c_ulong, ctypes.c_void_p])

    def add(self, key: str, title: str, body: str, *, thread: str, sound: bool) -> None:
        rt = self.rt
        with rt.pool():
            content = rt.send(rt.send(rt.cls("UNMutableNotificationContent"), "alloc"), "init")
            try:
                rt.send(content, "setTitle:", rt.string(title), restype=None)
                rt.send(content, "setBody:", rt.string(body), restype=None)
                rt.send(content, "setThreadIdentifier:", rt.string(thread), restype=None)
                if sound:
                    rt.send(content, "setSound:", rt.send(rt.cls("UNNotificationSound"), "defaultSound"), restype=None)
                request = rt.send(rt.cls("UNNotificationRequest"), "requestWithIdentifier:content:trigger:",
                                  rt.string(key), content, None)
                rt.send(self.center, "addNotificationRequest:withCompletionHandler:", request,
                        ctypes.byref(self._add_block), restype=None, argtypes=[ctypes.c_void_p, ctypes.c_void_p])
            finally:
                rt.send(content, "release", restype=None)

    def set_delegate(self) -> None:
        """Shows banners even while AccountDeck 2 is the active app, and opens the dashboard (`on_open`) when
        the owner clicks one."""
        rt = self.rt
        center = self
        delegate_class = rt.define_class(DELEGATE_CLASS, {
            "userNotificationCenter:willPresentNotification:withCompletionHandler:":
                (_DELEGATE_IMP(lambda _s, _c, _center, _note, done: center._present(done)), b"v@:@@@?"),
            "userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:":
                (_DELEGATE_IMP(lambda _s, _c, _center, _response, done: center._clicked(done)), b"v@:@@@?"),
        }, protocols=("UNUserNotificationCenterDelegate",))
        with rt.pool():
            self._delegate = rt.send(rt.send(delegate_class, "alloc"), "init")     # kept for the process's life
            rt.send(self.center, "setDelegate:", self._delegate, restype=None)

    def _present(self, done) -> None:
        try:
            ObjC.call_block(done, _PRESENT_DONE, PRESENT_BANNER | PRESENT_LIST | PRESENT_SOUND)
        except Exception as exc:
            diagnostics.log("notify.delegate_failed", "warning", error_type=type(exc).__name__)

    def _clicked(self, done) -> None:
        try:
            if self.on_open is not None:
                self.on_open()
        except Exception as exc:
            diagnostics.log("notify.delegate_failed", "warning", error_type=type(exc).__name__)
        finally:
            ObjC.call_block(done, _RESPONSE_DONE)


class UserNotificationSink:
    """The `desktop` sink over a center with read_settings(), request_authorization(), add(...) and the
    on_settings / on_authorized / on_added callbacks (`UNCenter` for real, a fake in tests)."""

    def __init__(self, center, *, clock=time.monotonic):
        self.center, self.clock = center, clock
        self.lock = threading.Lock()
        self._state = "pending"
        self._requested = False
        self._checked_at = None
        self.logged = set()
        center.on_settings, center.on_authorized, center.on_added = self._settings, self._authorized, self._added
        self._recheck()

    # -- what macOS says --
    def state(self) -> str:
        if self._checked_at is None or self.clock() - self._checked_at >= RECHECK:
            self._recheck()
        with self.lock:
            return self._state

    def accepting(self) -> bool:
        return self.state() in DELIVERABLE

    def _recheck(self) -> None:
        self._checked_at = self.clock()
        try:
            self.center.read_settings()
        except Exception as exc:
            self._failed("notify.settings_failed", type(exc).__name__)

    def _settings(self, status: int) -> None:
        state = STATUS.get(status)
        if state is None:
            self._failed("notify.settings_failed", f"status {status}")
            return
        with self.lock:
            if state == "not_determined" and self._requested and self._state in ("pending", "rejected"):
                return                    # still "not asked": the prompt is up, or the rejection stands
            changed, self._state = state != self._state, state
            ask = state == "not_determined" and not self._requested
            if ask:
                self._requested, self._state = True, "pending"
        if changed:
            diagnostics.log("notify.state", state=state)
        if ask:
            try:
                self.center.request_authorization()
            except Exception as exc:
                self._failed("notify.authorization_failed", type(exc).__name__)

    def _authorized(self, granted: bool, domain: str | None, code: int | None) -> None:
        if granted:
            state = "authorized"
        elif domain == UN_ERROR_DOMAIN and code == NOT_ALLOWED:
            state = "rejected"            # refused before any prompt: the identity, not the owner
        else:
            state = "denied"
        with self.lock:
            self._state = state
        diagnostics.log("notify.authorization", state=state, domain=domain, code=code)
        if state != "rejected":
            self._checked_at = None       # read the settings again on the next state()

    def _added(self, domain: str | None, code: int | None) -> None:
        if domain is not None:
            self._failed("notify.not_delivered", f"{domain} {code}")

    # -- delivering --
    def show(self, title: str, message: str, key: str, level: str = "info") -> None:
        self.center.add(key or "accountdeck2", title, message or title, thread=(key or "alert").split("|", 1)[0],
                        sound=level != "info")

    def _failed(self, event: str, reason: str) -> None:
        if (event, reason) not in self.logged:
            self.logged.add((event, reason))
            diagnostics.log(event, "warning", reason=reason)


def make_sink(app, *, on_open=None):
    """The `desktop` sink for this process (`app`: the running appkit.MacApp), or None when macOS cannot
    notify from it (no bundle identity: run outside AccountDeck 2.app). Never raises."""
    try:
        bundle = app.bundle_identifier()
    except Exception as exc:
        diagnostics.log("notify.unavailable", "warning", error_type=type(exc).__name__)
        return None
    if not bundle:
        diagnostics.log("notify.unavailable", reason="no bundle identity")
        return None
    try:
        center = UNCenter(app.rt)
        center.on_open = on_open
        center.set_delegate()
        sink = UserNotificationSink(center)
    except Exception as exc:
        diagnostics.log("notify.unavailable", "warning", error_type=type(exc).__name__)
        return None
    diagnostics.log("notify.backend", backend="user_notifications", bundle=bundle)
    return sink
