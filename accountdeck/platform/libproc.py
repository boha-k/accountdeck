"""Read-only facts about this Mac's processes, asked of libSystem through ctypes (stdlib only, never `ps`).

This module only reads; signalling and starting processes stay in procs.py and platform/procs_mac.py, which
build on it (docs/mac/PLATFORM-MAC.md §1):

- the pid list (`proc_listallpids`) and each executable's path (`proc_pidpath`, symlinks resolved);
- parent, owner, status and start time in microseconds (`proc_pidinfo` PROC_PIDTBSDINFO). macOS gives the
  start time only for this user's processes; for anyone else's there is the short record without it, and a
  row without a start time is an unknown identity that callers refuse to act on;
- argv (`sysctl KERN_PROCARGS2`), never past argc: the environment that follows can hold secrets;
- open descriptors (`PROC_PIDLISTFDS` + `proc_pidfdinfo`): who holds a file open;
- a kqueue EVFILT_PROC/NOTE_EXIT watch, the macOS stand-in for a process handle: it follows the process,
  not its number, so "verify, then signal" cannot hit a recycled pid unnoticed.

Start times are returned in Windows FILETIME units (100 ns since 1601), the unit `ProcessInfo.ctime` has
always had, so ProcessSet and ConsentToken comparisons and the stored fixtures stay as they are.

The structure layouts below were checked against <sys/proc_info.h> on macOS 26.6 and 27 (arm64). libSystem
is bound on first use (`api()`), so importing this module touches nothing; tests replace the binding with a
scripted stand-in through `use_api`.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import os
import plistlib
import re
import select
import struct
import threading

from ..core import DeckError

OS_ERROR = "system"           # the error code for "macOS refused" (core.ERROR_CODES)
MAX_PID = 2**31 - 1           # pid_t is a 32-bit int (macOS itself hands out pids up to 99,999)
UNIX_TO_FILETIME = 116_444_736_000_000_000   # 1970-01-01 in FILETIME units
BUNDLE_ID_PATTERN = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")   # reverse-DNS, as `open -b` takes it

# ---- <sys/proc_info.h>, <sys/resource.h>, <sys/sysctl.h> ----

PROC_PIDLISTFDS = 1
PROC_PIDTBSDINFO = 3
PROC_PIDT_SHORTBSDINFO = 13
PROC_PIDFDVNODEPATHINFO = 2
PROX_FDTYPE_VNODE = 1
CTL_KERN, KERN_ARGMAX, KERN_PROCARGS2 = 1, 8, 49
SZOMB = 5                                       # pbi_status of a zombie
PATH_MAX = 4096                                 # PROC_PIDPATHINFO_MAXSIZE
BSDINFO = struct.Struct("<12I16s32s5IiQQ")      # struct proc_bsdinfo, 136 bytes
SHORTINFO = struct.Struct("<4I16s8I")           # struct proc_bsdshortinfo, 64 bytes
FDINFO = struct.Struct("<iI")                   # struct proc_fdinfo: fd, type
VNODE_PATH_INFO_SIZE = 1200                     # struct vnode_fdinfowithpath
VNODE_DEV_INO = struct.Struct("<I4xQ")          # vst_dev, vst_ino at offset 24
VNODE_DEV_INO_OFFSET = 24
VNODE_PATH_OFFSET = 176                         # vip_path
NOTE_EXIT = 0x80000000
MAX_ARGV_BYTES = 4 << 20


# ---- records (pure: bytes in, values out) ----

@dataclass(frozen=True)
class BsdInfo:
    pid: int
    ppid: int
    uid: int
    status: int
    name: str
    start_sec: int          # 0 when unknown (the short record has no start time)
    start_usec: int

    @property
    def alive(self) -> bool:
        return self.status != SZOMB

    @property
    def ctime(self) -> int:
        """The start time in FILETIME units, or 0 when it is not known."""
        if self.start_sec <= 0 or not 0 <= self.start_usec < 1_000_000:
            return 0
        return start_filetime(self.start_sec, self.start_usec)


def parse_bsdinfo(raw) -> BsdInfo | None:
    """A proc_bsdinfo (PROC_PIDTBSDINFO) record; None when `raw` is not one."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != BSDINFO.size:
        return None
    fields = BSDINFO.unpack(raw)
    status, pid, ppid, uid = fields[1], fields[3], fields[4], fields[5]
    comm, name = _c_string(fields[12]), _c_string(fields[13])
    return BsdInfo(pid, ppid, uid, status, name or comm, fields[20], fields[21])


