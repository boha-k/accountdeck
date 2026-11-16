"""Exercise the installer's real lock functions with synthetic files and processes only."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def check(source: str, folder: Path, *, fallback: bool = False) -> None:
    folder.mkdir()
    script = folder / "install.sh"
    functions = source[source.index("take_lock() {"):source.index("\nplan() {")]
    if fallback:
        # Reproduce a macOS release without lockf's newer file-descriptor form.
        functions = functions.replace("/usr/bin/lockf -s -t 0 9", "(exit 64)")
    script.write_text('set -euo pipefail\n'
                      'die() { printf "%s\\n" "$*" >&2; exit 1; }\n'
                      + functions + '\nLOCK=$1\ntake_lock\nprintf "acquired\\n"\nread -r done\n')
    lock = folder / ".install.lock"
    guard = folder / ".install.lock.guard"
    env = {**os.environ, "ACCOUNTDECK_SAFE_MODE": "1"}

    def start():
        return subprocess.Popen(["/bin/bash", str(script), str(lock)], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def refused(process):
        out, err = process.communicate("done\n", timeout=10)
        assert process.returncode == 1 and "another install or update" in err, (out, err)

    # A process can be paused after acquiring the kernel lock but before writing its pid. Stale/empty
    # metadata must never let a contender move that lock away and enter the same update's staging.
    with guard.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for metadata in ("", "99999999\n"):
            lock.write_text(metadata)
            inode = guard.stat().st_ino
            refused(start())
            assert guard.stat().st_ino == inode and lock.read_text() == metadata

    holder = start()
    try:
        assert holder.stdout.readline().strip() == "acquired"
        # Independent kernel user agrees that the bash parent still owns the lock after its helper exits.
        with guard.open("a") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("the helper released the lock before the installer finished")
        contenders = [start() for _ in range(8)]
        try:
            for contender in contenders:
                refused(contender)
        finally:
            for contender in contenders:
                if contender.poll() is None:
                    contender.kill()
                contender.communicate()
    finally:
        holder.kill()   # no EXIT trap: a crashed installer leaves its pid file behind
        holder.communicate(timeout=10)

    successor = start()
    out, err = successor.communicate("done\n", timeout=10)
    assert successor.returncode == 0 and out == "acquired\n", (out, err)


if __name__ == "__main__":
    source = Path(sys.argv[1]).read_text()
    with tempfile.TemporaryDirectory(prefix="install-lock-") as temp:
        check(source, Path(temp) / "native")
        check(source, Path(temp) / "older-macos", fallback=True)
    print("install lock: contention, empty/stale metadata, crash recovery and older macOS fallback passed")
