"""The machine-wide owner lock (docs/v2 PLAN §9.1, docs/mac/PLAN.md §1.9).

One exclusive, non-blocking `fcntl.flock` on a file, held for the process lifetime. Never lockf or F_SETLK:
closing any other descriptor of the same file in this process would silently drop such a record lock
(docs/mac/PLATFORM-MAC.md §7). flock belongs to the open file, so a second OwnerLock on the same path is
refused even in the same process, and the kernel drops the lock when the process dies.
`core.OwnerLock` is this class.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

FILE_MODE = 0o600


class OwnerLock:
    """Machine-wide single-owner lock (§9.1): an exclusive lock on `path` for the process lifetime.
    Two AccountDeck 2 instances (or data dirs) can then never both own provider sign-ins; the one
    that cannot acquire it runs read-only."""

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    @property
    def held(self):
        return self._fd is not None

    def acquire(self):
        """Take the lock without waiting; False when another process or instance holds it."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:   # BlockingIOError when held elsewhere; any other doubt also means "not ours"
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self):
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