def parse_shortinfo(raw) -> BsdInfo | None:
    """A proc_bsdshortinfo (PROC_PIDT_SHORTBSDINFO) record, which macOS gives for every user's process; it
    carries no start time."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != SHORTINFO.size:
        return None
    pid, ppid, _pgid, status, comm, _flags, uid = SHORTINFO.unpack(raw)[:7]
    return BsdInfo(pid, ppid, uid, status, _c_string(comm), 0, 0)


def parse_procargs2(raw) -> list[str] | None:
    """argv from a KERN_PROCARGS2 buffer (int argc, the exec path, NUL padding, then argc strings). The
    environment follows argv and is never read. None when the buffer is not well formed."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 4:
        return None
    argc = struct.unpack_from("<i", raw)[0]
    if not 0 <= argc <= 65536:
        return None
    _exec_path, found, rest = bytes(raw[4:]).partition(b"\0")
    if not found:
        return None
    parts = rest.lstrip(b"\0").split(b"\0")
    if argc and len(parts) <= argc:
        return None                       # cut off before the last argument ended
    return [os.fsdecode(part) for part in parts[:argc]]


def start_filetime(seconds: int, microseconds: int) -> int:
    """A Unix start time as FILETIME units, the unit of ProcessInfo.ctime."""
    return seconds * 10_000_000 + microseconds * 10 + UNIX_TO_FILETIME


def valid_pid(pid) -> bool:
    return type(pid) is int and 0 < pid <= MAX_PID


def _c_string(value: bytes) -> str:
    return os.fsdecode(value.split(b"\0", 1)[0])


# ---- bundle ids: the macOS "family" of a process ----

_bundle_cache: dict[str, tuple[tuple, str]] = {}
_bundle_lock = threading.Lock()
BUNDLE_CACHE_LIMIT = 512


def bundle_family(path) -> str | None:
    """The CFBundleIdentifier of the outermost `.app` bundle that `path` runs from (a helper inside
    Claude.app/Contents/Frameworks/… belongs to Claude.app); None outside any bundle; "?" when there is a
    bundle whose identity cannot be read. Each Info.plist is read once and cached while its inode, size and
    modification time stay the same."""
    if not path:
        return "?"
    parts = str(path).split("/")
    for index, part in enumerate(parts[:-1]):
        if part.lower().endswith(".app"):
            return _bundle_id("/".join(parts[:index + 1] + ["Contents", "Info.plist"]))
    return None


def _bundle_id(plist: str) -> str:
    try:
        status = os.stat(plist)
    except OSError:
        return "?"
    signature = (status.st_ino, status.st_mtime_ns, status.st_size)
    with _bundle_lock:
        cached = _bundle_cache.get(plist)
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        with open(plist, "rb") as handle:
            data = plistlib.load(handle)
        value = data.get("CFBundleIdentifier") if isinstance(data, dict) else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        value = None
    family = value if isinstance(value, str) and len(value) <= 255 and BUNDLE_ID_PATTERN.fullmatch(value) else "?"
    with _bundle_lock:
        if len(_bundle_cache) >= BUNDLE_CACHE_LIMIT:
            _bundle_cache.clear()
        _bundle_cache[plist] = (signature, family)
    return family


# ---- the libSystem binding ----

class ExitWatch:
    """kqueue EVFILT_PROC/NOTE_EXIT registrations (PLATFORM-MAC.md §1). Use as a context manager."""

    def __init__(self):
        self._queue = select.kqueue()
        self.exited: set[int] = set()

    def __enter__(self) -> "ExitWatch":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def watch(self, pid: int) -> bool:
        """Starts watching `pid`; False when it is already gone (or a zombie)."""
        event = select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                              select.KQ_NOTE_EXIT)
        try:
            self._queue.control([event], 0, 0)
        except ProcessLookupError:
            return False
        return True

    def drain(self, timeout: float) -> set[int]:
        """Waits up to `timeout` s for exits; returns every watched pid seen exiting so far."""
        for event in self._queue.control(None, 64, max(0.0, timeout)):
            if event.fflags & NOTE_EXIT:
                self.exited.add(event.ident)
        return self.exited

    def close(self) -> None:
        self._queue.close()


