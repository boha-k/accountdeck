"""Claude Code proper-lockfile protocol, adapted from errhythm/ccswap (MIT).

See THIRD_PARTY_NOTICES.md for attribution. No process probing or signalling.
"""
from contextlib import contextmanager
import os
import threading
import time

from .core import DeckError

TIMEOUT = 9.0
TOUCH_INTERVAL = 3.0


@contextmanager
def proper_lockfile(path, *, timeout=TIMEOUT, staleness=60.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            path.mkdir()
            owned = path.stat().st_ino
            break
        except FileExistsError:
            pass
        try:
            before = path.stat()
            if time.time() - before.st_mtime > staleness:
                # Recheck before removal; never remove files or nonempty directories.
                after = path.stat()
                if (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns):
                    path.rmdir()
                    continue
        except FileNotFoundError:
            continue
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise DeckError("Claude Code is renewing its login — try again in a few seconds", "busy")
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    stop = threading.Event()
    lost = threading.Event()

    def check():
        try:
            intact = path.stat().st_ino == owned
        except OSError:
            intact = False
        if not intact or lost.is_set():
            lost.set()
            raise DeckError("Claude Code's login lock changed. Try again.", "busy")

    def touch():
        while not stop.wait(TOUCH_INTERVAL):
            try:
                check()
                os.utime(path, None)
            except (OSError, DeckError):
                lost.set()
                return

    thread = threading.Thread(target=touch, name="ad2-cli-lock", daemon=True)
    thread.start()
    try:
        yield check
    finally:
        stop.set()
        thread.join(timeout=1)
        try:
            if path.stat().st_ino == owned:
                path.rmdir()
        except OSError:
            pass


@contextmanager
def credential_locks(paths, *, timeout=TIMEOUT, storage_write=False):
    """Claude Code's token-refresh locks, in its order: `<config>/.oauth_refresh.lock`, then the legacy
    `~/.claude.lock`. With `storage_write`, also its secure-storage write lock `<config>/.storage-write.lock`
    (stale after 15 s), which Claude Code 2.1.293 holds around every credential read-modify-write, a
    refresh's save included, so it is taken last. The keychain store needs it (platform/credstore.py)."""
    paths.require_cli_paths()
    with proper_lockfile(paths.claude_cli_refresh_lock, timeout=timeout) as primary:
        with proper_lockfile(paths.claude_cli_legacy_lock, timeout=timeout) as legacy:
            if not storage_write:
                def check():
                    primary()
                    legacy()
                yield check
                return
            with proper_lockfile(storage_write_lock(paths), timeout=timeout, staleness=STORAGE_STALE) as storage:
                def check_all():
                    primary()
                    legacy()
                    storage()
                yield check_all


STORAGE_STALE = 15.0


def storage_write_lock(paths):
    """Claude Code's secure-storage write lock: proper-lockfile on `<config>/.storage-write`."""
    return paths.claude_cli_home / ".storage-write.lock"


def config_lock(paths, *, timeout=TIMEOUT):
    paths.require_cli_paths()
    return proper_lockfile(paths.claude_cli_config_lock, timeout=timeout, staleness=10.0)
