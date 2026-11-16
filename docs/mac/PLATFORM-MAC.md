# macOS platform facts for the AccountDeck 2 port

> Design notes of the Mac port as of October 2026; the code and [docs/GUIDE.md](../GUIDE.md) are current
> where they differ. AccountDeck 2 is now Mac-only: the Windows build, the PC export and
> AccountDeck 1 support were removed since (see CONTRACT-ADDITIONS.md). Section numbers stay as they are:
> code comments cite them.

Status: complete for the eight topics below. Research date 2026-10-07. Probes ran on the test Mac in `/tmp/accountdeck-sk6tcC` (deleted afterwards) with the pinned Python 3.14.8.

## Summary

| # | Topic | Recommendation | Status |
|---|---|---|---|
| 1 | Processes | libproc (`proc_listallpids`, `proc_pidinfo` BSDINFO for µs start time, `proc_pidpath`) + `sysctl KERN_PROCARGS2` via ctypes. `ctime` converted to FILETIME units. kqueue `NOTE_EXIT` as the "handle" for verify-then-signal and `wait_exit`. Graceful quit = osascript quit by bundle id, or SIGTERM to the Electron main; relaunch with `open -b` / `open -g -b`. | [mac]; quit/relaunch of a real app [owner] |
| 2 | Menu bar | One `NSStatusItem` via ctypes into libobjc/AppKit, AppKit on the main thread of the app-bundle process, service on background threads. Title = totals, tooltip = hover detail, menu = tray menu. | plumbing [mac]; visible UI [owner] |
| 3 | Notifications | `UNUserNotificationCenter` via ctypes (needs the bundle identity from section 5), fallback `osascript display notification` (attributed to Script Editor, exit code 0 even on failure). | plumbing [mac]; delivery and prompt [owner] |
| 4 | Start at login | User LaunchAgent plist + `launchctl bootstrap/bootout gui/$UID`; reversible from code. | plist linted [mac]; bootstrap [owner] |
| 5 | App bundle | `~/Applications/AccountDeck 2.app`: sh launcher that execs an in-bundle symlink to the runtime's `python3.14` (this is what gives Python the bundle id), `LSUIElement`, .icns from `make_icon.render` + `iconutil`; curl downloads carry no quarantine. | [mac]; Finder launch [owner] |
| 6 | Pinned runtime | CPython 3.14.8 `20261003` install_only, cryptography 50.0.1 arm64 wheel. **50.0.1 has no Intel macOS wheel** (48.0.1 universal2 is the nearest), and **cryptography needs cffi at runtime**: an extra pinned wheel is needed (owner decision). | downloads + checksums [mac] |
| 7 | Paths | `~/Library/Application Support/AccountDeck2` (0700), `~/Library/Logs/AccountDeck2`, `fcntl.flock` (not `lockf`), `os.replace` + `F_FULLFSYNC`, loopback only, `webbrowser.open` (URL via stdin, not argv). Not under `~/Documents`. | [mac] |
| 8 | Keychain | One vault key per data folder as a base64url generic password, written with `security -i` (stdin), read with `find-generic-password -w`; tests on a `create-keychain` file. | [mac] (temporary keychain) |

### Owner checks on a real Mac with a logged-in desktop

1. Menu bar item appears, updates live, menu clicks work, in light and dark mode, with a notch (section 2).
2. `UNUserNotificationCenter` permission prompt and banner for the ad-hoc bundle; if refused, try the self-contained signed layout (sections 3, 5).
3. `launchctl bootstrap gui/$UID` of the LaunchAgent, the "Background Items Added" notice and the Login Items entry name; disabling in Settings → bootstrap error (section 4).
4. Opening `AccountDeck 2.app` from Finder/Spotlight; reopen while running (section 5).
5. Claude Desktop: bundle id; whether `osascript … quit` raises an Automation prompt; behaviour with a dialog open; whether SIGTERM runs its quit cleanup (section 1).
6. Keychain access dialog text and partition-list password prompt for other apps' items (section 8).
7. cffi wheel (once approved): `pip install --no-index --no-deps` cffi + cryptography, then the AESGCM self-test (section 6).

Verification labels:

- **[mac]** run and observed on the test Mac (macOS 26.6 arm64, SSH session, no GUI login on that session), inside a `mktemp -d /tmp/accountdeck-XXXXXX` folder that was deleted afterwards
- **[mac-partial]** the non-GUI part ran on the test Mac; the visible part needs a logged-in desktop
- **[doc]** documented behaviour (man page or Apple docs), not run
- **[owner]** needs an owner check on a real Mac with a logged-in desktop

## 1. Processes (stdlib only)

### How the Windows side is shaped (what the Mac must match)

`procs.py` exposes `ProcessInfo(pid, parent, name, path, ctime, family, cmdline, kind)` and a `ProcessController` ABC. `LiveProcessController` (Windows) builds `snapshot()` from Toolhelp32, keys every row by `(pid, ctime)` (creation FILETIME, 100 ns units), and every mutating call (`request_close`, `terminate`, `stop_consumer`) goes through `_interlock` (consent token covers the pair, safe-mode ownership) and `_verified_handle` (opens the PID, re-reads ctime, image path and package family, refuses `ctime_mismatch` / `path_mismatch` / `family_mismatch`). `wait_exit(targets, timeout)` waits on handles and returns the survivors. `stop_ladder.py` uses only that interface: T1a `request_close` (one WM_CLOSE), T2 `terminate` main, wait for helpers to self-exit, `terminate` stragglers, `wait_exit`, then settle. On Windows a handle pins the process, so "verify then kill" has no PID-reuse window. On macOS there is no pidfd/handle; the equivalent pin is a **kqueue `EVFILT_PROC` watch**, below.

Mapping for a `MacProcessController` (same ABC):

| ABC member | macOS technique |
|---|---|
| `snapshot()` | `proc_listallpids` + `proc_pidinfo(PROC_PIDTBSDINFO)` + `proc_pidpath` + `KERN_PROCARGS2` |
| `ctime` | `pbi_start_tvsec/usec` converted to FILETIME units so the field stays an int with the same meaning: `sec * 10_000_000 + usec * 10 + 116444736000000000` |
| `family` | no package family on macOS. Use the **bundle id** of the enclosing `.app` (read `Contents/Info.plist` `CFBundleIdentifier` with `plistlib`) or `None` |
| `cmdline` | `KERN_PROCARGS2` argv (a list; join only for display) |
| `request_close` | `osascript … tell application id "<bundle id>" to quit`, or SIGTERM to the Electron main process (see below) |
| `terminate` / `stop_consumer` | kqueue watch, re-verify, `os.kill(pid, SIGTERM)`; SIGKILL only as the straggler step |
| `wait_exit` | `kqueue.control(None, n, timeout)` collecting `NOTE_EXIT` |
| `io_delta` | `proc_pid_rusage(pid, RUSAGE_INFO_V2)` → `ri_diskio_bytesread + ri_diskio_byteswritten` |
| `main_window`, `app_user_model_id`, `start_apps_aumid` | no equivalent; the bundle id replaces the AUMID |
| `launch_claude` | `open -b <bundle id>` (`-g` for background) |

### Recommendation: libproc + sysctl via ctypes, not `ps` **[mac]**

Measured on the test Mac with the pinned Python 3.14.8 (`ctypes.CDLL(None)` resolves libSystem, where libproc and sysctl live):