class LibSystem:
    """ctypes prototypes for libproc and sysctl. Readers answer None (with the errno where it matters)
    instead of raising, so callers decide what "unknown" means."""

    def __init__(self):
        lib = self.lib = ctypes.CDLL(None, use_errno=True)
        c_int, c_void_p = ctypes.c_int, ctypes.c_void_p
        for name, argtypes in (
                ("proc_listallpids", [c_void_p, c_int]),
                ("proc_pidinfo", [c_int, c_int, ctypes.c_uint64, c_void_p, c_int]),
                ("proc_pidfdinfo", [c_int, c_int, c_int, c_void_p, c_int]),
                ("proc_pidpath", [c_int, c_void_p, ctypes.c_uint32]),
                ("sysctl", [ctypes.POINTER(c_int), ctypes.c_uint, c_void_p, ctypes.POINTER(ctypes.c_size_t),
                            c_void_p, ctypes.c_size_t])):
            function = getattr(lib, name)
            function.restype, function.argtypes = c_int, argtypes
        mib = (c_int * 2)(CTL_KERN, KERN_ARGMAX)
        argmax, size = c_int(), ctypes.c_size_t(ctypes.sizeof(c_int))
        if lib.sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0 or argmax.value <= 0:
            raise DeckError("macOS did not say how long a command line can be.", OS_ERROR)
        self.argmax = min(argmax.value, MAX_ARGV_BYTES)

    def listpids(self) -> list[int]:
        for _ in range(3):
            count = self.lib.proc_listallpids(None, 0)
            if count <= 0:
                break
            buffer = (ctypes.c_int * (count + 256))()
            found = self.lib.proc_listallpids(buffer, ctypes.sizeof(buffer))
            if 0 < found < len(buffer):
                return [pid for pid in buffer[:found] if pid > 0]
        raise DeckError("macOS could not list the running applications. Try again.", OS_ERROR)

    def _pidinfo(self, pid: int, flavor: int, size: int) -> tuple[bytes | None, int]:
        buffer = ctypes.create_string_buffer(size)
        ctypes.set_errno(0)
        filled = self.lib.proc_pidinfo(pid, flavor, 0, buffer, size)
        if filled != size:
            return None, ctypes.get_errno() or errno.ESRCH
        return buffer.raw, 0

    def bsdinfo(self, pid: int) -> tuple[bytes | None, int]:
        """The full record and 0, or None and the errno (ESRCH: gone; EPERM: another user's process)."""
        return self._pidinfo(pid, PROC_PIDTBSDINFO, BSDINFO.size)

    def shortinfo(self, pid: int) -> bytes | None:
        return self._pidinfo(pid, PROC_PIDT_SHORTBSDINFO, SHORTINFO.size)[0]

    def pidpath(self, pid: int, buffer=None) -> str | None:
        """The executable's resolved path. `buffer` (PATH_MAX bytes) may be reused by one thread's loop."""
        buffer = buffer if buffer is not None else ctypes.create_string_buffer(PATH_MAX)
        length = self.lib.proc_pidpath(pid, buffer, PATH_MAX)
        return os.fsdecode(buffer.raw[:length]) if length > 0 else None

    def procargs(self, pid: int) -> bytes | None:
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        buffer = ctypes.create_string_buffer(self.argmax)
        size = ctypes.c_size_t(self.argmax)
        if self.lib.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
        return buffer.raw[:size.value]

    def fds(self, pid: int) -> tuple[list[tuple[int, int]] | None, int]:
        """(fd, type) of every open descriptor; (None, errno) when they cannot be read."""
        ctypes.set_errno(0)
        needed = self.lib.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, None, 0)
        for _ in range(3):
            if needed <= 0:
                return None, ctypes.get_errno() or errno.ESRCH
            size = needed + 64 * FDINFO.size
            buffer = ctypes.create_string_buffer(size)
            ctypes.set_errno(0)
            filled = self.lib.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, buffer, size)
            if filled <= 0:
                return None, ctypes.get_errno() or errno.ESRCH
            if filled < size:
                return [FDINFO.unpack_from(buffer.raw, offset) for offset in range(0, filled, FDINFO.size)], 0
            needed = size * 2
        return None, errno.EOVERFLOW

    def fdinfo(self, pid: int, fd: int, flavor: int, size: int) -> bytes | None:
        buffer = ctypes.create_string_buffer(size)
        return buffer.raw if self.lib.proc_pidfdinfo(pid, fd, flavor, buffer, size) == size else None

    def exit_watch(self) -> ExitWatch:
        return ExitWatch()


_api: LibSystem | None = None
_api_lock = threading.Lock()


def api():
    """This process's libSystem binding, made on first use."""
    global _api
    with _api_lock:
        if _api is None:
            _api = LibSystem()
        return _api


def use_api(replacement):
    """Puts a stand-in for the binding in place (tests); returns the previous one, for restoring."""
    global _api
    with _api_lock:
        previous, _api = _api, replacement
        return previous


# ---- questions callers ask ----

def start_time(pid) -> tuple[str, int | None]:
    """("alive", FILETIME start), ("gone", None) for an exited, reaped or zombie process, or
    ("unreadable", None) when macOS will not say (another user's process)."""
    if not valid_pid(pid):
        return "gone", None
    raw, error = api().bsdinfo(pid)
    info = parse_bsdinfo(raw) if raw is not None else None
    if info is None:
        return ("gone", None) if raw is None and error == errno.ESRCH else ("unreadable", None)
    if info.pid != pid or not info.alive:
        return "gone", None
    return ("alive", info.ctime) if info.ctime else ("unreadable", None)


def creation_time(pid) -> int | None:
    """The start time (FILETIME units) of a running process; None when it is gone, a zombie or unreadable."""
    try:
        state, ctime = start_time(pid)
    except (DeckError, OSError):
        return None
    return ctime if state == "alive" else None


def parent_of(pid) -> int | None:
    info = parse_shortinfo(api().shortinfo(pid)) if valid_pid(pid) else None
    return info.ppid if info is not None and info.pid == pid and info.alive else None


def children_by_parent() -> dict[int, list[int]]:
    """Every live process's pid, grouped by its parent's pid (one pass over the table)."""
    lib = api()
    children: dict[int, list[int]] = {}
    for pid in lib.listpids():
        info = parse_shortinfo(lib.shortinfo(pid))
        if info is not None and info.pid == pid and info.alive:
            children.setdefault(info.ppid, []).append(pid)
    return children


def descends_from_self(pid: int, ctime: int) -> bool:
    """True when `pid` (started at `ctime`) is this process's child, grandchild, …, by a chain in which every
    parent started before its child (an older "child" means the parent's pid was reused)."""
    try:
        me = os.getpid()
        if creation_time(pid) != ctime:
            return False
        current, current_ctime = pid, ctime
        for _ in range(64):
            parent = parent_of(current)
            parent_ctime = creation_time(parent) if parent else None
            if parent_ctime is None or parent_ctime >= current_ctime:
                return False
            if parent == me:
                return True
            current, current_ctime = parent, parent_ctime
    except (DeckError, OSError):
        return False
    return False


def open_paths(pid) -> list[str] | None:
    """The paths of the files `pid` holds open, with the links in them resolved; None when it refused or has gone."""
    lib = api()
    descriptors, _error = lib.fds(pid)
    if descriptors is None:
        return None
    paths = []
    for fd, kind in descriptors:
        if kind == PROX_FDTYPE_VNODE:
            raw = lib.fdinfo(pid, fd, PROC_PIDFDVNODEPATHINFO, VNODE_PATH_INFO_SIZE)
            if raw is not None:
                paths.append(os.fsdecode(raw[VNODE_PATH_OFFSET:].partition(b"\0")[0]))
    return paths


def file_holders(path) -> set[int] | None:
    """Pids of this user's processes that hold `path` open (matched by device and inode); None when that
    cannot be said for certain (the file vanished mid-scan, or one of this user's processes refused)."""
    try:
        status = os.stat(path)
        lib = api()
        pids = lib.listpids()
    except (DeckError, OSError):
        return None
    key = (status.st_dev & 0xFFFFFFFF, status.st_ino)
    me = os.getuid()
    holders = set()
    for pid in pids:
        descriptors, error = lib.fds(pid)
        if descriptors is None:
            if error == errno.ESRCH:
                continue
            owner = parse_shortinfo(lib.shortinfo(pid))
            if owner is None or owner.uid != me:
                continue                      # gone, or another user's: it cannot open this user's files
            return None
        for fd, kind in descriptors:
            if kind != PROX_FDTYPE_VNODE:
                continue
            raw = lib.fdinfo(pid, fd, PROC_PIDFDVNODEPATHINFO, VNODE_PATH_INFO_SIZE)
            if raw is not None and VNODE_DEV_INO.unpack_from(raw, VNODE_DEV_INO_OFFSET) == key:
                holders.add(pid)
                break
    return holders