- `proc_listallpids` listed 801 PIDs in 0.1 ms; a full scan of all of them (bsdinfo + procargs) took 19 ms.
- `proc_pidinfo(PROC_PIDTBSDINFO)` (struct is 136 bytes) returns pid, ppid, uid, comm and the **start time in microseconds**, but **only for the caller's own processes** (8 of 801 over SSH). For other users' processes (and root's) it fails with `EPERM`; `PROC_PIDT_SHORTBSDINFO` still gives pid/ppid/uid/comm but no start time. Claude Desktop, Claude Code and Codex run as the same user, so this is enough; treat any row without a start time as unknown (fail closed, as on Windows).
- `proc_pidpath` returned the executable path even for PID 1 (`/sbin/launchd`). It returns the **resolved** path (`/private/tmp/...`), while `KERN_PROCARGS2`'s exec path is the path **as invoked** (`/tmp/...`). Compare paths with `os.path.realpath` on both sides, or always use `proc_pidpath`.
- `KERN_PROCARGS2` gave the exact argv for own processes, including an argument with a space, a non-ASCII argument (`ünï`) and an empty argument. For other users' processes it fails with `EINVAL` (22).
- `ps -axww -o pid=,ppid=,lstart=,comm=,args=`: start time has **1-second resolution** (unusable for PID-reuse checks), `comm` is truncated, `args` is space-joined (ambiguous), and non-ASCII came out mangled (`M-CM-<nM-CM-/`); the empty argument vanished. Use `ps` only as a debugging aid.

```python
import ctypes, os, struct
libc = ctypes.CDLL(None, use_errno=True)

class proc_bsdinfo(ctypes.Structure):          # <sys/proc_info.h>, 136 bytes
    _fields_ = [(n, ctypes.c_uint32) for n in ("pbi_flags", "pbi_status", "pbi_xstatus", "pbi_pid", "pbi_ppid",
                "pbi_uid", "pbi_gid", "pbi_ruid", "pbi_rgid", "pbi_svuid", "pbi_svgid", "rfu_1")] + [
                ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32)] + [
                (n, ctypes.c_uint32) for n in ("pbi_nfiles", "pbi_pgid", "pbi_pjobc", "e_tdev", "e_tpgid")] + [
                ("pbi_nice", ctypes.c_int32), ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64)]

libc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
libc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]

def list_pids():
    n = libc.proc_listallpids(None, 0)
    buf = (ctypes.c_int * (n + 64))()
    return [p for p in buf[:libc.proc_listallpids(buf, ctypes.sizeof(buf))] if p > 0]

def bsdinfo(pid):                               # None: gone, or not ours (EPERM)
    info = proc_bsdinfo()
    ok = libc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) == ctypes.sizeof(info)  # PROC_PIDTBSDINFO
    return info if ok else None

def ctime(info):                                # same unit as Windows FILETIME
    return info.pbi_start_tvsec * 10_000_000 + info.pbi_start_tvusec * 10 + 116444736000000000

def exe_path(pid):
    buf = ctypes.create_string_buffer(4096)     # PROC_PIDPATHINFO_MAXSIZE
    n = libc.proc_pidpath(pid, buf, 4096)
    return os.fsdecode(buf.raw[:n]) if n > 0 else None

def argv(pid):                                  # KERN_PROCARGS2: int argc, exec_path\0, \0 padding, argv..., env...
    mib = (ctypes.c_int * 2)(1, 8); argmax = ctypes.c_int(); size = ctypes.c_size_t(4)   # CTL_KERN, KERN_ARGMAX
    libc.sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0)
    mib = (ctypes.c_int * 3)(1, 49, pid); buf = ctypes.create_string_buffer(argmax.value)  # KERN_PROCARGS2
    size = ctypes.c_size_t(argmax.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    raw = buf.raw[:size.value]
    argc = struct.unpack_from("i", raw)[0]
    _exec, _, rest = raw[4:].partition(b"\0")
    return [os.fsdecode(a) for a in rest.lstrip(b"\0").split(b"\0")[:argc]]
```

The environment block follows argv in `KERN_PROCARGS2`. Never read past `argc`: the environment can hold secrets.

### Signalling a verified PID safely **[mac]**

macOS has no process handle, but a kqueue `EVFILT_PROC`/`NOTE_EXIT` registration attaches to the live process, not the number. The safe order:

1. Register the watch (`KQ_EV_ADD | KQ_EV_ONESHOT`, `KQ_NOTE_EXIT`). If the PID is already gone, registration raises `OSError(ESRCH)` (observed).
2. Re-read the start time (and `proc_pidpath`) and compare with the consented `(pid, ctime)`. A mismatch means PID reuse: refuse.
3. Poll the kqueue with timeout 0. No event means the verified process is still the one at that PID.
4. `os.kill(pid, SIGTERM)`. The window between steps 3 and 4 is a few microseconds and would need the process to exit, be reaped and the PID recycled in between. macOS allocates PIDs sequentially up to 99999, so this is not a practical risk.
5. `wait_exit`: `kq.control(None, len(targets), timeout)` returns one `NOTE_EXIT` event per exited process (`fflags 0x80000000`), and works for processes that are **not** our children. Observed: SIGTERM to `sleep`, exit event within 0.2 ms; a process that ignores SIGTERM produced no event in 1 s, then exited on SIGKILL with an event.

```python
import os, select, signal, time
kq = select.kqueue()
exited: set[int] = set()                 # every NOTE_EXIT seen, so no poll loses another target's event
watched: set[int] = set()

def _drain(timeout):
    for ev in kq.control(None, 64, timeout):
        exited.add(ev.ident)

def watch(pid):     # raises ProcessLookupError (ESRCH) when the pid is already gone
    if pid not in watched:
        kq.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                  select.KQ_NOTE_EXIT)], 0, 0)
        watched.add(pid)

def terminate_verified(pid, expected_ctime, sig=signal.SIGTERM):
    try:
        watch(pid)
    except ProcessLookupError:
        return False                     # already exited
    info = bsdinfo(pid)
    if info is None or ctime(info) != expected_ctime:
        raise RuntimeError("ctime_mismatch")      # map to procs._refusal(...)
    _drain(0)
    if pid in exited:
        return False                     # exited while we checked
    os.kill(pid, sig)
    return True

def wait_exit(pids, timeout):            # pids must already be watched; returns the ones still running
    deadline = time.monotonic() + timeout
    while set(pids) - exited and (remaining := deadline - time.monotonic()) > 0:
        _drain(remaining)
    return set(pids) - exited
```

Other observed facts: `os.kill(dead_pid, 0)` raises `ProcessLookupError`; `os.kill(1, 0)` raises `PermissionError` (exists, not ours). Signals to processes of other users fail with `PermissionError`, so AccountDeck can never signal a root or other-user process even by mistake. `proc_pid_rusage(pid, 2, …)` (`rusage_info_v2`) gives `ri_diskio_bytesread/written` for own processes and `EPERM` for others: a drop-in for `io_delta`.

### Graceful quit and relaunch by bundle id

```sh
osascript -e 'tell application id "com.anthropic.claudefordesktop" to quit'   # graceful: runs the app's quit path
open -b com.anthropic.claudefordesktop        # launch or activate
open -g -b com.anthropic.claudefordesktop     # launch without bringing it to the front
open -g -j -b <id>                            # also hidden (-j); -n forces a new instance (never use for Claude)
```

(The Claude bundle id above is a placeholder, to be confirmed in PROVIDERS-MAC.md; read it from `/Applications/Claude.app/Contents/Info.plist`.)

Observed on the test Mac **[mac]**: `open -g -b <missing id>` exits 1. `osascript` with a missing bundle id prints `Can't get application id "…" (-1728)` and exits 1. So both report errors through the exit code. No real app was quit or opened (rules), so the following is **[doc]/[owner]**:

- **Apple Events need consent.** Since macOS 10.14, sending Apple Events to another app needs Automation consent (TCC). The first `tell application id … to quit` from a new sender shows "“AccountDeck 2” wants access to control “Claude”" (the responsible process is the app bundle that launched osascript, or Terminal). If the user declines, osascript fails with `-1743` (errAEEventNotPermitted) and will keep failing until changed in System Settings › Privacy & Security › Automation. Treat `-1743` as "graceful quit unavailable" and fall back to T2/T3. Check on a real Mac whether `quit` alone triggers the prompt.
- **A dialog blocks the quit.** If the app shows an unsaved-work or "are you sure" sheet, or vetoes in `applicationShouldTerminate`, the quit event is accepted, the app stays running, and osascript returns after its default 2-minute Apple Event timeout, or immediately with no error if the app replied "cancelled". Never trust the exit code. Always follow with `wait_exit` on the verified PIDs (this matches T1a's "if still running after `graceful_wait`, continue with T2"). Use `with timeout of 10 seconds` inside the script, or run osascript with a subprocess timeout.
- **SIGTERM is a graceful quit for Electron [inferred from Electron source].** Electron's browser process installs SIGTERM/SIGINT/SIGHUP handlers that call `app.quit()` (`shell/browser/electron_browser_main_parts_posix.cc`), so `before-quit`/`will-quit` run. That makes SIGTERM to the verified main PID an Apple-Event-free T1a, with no TCC prompt. Verify on a real Mac with Claude Desktop that its quit cleanup markers appear in its log after SIGTERM.
- `open -b` is handled by LaunchServices, so the new app's parent is `launchd` (PID 1), not AccountDeck. It needs a GUI login session (over SSH as a user without one it cannot show anything).

Limits: no process handle, so the kqueue pin is the substitute. Zombies only matter for our own children (reap them with `Popen.wait`). Apps launched by `open` are never our children.

## 2. Menu bar item

### What it replaces

`tray.py` is one thread (`ad2-tray`) owning a hidden window, the icon (a drawn ring), tooltip, a menu built from the cached snapshot every 5 s, quick switch with a consent MessageBox, and balloons. `taskbar_widget.py` is a borderless layered popup showing two lines of totals (`5h ~4.2 ▬▬▬▭` / `wk 5.6 ▬▬▬▬`) with hover detail and click-to-open-dashboard. Every OS call goes through one seam object (`Win32`, `Win32Widget`) that tests replace. On macOS both collapse into **one `NSStatusItem`**: its title carries the totals (the widget's job), its image the ring, its tooltip the hover detail, its menu the tray menu.

### Recommendation: ctypes into libobjc + AppKit, in a dedicated menu-bar process **[mac-partial]**

Proved over SSH with the pinned Python 3.14.8 and no PyObjC (`menubar.py` probe, condensed below): `NSApplication` with the accessory activation policy, `NSStatusBar.systemStatusBar` → `statusItemWithLength:-1` (variable length), button title and tooltip, an `NSMenu` whose items call into Python through a runtime-registered Objective-C class (`objc_allocateClassPair` + `class_addMethod` with a `CFUNCTYPE` IMP), a title update from a **background thread** via `performSelectorOnMainThread:withObject:waitUntilDone:`, a programmatic menu click (`performActionForItemAtIndex:`), then a clean `terminate:` (exit 0). All objects were created and the run loop ran, even though this SSH session has no WindowServer GUI (`launchctl managername` = `Background`). **Not confirmed:** that the item is visible and clickable in a real menu bar, its look in light/dark mode, and placement behind the notch. **[owner]**

```python
import ctypes
objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
for f, r, a in (("objc_getClass", ctypes.c_void_p, [ctypes.c_char_p]), ("sel_registerName", ctypes.c_void_p, [ctypes.c_char_p]),
                ("objc_allocateClassPair", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]),
                ("class_addMethod", ctypes.c_bool, [ctypes.c_void_p] * 3 + [ctypes.c_char_p]),
                ("objc_registerClassPair", None, [ctypes.c_void_p])):
    getattr(objc, f).restype, getattr(objc, f).argtypes = r, a
_SEND, _protos = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value, {}

def send(obj, sel, *args, restype=ctypes.c_void_p, argtypes=None):
    """objc_msgSend with an exact prototype per call (on arm64 the variadic convention is wrong)."""
    key = (restype, tuple(argtypes or [ctypes.c_void_p] * len(args)))
    if key not in _protos:
        _protos[key] = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *key[1])(_SEND)
    return _protos[key](obj, objc.sel_registerName(sel.encode()), *args)

cls = lambda n: objc.objc_getClass(n.encode())
nsstr = lambda t: send(cls("NSString"), "stringWithUTF8String:", t.encode(), argtypes=[ctypes.c_char_p])

HANDLERS = {}                                         # menu tag -> Python callable
IMP = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)   # "v@:@"
on_menu = IMP(lambda self, cmd, sender: HANDLERS[send(sender, "tag", restype=ctypes.c_long)]())  # keep a reference!
Target = objc.objc_allocateClassPair(cls("NSObject"), b"AD2MenuTarget", 0)
objc.class_addMethod(Target, objc.sel_registerName(b"onMenu:"), ctypes.cast(on_menu, ctypes.c_void_p), b"v@:@")
objc.objc_registerClassPair(Target)

app = send(cls("NSApplication"), "sharedApplication")
send(app, "setActivationPolicy:", 1, restype=ctypes.c_bool, argtypes=[ctypes.c_long])   # accessory: no Dock icon
target = send(send(Target, "alloc"), "init")
item = send(send(cls("NSStatusBar"), "systemStatusBar"), "statusItemWithLength:", -1.0, argtypes=[ctypes.c_double])
send(item, "retain")
send(send(item, "button"), "setTitle:", nsstr("5h ~4.2 · wk 5.6"))
send(send(item, "button"), "setToolTip:", nsstr("Claude Code room left…"))      # hover detail
menu = send(send(cls("NSMenu"), "alloc"), "init")

def add(title, tag, fn):
    mi = send(send(cls("NSMenuItem"), "alloc"), "initWithTitle:action:keyEquivalent:",
              nsstr(title), objc.sel_registerName(b"onMenu:"), nsstr(""))
    send(mi, "setTarget:", target); send(mi, "setTag:", tag, restype=None, argtypes=[ctypes.c_long])
    send(menu, "addItem:", mi); HANDLERS[tag] = fn

add("Open dashboard", 1, lambda: open_url(dashboard_url))
send(item, "setMenu:", menu)
send(app, "run", restype=None)                        # main thread only; never returns until terminate:
```

Design notes for the port:

- **AppKit owns the main thread.** `NSApplication run` must be called on the process's main thread, and every AppKit call must happen there. Other threads hand work over with `performSelectorOnMainThread:withObject:waitUntilDone:` (proved) on a target-class method that drains a Python `queue.Queue`. On Windows the tray is just another thread, but on macOS it cannot be. So run the menu bar as **its own process** (for example `run.py --menubar`, launched by the service, or the reverse: the menu bar process is the app's main process and starts the service in background threads). Recommended: the app-bundle process runs AppKit on its main thread and the existing service (HTTP server, engine, scheduler) on background threads. That is one process, one bundle identity (needed for notifications, section 3), and the snapshot is read in-process as `tray.py` does today. If AppKit crashes the process, launchd's KeepAlive (section 4) restarts it.
- **Keep the seam.** Wrap the `send` calls in a `MacStatusBar` seam object (`set_title`, `set_tooltip`, `set_menu(MenuItem…)`, `set_image`, `alert(prompt)`) so tests use a fake, as `Win32` is faked today. Reuse `tray.menu_model`, `tooltip`, `switch_prompt` and `pool.summarize` unchanged; only the drawing layer is new.
- **Click behaviour.** With a menu set, any click opens the menu (the Mac convention). To make a left click open the dashboard and a right click open the menu (the widget's behaviour), set no menu, give the button `setTarget:`/`setAction:` and `sendActionOn:` (left | right mouse up), and in the action read `NSApp.currentEvent.type`; for a right click, assign the menu, call `button performClick:`, then clear it. Recommend the plain Mac convention ("Open dashboard" as the first menu item).
- **Totals text.** One line only (the menu bar is about 24 pt tall); for example `4.2 · 5.6` with tones. For stable width use `setAttributedTitle:` with `NSFont monospacedDigitSystemFontOfSize:weight:` and colour runs via `NSForegroundColorAttributeName` (both are plain message sends). The ring: render the existing `tray.ring_pixels` RGBA to PNG bytes (stdlib `zlib`+`struct`), `NSData dataWithBytes:length:` → `NSImage initWithData:` → `button setImage:`; use `setTemplate:YES` only for a monochrome icon.
- **Consent dialogs** (`tray.ask` / MessageBox): `NSAlert` (`alertWithMessageText…` is deprecated; use `alloc/init`, `setMessageText:`, `addButtonWithTitle:`, `runModal` → 1000/1001). Call `NSApp activateIgnoringOtherApps:YES` first, or the alert opens behind other windows for an accessory app.
- Keep references to every `CFUNCTYPE` object and every block for the process lifetime; a collected callback crashes the process on the next click.

### Alternatives considered

| Candidate | Verdict |
|---|---|
| **JXA helper** (`osascript -l JavaScript`, ObjC bridge) | Works headless too (**[mac]**: status item, title, `ObjC.registerSubclass` target, menu action fired). But it is a second runtime with weak error reporting, IPC to Python is needed (stdin pipe via `NSFileHandle` notifications, or loopback HTTP polling), the process shows as `osascript` (Activity Monitor, TCC prompts attribute to it), and JXA has had no Apple development since 2016. Keep it as the fallback if ctypes proves unstable. |
| **Swift helper compiled at install time** | Not viable on a stock Mac: `/usr/bin/swiftc` is a 118 KB `xcrun` shim (the same file is hard-linked 78 times on the test Mac), and without Command Line Tools invoking it opens the "install developer tools" dialog. The test Mac has Xcode and CLT installed (Swift 6.3.3), so it compiles there, but that is not a stock Mac. A prebuilt helper would be a non-Python runtime component that has to be built, signed and pinned per release. |
| **PyObjC / rumps** | Excluded by the runtime rule. |

## 3. Notifications

### Recommendation: `UNUserNotificationCenter` from the bundled process via ctypes, with `osascript` as fallback **[mac-partial]**

| Technique | Attribution | Permission | Click | Verified |
|---|---|---|---|---|
| `UNUserNotificationCenter` (ctypes + a global block for the completion handler) | **AccountDeck 2** (bundle name/icon) | one system prompt on first `requestAuthorization`; the user can change it in System Settings › Notifications › AccountDeck 2 | activates the app; a delegate (`userNotificationCenter:didReceiveNotificationResponse:…`) can open the dashboard | plumbing **[mac]**, delivery **[owner]** |
| `NSUserNotificationCenter` (deprecated since macOS 11, still present on 26.6) | AccountDeck 2 | none for banners on old systems; on recent macOS it routes through the same permission | as above | class present, center non-nil in the bundle **[mac]**; delivery **[owner]** |
| `osascript -e 'display notification "…" with title "…"'` | **Script Editor** (its icon and name) | Script Editor needs notification permission; on recent macOS the first call prompts for Script Editor, and if it is denied nothing shows | **opens Script Editor** | runs; **exit code 0 even when nothing is delivered** **[mac]** |
| Browser notifications from the dashboard page (already implemented as the `page` sink) | the browser | per-site browser permission | focuses the tab | existing code |

Facts observed on the test Mac:

- `UNUserNotificationCenter currentNotificationCenter` **aborts the process** (`NSInternalInconsistencyException: bundleProxyForCurrentProcess is nil`) when Python runs from the bare runtime folder. Run as the in-bundle symlink `AccountDeck 2.app/Contents/MacOS/python3` (section 5) and it returns a center. `NSUserNotificationCenter defaultUserNotificationCenter` likewise returns nil without a bundle and a center with one. **Never call these outside the bundle context**: guard with `NSBundle.mainBundle.bundleIdentifier` first.
- With the bundle identity, `requestAuthorizationWithOptions:completionHandler:` and `addNotificationRequest:withCompletionHandler:` both ran and called back into Python through a ctypes block. Over SSH they reported `NSCocoaErrorDomain 4099 … com.apple.usernotifications.listener … invalidated` (no notification service in a non-GUI session). So errors are reported properly, unlike osascript.
- `osascript display notification` in the same session printed `Connection to notification center invalid` on stderr and still **exited 0**. Do not use its exit code as a delivery signal.

Block snippet (the part that is not obvious):

```python
class BlockDescriptor(ctypes.Structure):
    _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong)]
class BlockLiteral(ctypes.Structure):
    _fields_ = [("isa", ctypes.c_void_p), ("flags", ctypes.c_int), ("reserved", ctypes.c_int),
                ("invoke", ctypes.c_void_p), ("descriptor", ctypes.POINTER(BlockDescriptor))]
GLOBAL_ISA = ctypes.c_void_p.in_dll(ctypes.CDLL(None), "_NSConcreteGlobalBlock")

def make_block(cfunc):                     # cfunc's first argument is the block itself
    desc = BlockDescriptor(0, ctypes.sizeof(BlockLiteral))
    block = BlockLiteral(ctypes.addressof(GLOBAL_ISA), 1 << 28, 0, ctypes.cast(cfunc, ctypes.c_void_p), ctypes.pointer(desc))
    block._keep = (cfunc, desc)            # keep alive as long as the block may be called
    return block

AUTH_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p)   # ^(BOOL granted, NSError *e)
ADD_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)                  # ^(NSError *e)
auth = make_block(AUTH_CB(lambda blk, granted, err: print("granted", granted)))
add_block = make_block(ADD_CB(lambda blk, err: print("error" if err else "queued")))
center = send(cls("UNUserNotificationCenter"), "currentNotificationCenter")
send(center, "requestAuthorizationWithOptions:completionHandler:", (1 << 2) | (1 << 1), ctypes.byref(auth),
     restype=None, argtypes=[ctypes.c_ulong, ctypes.c_void_p])          # alert | sound
content = send(send(cls("UNMutableNotificationContent"), "alloc"), "init")
send(content, "setTitle:", nsstr("AccountDeck 2"), restype=None); send(content, "setBody:", nsstr("Limit reset"), restype=None)
req = send(cls("UNNotificationRequest"), "requestWithIdentifier:content:trigger:", nsstr("kind|account|window"), content, None)
send(center, "addNotificationRequest:withCompletionHandler:", req, ctypes.byref(add_block),
     restype=None, argtypes=[ctypes.c_void_p, ctypes.c_void_p])
```

Use the `Delivery.key` (`kind|account|window`) as the request identifier: a new request with the same identifier **replaces** the shown one, which matches `notify.Delivery` semantics. Completion handlers run on a background queue, so do not touch AppKit from them. Map "not authorised" to the notifier's "desktop not accepting notifications" (defer, then expire).

**[owner]** On a logged-in Mac: whether the permission prompt appears for an ad-hoc-signed, non-notarised bundle whose executable is a symlink to `python3.14` (Identifier `python3.14`, `Signature=adhoc`), and whether banners show with the AccountDeck icon. If UN refuses that bundle, fall back to `NSUserNotificationCenter`, then to osascript, and say in Settings that clicks open Script Editor.

## 4. Start at login

### What it replaces

`autostart.py` writes HKCU `…\Run\AccountDeck2` = `"<pythonw>" "<run.py>" --serve --tray --no-browser`, reads Task Manager's `StartupApproved` record, and reports `Status(registered, command, approved)` with `enabled = registered and approved is not False`; `repair()` rewrites a moved path. All registry access goes through a `Registry` seam that tests fake.

### Recommendation: a user LaunchAgent in `~/Library/LaunchAgents`, managed with `launchctl bootstrap/bootout` **[mac-partial]**

`SMAppService` (macOS 13+) is the modern API, but it wants the plist **inside the signed app bundle** (`Contents/Library/LaunchAgents`), is called from the app through ObjC (`SMAppService agentServiceWithPlistName:` → `registerAndReturnError:`), and is effectively designed for Developer-ID-signed apps; with an ad-hoc bundle whose executable is a symlink it is unlikely to register cleanly **[doc/inferred]**. A plain LaunchAgent needs nothing but a file and `launchctl`, is fully reversible from code, and maps one-to-one onto `autostart.Status`.

Plist (written and linted on the test Mac with `plutil -lint` → OK and read back with `plistlib`; **not loaded**, per the rules):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.accountdeck.accountdeck2</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/NAME/Applications/AccountDeck 2.app/Contents/MacOS/AccountDeck2</string>
    <string>--serve</string><string>--tray</string><string>--no-browser</string>
  </array>
  <key>AssociatedBundleIdentifiers</key><array><string>com.accountdeck.accountdeck2</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>   <!-- restart after a crash, not after Quit -->
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>                 <!-- GUI logins only, not SSH -->
  <key>StandardOutPath</key><string>/Users/NAME/Library/Logs/AccountDeck2/launchd.log</string>
  <key>StandardErrorPath</key><string>/Users/NAME/Library/Logs/AccountDeck2/launchd.log</string>
</dict>
</plist>
```

Write it with `plistlib.dump` (absolute paths only; launchd does not expand `~` or `$HOME`; paths with spaces are fine because `ProgramArguments` is an array). Run the bundle launcher, not the bare Python, so the process has the app's bundle identity (sections 3 and 5).

Commands (`launchctl help` on 26.6 lists `bootstrap`, `bootout`, `enable`, `disable`, `kickstart`, `print`, `print-disabled`; `load`/`unload` are marked legacy):

```sh
UID_=$(id -u); L=com.accountdeck.accountdeck2; P="$HOME/Library/LaunchAgents/$L.plist"
# on
launchctl bootout "gui/$UID_/$L" 2>/dev/null || true      # idempotent: drop an older copy first
launchctl enable "gui/$UID_/$L"                            # clear a previous `disable` override
launchctl bootstrap "gui/$UID_" "$P"                       # loads now; RunAtLoad starts it now and at every login
# off (fully reversible, no System Settings)
launchctl bootout "gui/$UID_/$L"; rm -f "$P"
# status
launchctl print "gui/$UID_/$L" >/dev/null 2>&1 && echo loaded     # non-zero when not loaded
launchctl print-disabled "gui/$UID_" | grep "\"$L\" => disabled"
```

Mapping to `autostart.Status`: `registered` = the plist exists with our `ProgramArguments`; `command` = `ProgramArguments`; `approved` = `False` when `print-disabled` lists the label as disabled or `bootstrap` fails with a permission error (below), otherwise `None`. `repair()` rewrites the plist when the app path changed, then runs bootout + bootstrap.

Limits and things to check on a real Mac **[owner]**:

- The `gui/<uid>` domain exists only while that user is logged in at the console. Over SSH on the test Mac it was absent (`Could not print domain: 125`), so bootstrap could not be tried. `print-disabled user/<uid>` worked and shows the `enabled/disabled` override list format. Do this from the app (which runs in the GUI session), never from an SSH installer.
- **Background Task Management (macOS 13+):** adding a LaunchAgent shows a "Background Items Added" notification, and the item appears in System Settings › General › Login Items & Extensions › "Allow in the Background". For an unsigned item it is listed by the executable or bundle name; `AssociatedBundleIdentifiers` lets it show the app's name and icon when the signatures match, which is not guaranteed for an ad-hoc app. If the user switches it off there, launchd will not run it, and `bootstrap` fails (typically `Bootstrap failed: 5: Input/output error`). Treat that failure as `approved=False` and tell the user to switch it on in Login Items. Reading the BTM database directly (`sfltool dumpbtm`) needs admin rights; do not depend on it.
- Removing the plist and `bootout` reverses everything; System Settings is only needed if the user disabled it there.
- With `KeepAlive.SuccessfulExit=false`, a user "Quit" must exit with status 0, or launchd restarts it after `ThrottleInterval`.

## 5. App bundle and install

### What `install.ps1` does (so `install.sh` matches)

Parameters `-NoLaunch`, `-StartOnLogin`, `-PythonExe`. It checks Python ≥ 3.12 imports `cryptography` (installs nothing), requires `pythonw.exe` beside it and `accountdeck2.ico`, then creates **"AccountDeck 2.lnk" on the Desktop and in the Start menu** (target `pythonw.exe "<root>\run.py"`, working directory = app root, icon = the .ico, minimised). With `-StartOnLogin` it runs `run.py --set-autostart on`. Unless `-NoLaunch` it starts `pythonw run.py` hidden, then prints a one-line confirmation that says AccountDeck 1 was not touched.

`install.sh` equivalent: `--no-launch`, `--start-on-login`; **downloads and verifies** the pinned runtime into `<app root>/runtime/python` (section 6) instead of taking `-PythonExe`; self-tests it; builds `~/Applications/AccountDeck 2.app` (the Start-menu shortcut equivalent: Spotlight and Launchpad index `~/Applications`; a Desktop alias is not the Mac habit, so skip it); `--start-on-login` → `runtime/python/bin/python3 -I run.py --set-autostart on` (writes the LaunchAgent, section 4, which must then be bootstrapped from the GUI session); launch with `open "$HOME/Applications/AccountDeck 2.app"`; print the same kind of one-line confirmation. `~/Applications` does not exist by default (it did not on the test Mac): `mkdir -p` it.

### Recommended bundle layout **[mac]** (built and run on the test Mac; not opened through Finder/LaunchServices **[owner]**)

```
~/Applications/AccountDeck 2.app/
  Contents/
    Info.plist
    MacOS/AccountDeck2        # /bin/sh launcher (CFBundleExecutable)
    MacOS/python3 -> <app root>/runtime/python/bin/python3.14   # symlink: gives Python the bundle identity
    Resources/AccountDeck2.icns
```

```sh
#!/bin/sh
# Contents/MacOS/AccountDeck2. Exec the in-bundle python link (same PID, so LaunchServices keeps tracking it).
HERE="$(cd "$(dirname "$0")" && pwd)"
APPROOT='/Users/NAME/AccountDeck2'          # written by install.sh
cd "$APPROOT" || exit 1
exec "$HERE/python3" -I "$APPROOT/run.py" "$@"
```

Why this shape (all observed on the test Mac with a probe script printing `NSBundle.mainBundle`):

| Launch method | `bundlePath` / `bundleIdentifier` seen by Python |
|---|---|
| `Contents/MacOS/X` is a **symlink** to `python3.14` | the `.app` / `com.accountdeck.probe` ✔ |
| shell launcher that `exec`s the **runtime's own path** | `…/runtime/python/bin` / **None** ✘ |
| shell launcher that `exec`s the **in-bundle symlink** (above) | the `.app` / bundle id ✔ (also passes `run.py` and arguments) |
| a **hard link** or copy of `python3.14` in `MacOS/` | Python cannot find its stdlib (it locates `lib/` via `@executable_path/../lib` and its prefix by resolving symlinks) ✘ |

The bundle identity is what makes `UNUserNotificationCenter` work at all (section 3), names the app in TCC prompts, and lets the LaunchAgent associate with it.

Info.plist:

```xml
<dict>
  <key>CFBundleIdentifier</key><string>com.OWNER.accountdeck2</string>   <!-- owner picks the reverse-DNS -->
  <key>CFBundleName</key><string>AccountDeck 2</string>
  <key>CFBundleDisplayName</key><string>AccountDeck 2</string>
  <key>CFBundleExecutable</key><string>AccountDeck2</string>
  <key>CFBundleIconFile</key><string>AccountDeck2</string>               <!-- Resources/AccountDeck2.icns -->
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleShortVersionString</key><string>2.0.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>                  <!-- the cryptography wheel's floor -->
  <key>LSUIElement</key><true/>                                           <!-- no Dock icon, no app menu: menu bar only -->
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>AccountDeck 2 asks Claude to quit before switching accounts.</string>   <!-- shown in the Automation prompt -->
</dict>
```

**LSUIElement: yes.** The menu bar item is the whole UI and the dashboard lives in the browser. `LSUIElement` hides the Dock icon and app menu from launch on (no flash); `setActivationPolicy:1` in code (section 2) does the same at runtime but only after Python starts. Re-opening the app from Finder/Spotlight while it runs sends a reopen Apple Event to the running instance (LaunchServices does not start a second copy). Handle it by opening the dashboard (application delegate `applicationShouldHandleReopen:hasVisibleWindows:`), or rely on the existing owner lock to make a second copy exit.

Build with `plistlib.dump` (or a heredoc) and validate with `plutil -lint` (OK on the test Mac). After writing, register it so Finder and Spotlight pick up the name and icon at once: `/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"` (present on 26.6; `-u` unregisters on uninstall).

### Icon (.icns) **[mac]**

`accountdeck2.ico` holds only 64/32/16 px; `sips` reads it, but the result is 64 px, too small for Finder (needs 512 and 1024). Two working routes, both checked visually at 256 px with identical results:

```sh
# A: sips renders the SVG directly (`sips -s format png -Z 1024 icon.svg` gave a crisp 1024 px PNG with alpha
#    on 26.6, despite the file's UTF-8 BOM; the per-size loop below is the same call, not run as a loop)
mkdir AccountDeck2.iconset
for s in 16 32 128 256 512; do
  sips -s format png -z $s $s accountdeck/web/icon.svg --out AccountDeck2.iconset/icon_${s}x${s}.png
  sips -s format png -z $((s*2)) $((s*2)) accountdeck/web/icon.svg --out AccountDeck2.iconset/icon_${s}x${s}@2x.png
done
iconutil -c icns AccountDeck2.iconset -o AccountDeck2.icns
# B: stdlib only: make_icon.render(px) (BGRA) -> PNG via zlib/struct, the same iconset names, then iconutil.
#    16..1024 px took 6 s total with SAMPLES lowered to 4 (≤256 px) and 2 (512/1024 px).
```

The result is a 114931-byte `Mac OS X icon … "ic12" type`, which `sips` reads as 1024 px wide. Recommendation: **route B** in a small `make_icon.py --icns` mode, so the Mac icon comes from the same drawing code as the Windows one. Design note: Mac app icons normally leave about 10% transparent margin around the body (Apple's grid is an 824-px body on a 1024 canvas); the current mark is full-bleed and will look a little large next to other icons. Rendering it inset is a one-line change.

### Gatekeeper and quarantine **[mac]**

| Situation | Result on the test Mac |
|---|---|
| Files fetched with `curl` | **no** `com.apple.quarantine` xattr (`xattr -l` empty) |
| A quarantined Mach-O (copy of `python3.14` with a Safari-style quarantine xattr) run from the shell | **killed with SIGKILL (exit 137)** by Gatekeeper |
| The same binary without the xattr | runs |
| A quarantined `.py` file read by the interpreter | runs (scripts are data to Python; Gatekeeper does not check them) |
| `cp` of a quarantined file | the copy **keeps** the quarantine xattr |
| `spctl --assess --type execute` on the bundle (unsigned, or ad-hoc signed) | `rejected` (not notarised); this only matters for a quarantined bundle opened from Finder |

Consequences: the installer must fetch the runtime itself with `curl` (never ask the user to download the tarball in a browser). If AccountDeck itself arrives as a browser-downloaded zip, its files carry quarantine (Archive Utility propagates it). The Python sources still run, but `install.sh` should clear quarantine only on what it builds and verifies: `xattr -dr com.apple.quarantine "$APP" "$APPROOT/runtime"`. A bundle that stays quarantined would show "cannot be opened because Apple cannot check it for malicious software" on first open (Finder › Open, or System Settings › Privacy & Security › Open Anyway, is the user's way out) **[doc]**.

Signing: `python3.14` from python-build-standalone is already ad-hoc, linker-signed (`flags=0x20002(adhoc,linker-signed)`), which is all arm64 needs to execute. The thin bundle above (symlink pointing outside) **cannot be signed**: `codesign --sign -` exits 0, but `codesign --verify` fails with `invalid destination for symbolic link in bundle`. If the owner check shows that notifications or TCC need a valid bundle signature, switch to the self-contained layout: runtime copied to `Contents/Resources/python` with `MacOS/python3 -> ../Resources/python/bin/python3.14`. That layout signs with `codesign --force --deep --sign -` and verifies with `--strict`, and it still gives the bundle identity (both checked). The cost: an ad-hoc signature changes on every rebuild, so TCC may ask again after updates **[inferred]**.

## 6. Pinned runtime

### Pins (looked up 2026-10-07; downloads verified on the test Mac **[mac]**)

CPython 3.14.8, python-build-standalone release `20261003` (published 2026-10-03, the newest at research time), `install_only` flavour (not `install_only_stripped`, which drops debug symbols but is otherwise the same; either works):

| Asset | Size (bytes) | SHA-256 |
|---|---|---|
| `https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.14.8%2B20261003-aarch64-apple-darwin-install_only.tar.gz` | 26798928 | `d15291f940cfecd2e54010d5e37d2e03aa192f076a65d26ab741372fff2dabfe` |
| `https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.14.8%2B20261003-x86_64-apple-darwin-install_only.tar.gz` | 26969076 | `4b25eec9616cf363ad7181b2e8098f76fc453705fce22322da2502c9e16f44a9` |

The sums match three sources: the GitHub API asset `digest`, the release's `SHA256SUMS` file, and `shasum -a 256` of the actual download on the test Mac.

cryptography (PyPI JSON API, `https://pypi.org/pypi/cryptography/50.0.1/json`):

| Wheel | Size | SHA-256 | Notes |
|---|---|---|---|
| `https://files.pythonhosted.org/packages/ba/19/797e2aaac9df6a66f1550f49979dc1b1e39ecd2077501c30efa81e8d5d67/cryptography-50.0.1-cp311-abi3-macosx_11_0_arm64.whl` | 4010153 | `b8f852c65863251b9e3a1b8c150ce21e59b522dbb6a7d4bc80e680d38388e986` | Apple silicon. The pin. |
| `https://files.pythonhosted.org/packages/1b/bc/ee4137cbbe105652c0ee4252792b78fc8e7afa4b8e61d9d5dc05a7f45731/cryptography-48.0.1-cp311-abi3-macosx_10_9_universal2.whl` | 8008324 | `3e4a1a3232eef2e6c732827d5722db29a0cc8b27af2a4d865b094cf954be9ca1` | Intel fallback, see below. |

**cryptography 50.0.1 has no Intel macOS wheel.** From 49.0.0 on, cryptography ships macOS wheels for arm64 only (`macosx_11_0_arm64`). The newest release with an Intel-capable macOS wheel is **48.0.1** (`universal2`, 2026-06-09). 50.0.2 (2026-09-30) also has arm64 only. Choices for the owner: support Apple silicon only (simplest; the repo pins `cryptography==50.0.1`), or pin 48.0.1 on Intel Macs (two versions in the field; the project uses only `AESGCM` and `InvalidTag`, which 48 also has).

### Finding: cryptography needs cffi at runtime (owner decision)

On the test Mac, `pip install --no-index --no-deps cryptography-50.0.1-…arm64.whl` into the standalone Python succeeded, and `python -c "import cryptography, sys; print(sys.version)"` printed `3.14.8 (main, Oct  3 2026, 00:54:40) [Clang 22.1.3 ]` and `50.0.1`. **But the first real use fails:**

```
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
ModuleNotFoundError: No module named '_cffi_backend'
```

The Rust extension `_rust.abi3.so` imports `_cffi_backend` (it is in its strings, and `Requires-Dist: cffi>=2.0.0` is in the wheel metadata; 48.0.1 has the same requirement). The Windows install works only because pip pulled cffi in there. So the Mac installer needs a third pinned file, or AccountDeck cannot decrypt anything. The rules only allowed Python and cryptography downloads, so **cffi was not downloaded or tested**. The pins would be (PyPI, cffi 2.1.1, uploaded 2026-08-03):

| Wheel | Size | SHA-256 |
|---|---|---|
| `https://files.pythonhosted.org/packages/b3/a9/9db617d05d7367c1ad0ab00b3aa6e6f9281edd689b4ee9ea0e5a84e89c97/cffi-2.1.1-cp314-cp314-macosx_11_0_arm64.whl` | 184962 | `661c298b4821edebead0c91edd2b00374d67ad7c5a1f7a91d4442633b79d6a72` |
| `https://files.pythonhosted.org/packages/d9/99/c4b0c17cacdc9c3b8f280026286a9826d6a208c0f047591a3c3ce99b91fd/cffi-2.1.1-cp314-cp314-macosx_10_15_x86_64.whl` | 184964 | `d28630f5854ab07ab1fd4aba756de52326c82e6be15d414b12793f1975048b54` |

cffi declares `pycparser`, but that is used only by `cffi.FFI().cdef()` at build time; cryptography loads only the C module `_cffi_backend` **[inferred]**. Confirm with `--no-deps` on a Mac once the owner allows the cffi download.

**The installer's self-test must exercise the binding**, not only `import cryptography`:

```sh
"$PY" -I -c 'import sys; from cryptography.hazmat.primitives.ciphers.aead import AESGCM; import os; k=AESGCM.generate_key(256); n=os.urandom(12); assert AESGCM(k).decrypt(n, AESGCM(k).encrypt(n, b"x", None), None) == b"x"; assert sys.version_info >= (3, 14)'
```

### Install technique (verified on the test Mac for arm64 **[mac]**)

```sh
set -eu
ARCH=$(uname -m)                      # arm64 or x86_64 (under Rosetta a shell says x86_64: use `sysctl -n hw.optional.arm64`)
RT="$HOME/Library/Application Support/AccountDeck2/runtime"   # or inside the app folder; see section 5
DL=$(mktemp -d); trap 'rm -rf "$DL"' EXIT
curl -fsSL --proto '=https' --tlsv1.2 -o "$DL/py.tgz" "$PY_URL"
echo "$PY_SHA256  $DL/py.tgz" | shasum -a 256 -c -          # abort on mismatch (set -e)
mkdir -p "$RT.new" && tar -xzf "$DL/py.tgz" -C "$RT.new"     # yields $RT.new/python/bin/python3
curl -fsSL --proto '=https' -o "$DL/$WHEEL_NAME" "$WHEEL_URL"   # keep the real file name: pip parses tags from it
echo "$WHEEL_SHA256  $DL/$WHEEL_NAME" | shasum -a 256 -c -
"$RT.new/python/bin/python3" -I -m pip install --no-index --no-deps --disable-pip-version-check "$DL/$WHEEL_NAME"   # (+ cffi wheel)
# self-test above, then swap: rm -rf "$RT.old"; [ -d "$RT" ] && mv "$RT" "$RT.old"; mv "$RT.new" "$RT"
```

Facts observed: the archive unpacks to a single `python/` folder (70 MB unpacked); it includes pip 26.2.1, OpenSSL 3.5.9, `ssl`, `sqlite3`, `ctypes`, `fcntl`; `sys.platform` is `darwin`. `pip install --no-index --no-deps <wheel>` works fully offline. Files fetched with `curl` carry no `com.apple.quarantine` attribute (`xattr -l` was empty), so Gatekeeper never checks the interpreter (section 5). The test Mac has no Rosetta, so the x86_64 build was checksum-verified and confirmed as a `Mach-O 64-bit executable x86_64` but not run. Use `shasum -a 256` (always on macOS); `sha256sum` is not.

Limits: `-I` (isolated mode) keeps user site-packages and `PYTHON*` variables out; use it for every AccountDeck invocation. Do not use the system `/usr/bin/python3` (it is the Xcode/CLT stub on a stock Mac and triggers an install dialog).

## 7. Paths and conventions

| Windows today | macOS | Verified |
|---|---|---|
| `%LOCALAPPDATA%\AccountDeck2` (data, `Paths.live`) | `~/Library/Application Support/AccountDeck2` (mode 0700) | dir exists by default **[mac]** |
| owner lock `%LOCALAPPDATA%\AccountDeck2-Owner.lock` (beside, not inside, the data dir) | `~/Library/Application Support/AccountDeck2-Owner.lock` | **[mac]** semantics below |
| logs in the data folder (`diagnostics.py`) | `~/Library/Logs/AccountDeck2/` (Console.app shows it under "Log Reports"); launchd stdout/stderr also there | dir exists **[mac]** |
| app root `Documents\AccountDeck2` | **not** `~/Documents` on a Mac: with iCloud "Desktop & Documents Folders" on, it is synced, and "Optimize Mac Storage" can evict files, including the Python runtime, to dataless placeholders. Use `~/Library/Application Support/AccountDeck2/app` or `~/AccountDeck2`. | **[doc]** |
| `os.environ["LOCALAPPDATA"]` | `Path.home() / "Library" / ...`; do not trust `$HOME` under launchd (it is set, but use `pwd.getpwuid(os.getuid()).pw_dir` as the source of truth) | **[doc]** |

`Paths.live` needs a `sys.platform == "darwin"` branch; `Paths.synthetic` stays the same on both.

### File locking: `fcntl.flock`, not `lockf` **[mac]**

Replacement for `core.OwnerLock` (`msvcrt.locking(fd, LK_NBLCK, 1)`):

```python
import fcntl, os
def acquire(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:              # EAGAIN: someone else holds it
        os.close(fd); return None
    return fd                             # keep open for the process lifetime; close (or exit) releases it
```

Observed: while another process held it, `flock(LOCK_EX|LOCK_NB)` raised `BlockingIOError` (EAGAIN). After that holder was SIGKILLed, the lock was free at once (the kernel drops it on exit, as on Windows). A second `open()` of the same file **in the same process** is also blocked: flock belongs to the open file description, which matches the Windows "two instances or data dirs" behaviour. **Do not use `fcntl.lockf`/`fcntl.fcntl(F_SETLK)`** (POSIX record locks): the probe showed that closing *any* unrelated descriptor of that file in the holding process silently dropped the lock, and another process then acquired it. Python's own `open().close()` of the lock file elsewhere would trigger that.

The provider lock files (`.oauth_refresh.lock`, `.claude.json.lock`) are the other programs' conventions. Check in PROVIDERS-MAC.md which primitive Claude Code uses on macOS (proper-lockfile style `mkdir` locks vs flock) before reusing `cli_locks.py` unchanged.

### Atomic replace **[mac]**

`os.replace` = `rename(2)`: atomic within one volume, and on macOS it **succeeds while other processes have the old file open** (observed: an open reader kept reading `old`, a new `open` saw `new`). The Windows retry loop in `core.replace_with_retry` (winerror 5/32/33) is harmless but never triggers on macOS; keep it platform-neutral. Cross-volume rename fails (`EXDEV`), so always create the temp file in the destination directory (as `atomic_write` already does).

Durability: on macOS `os.fsync` only pushes data to the drive's cache. For crash-safe vault writes use `fcntl.fcntl(fd, fcntl.F_FULLFSYNC)` (exposed by Python 3.14 on darwin, worked on the test Mac), then `fsync` the directory fd after the rename (worked):

```python
def atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)    # created 0600 (observed)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush()
            if hasattr(fcntl, "F_FULLFSYNC"): fcntl.fcntl(f.fileno(), fcntl.F_FULLFSYNC)
            else: os.fsync(f.fileno())
        os.replace(name, path)                                        # keeps the temp file's 0600
        d = os.open(path.parent, os.O_RDONLY); os.fsync(d); os.close(d)
    finally:
        Path(name).unlink(missing_ok=True)
```

### Permissions **[mac]**

The default umask is `022`. `os.makedirs(p, mode=0o700)` → `0700`; `os.open(..., O_CREAT|O_EXCL, 0o600)` → `0600`; `tempfile.mkstemp` → `0600`; `os.replace` keeps the source's mode (`0600`). The home folder is `drwxr-x---+` and `~/Library` is `drwx------`, so Application Support is already private to the user. Still create the data dir 0700 and private files 0600 explicitly, and `chmod` existing ones on start: a folder copied from elsewhere keeps its old mode. `makedirs(mode=)` only applies to the leaf; create parents first.

### Loopback server and opening the browser

- Bind `127.0.0.1` with port 0 (or the fixed port), never `0.0.0.0`. Observed: bind/listen/connect on `127.0.0.1:<ephemeral>` works **[mac]**. The macOS application firewall only prompts ("accept incoming network connections?") for listeners on non-loopback addresses, and macOS 15+ "Local Network" privacy covers LAN, not loopback **[doc]**.
- **Never put a secret in a command line.** On macOS `/bin/ps` is **setuid root** (`-rwsr-xr-x root`), and over SSH as an ordinary user it printed the full argv of another user's processes (307 processes with arguments) **[mac]**. Any local user can read any process's arguments. The dashboard URL is `http://127.0.0.1:<port>/#token=<token>` (`tray.dashboard_url`), so **do not** run `open "<url>"`.
- Open the dashboard either with **`webbrowser.open(url)`** (keep it, as `tray._open_browser` does today) or in-process with AppKit. In Python 3.14, `webbrowser.MacOSXOSAScript` pipes `open location "<url>"` to `/usr/bin/osascript` **over stdin** (read in the pinned 3.14.8 source), so the token never appears in argv. `open location` is a scripting addition, not an event to another app, so no Automation prompt **[doc]**. In the AppKit menu-bar process, `NSWorkspace.sharedWorkspace openURL:` with `NSURL URLWithString:` does the same with no child process at all (two message sends with the section 2 helper). Not run (no GUI apps allowed). The same argv rule applies to `security … -w <secret>` (section 8).

## 8. Keychain via `security`

### Recommendation **[mac]** (temporary keychain file only)

Replace DPAPI with **one random 32-byte vault key per data folder, kept in the login keychain as a generic password**, and keep the vault records as AES-GCM files in the data folder (the project already depends on `AESGCM`). Talk to Keychain only through **`/usr/bin/security`**, never through Security.framework from Python:

- An item gets an ACL that trusts the app that **created** it (`security` usage text: "By default, the application which creates an item is trusted to access its data without warning"). When `/usr/bin/security` creates the item, later `security find-generic-password` calls are silent whoever runs them. `security` is Apple-signed, so its identity is stable across macOS updates.
- If Python called `SecItemAdd` itself, the trusted app would be the ad-hoc-signed `python3.14`. Its identity is its cdhash, which changes with every runtime update, so every update would bring back the "wants to use your confidential information" prompt **[inferred]**.
- Store **ASCII base64url text** (the key is 43 characters), never raw bytes. That makes `-w` output unambiguous (see the hex rule below) and keeps the `security -i` line far below its limit.
- Writing an existing item's data from Python (`SecKeychainItemModifyAttributesAndData`) is no better: the item moves to the writer's keychain partition (`cdhash:` of the ad-hoc `python3`), and every later `security` read, Claude Code's included, asks for the login keychain password until `security` writes the item again **[mac, 2026-10-09: securityd "ACL partition mismatch: client apple-tool:"]**.
- Pass the secret on **stdin with `security -i`**, never in argv (`/bin/ps` is setuid root, section 7). The one exception is a value too long for one `security -i` line (Claude Code's login once it holds MCP sign-ins, about 15,000 characters): it goes as `add-generic-password -X <hex>` in argv, as Claude Code's own writes of that item do (`platform/keychain.py`).

```python
import base64, os, re, subprocess
SECURITY = "/usr/bin/security"
SERVICE = "AccountDeck2 vault"          # one item per data folder: account = sha256(data_dir)[:16]

def _q(text):                           # security -i quoting: double quotes, backslash escapes
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

def put_key(account, key: bytes, keychain=None):
    value = base64.urlsafe_b64encode(key).decode().rstrip("=")
    line = f"add-generic-password -U -a {_q(account)} -s {_q(SERVICE)} -l {_q('AccountDeck 2')} -w {_q(value)}"
    line += f" {_q(str(keychain))}" if keychain else ""
    assert len(line) < 4000            # security -i rejects command lines over ~4 KB
    r = subprocess.run([SECURITY, "-i"], input=line + "\n", text=True, capture_output=True, timeout=30)
    if r.returncode != 0 or r.stderr.strip():
        raise OSError(f"keychain write failed ({r.returncode})")    # never log r.stdout / the line

def get_key(account, keychain=None) -> bytes | None:
    argv = [SECURITY, "find-generic-password", "-a", account, "-s", SERVICE, "-w"] + ([str(keychain)] if keychain else [])
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)   # Allow/Deny dialog may block: keep a timeout
    if r.returncode == 44:
        return None                     # not found
    if r.returncode != 0:
        raise OSError(f"keychain read failed ({r.returncode})")   # 36 = locked / interaction not allowed (observed); others: denied or error
    value = r.stdout.rstrip("\n")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise ValueError("unexpected keychain value")
    return base64.urlsafe_b64decode(value + "=")

def delete_key(account, keychain=None):
    argv = [SECURITY, "delete-generic-password", "-a", account, "-s", SERVICE] + ([str(keychain)] if keychain else [])
    return subprocess.run(argv, capture_output=True, timeout=30).returncode in (0, 44)
```

### Observed behaviour of `security` on macOS 26.6

| Check | Result |
|---|---|
| `add-generic-password … -w v -U <kc>` on a new item / existing item | rc 0 / rc 0 (updated in place) |
| same without `-U` on an existing item | rc **45**, "The specified item already exists" |
| `find-generic-password -w` printable ASCII (`second`, `{"a":"b c"}`) | printed **literally**, plus a trailing `\n` |
| `-w` for a value with non-ASCII (`ünï ✓`), a newline, or binary (`-X 00ff10e29c93`) | the **whole value printed as lowercase hex with no prefix** (`c3bc6ec3af20e29c93`, `6c696e65310a6c696e6532`, `00ff10e29c93`) |
| ambiguity | an ASCII value that happens to look like hex (`7b2261223a317d0a`) is printed the same as a hex-encoded binary value, so **`-w` output cannot be decoded reliably unless you control the alphabet**. Hence base64url. |
| `-g` instead of `-w` | on **stderr**: `password: "plain text"` for printable, `password: 0xC3BC… "\303\274n…"` for non-printable (the `0x` prefix disambiguates; use `-g` and parse stderr if you must read values you did not write) |
| `-X <hex>` (add binary data as hex) | works, also inside `security -i` |
| `-w` as the last option with no value (prompt mode) | **not usable here**: an explicit keychain path is a trailing positional, so `-w` cannot be last; it printed usage (rc 2). The prompt also needs a tty. It does not read stdin. |
| `security -i` with `add-generic-password … -w "sp ace'q\"x"` on stdin | rc 0, value stored exactly; quoting with `"…"` and `\"` works |
| `ps -axww` while `security -i` runs with a secret on stdin | shows only `security -i`; 0 matches for the secret |
| `security -i` line length | 4088-byte line OK; **4128 bytes and above fail (rc 1)**. Base64 values up to ~3.9 KB fit; larger secrets belong in the encrypted vault file, not Keychain |
| item created with `-T ''` (no trusted app), then `find -w` with no GUI | rc **36** (user interaction not allowed). With a GUI this is the access dialog |
| locked keychain, `find -w` | rc **36** |
| missing item: `find` / `delete` | rc **44** "could not be found" |
| `delete-generic-password` existing | rc 0 |

### Access prompts **[doc]/[owner]**

- Items AccountDeck creates via `security`: no prompts on read, update or delete by `security`, because `security` is in the item's ACL.
- Login keychain locked (rare in a GUI session; it unlocks at login): `find` fails with 36 over SSH. In a GUI session macOS asks for the login password.
- Items **other apps** created (for example `Claude Safe Storage`, `Claude Code-credentials`; see PROVIDERS-MAC.md): `security find-generic-password -w` shows "security wants to use your confidential information stored in "…" in your keychain" with *Always Allow / Deny / Allow*. For items that carry a Team-ID partition list (macOS 10.12+), the dialog asks for the **login keychain password**, and "Always Allow" adds `security` (so every caller of `security`) to that item. That is a real widening of access, so prefer plain *Allow* and say so in the UI. While a dialog is up, `security` blocks, so always use a subprocess timeout. These are owner checks on a real Mac with the real apps; nothing here touched them.
- `-A` (any app, no warning) must never be used.

### Testing on a temporary keychain (exactly what was run) **[mac]**

```sh
T=$(mktemp -d /tmp/accountdeck-XXXXXX); K="$T/kc/test.keychain-db"; mkdir -p "$T/kc"
security list-keychains -d user > "$T/list.before"; security default-keychain -d user > "$T/default.before"
security create-keychain -p 'fake-test-pw' "$K"      # creates the file; does NOT add it to the search list (checked)
security set-keychain-settings "$K"                  # no auto-lock timeout
security unlock-keychain -p 'fake-test-pw' "$K"
# … every add/find/delete passes "$K" as the last argument (or inside the security -i line) …
security delete-keychain "$K"                        # removes the file
security list-keychains -d user | diff - "$T/list.before" && security default-keychain -d user | diff - "$T/default.before"
rm -rf "$T"
```

Both diffs were empty after every run on the test Mac: the search list and the default keychain never changed. The keychain password in `create-keychain -p` is in argv. That is acceptable only for a throwaway test keychain, never for a real secret. In the Python suite, give the Keychain wrapper a `keychain=` parameter (as in the snippet), make safe mode **require** a keychain path inside a registered test root (mirroring `autostart.Registry(sandbox=…)` and `procs.is_test_owned_path`), and refuse the login keychain outright in safe mode.
