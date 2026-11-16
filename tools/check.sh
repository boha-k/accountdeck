#!/bin/bash
# The developer gate: lint, the unit tests in safe mode, the syntax of the shell scripts, the dashboard and the workflow
# files, a smoke run of every demo scenario with its /api/state checked against the contract, and the shape of the
# doctor's report. Nothing here touches the live service, the real accounts or the login keychain.
#
#     tools/check.sh [unittest arguments…]     e.g. tools/check.sh -k test_mac_platform
#
# ACCOUNTDECK_PYTHON names another Python 3.14 with cryptography (CI has its own); the installed runtime is the default.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
PY=${ACCOUNTDECK_PYTHON:-"$HOME/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14"}
[ -x "$PY" ] || { echo "the pinned runtime is missing: $PY" >&2; exit 1; }
cd "$REPO"

if [ -x .venv-dev/bin/ruff ]; then
    .venv-dev/bin/ruff check --select F,E9 accountdeck run.py make_icon.py tests
else
    echo "lint: skipped (no .venv-dev/bin/ruff here)"
fi

T=$(mktemp -d "${TMPDIR:-/tmp}/accountdeck-check-XXXXXX")
trap 'rm -rf "$T"' EXIT

# The released builds, for tests/test_data_compat.py: each must still open a folder this build has used (a rollback).
# Taken from their tags when this is a git checkout that has them; ACCOUNTDECK_BASELINES set by the caller wins.
if [ -z "${ACCOUNTDECK_BASELINES:-}" ]; then
    found=() names=()
    if [ -e .git ] && command -v git >/dev/null 2>&1; then
        for version in 2.0.0 2.1.0; do
            git rev-parse --verify --quiet "v$version^{commit}" >/dev/null || continue
            mkdir "$T/release-$version"
            git archive "v$version" | tar -x -C "$T/release-$version"
            found+=("$T/release-$version") names+=("$version")
        done
    fi
    if [ ${#found[@]} -gt 0 ]; then
        ACCOUNTDECK_BASELINES=$(IFS=:; echo "${found[*]}")
        echo "old builds checked: ${names[*]} (from their tags)"
    else
        echo "old builds: skipped (no tag v2.0.0 or v2.1.0 here: those releases predate this repository's history)"
    fi
else
    echo "old builds checked: those ACCOUNTDECK_BASELINES names"
fi
export ACCOUNTDECK_BASELINES

# Synthetic roots must sit under the temporary folder the tests see (procs.register_test_root).
env TMPDIR="$T" ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s -m unittest discover -s tests -t . "$@"

for script in install.sh uninstall.sh get.sh tools/*.sh; do
    if [ "$script" = get.sh ] && [ ! -f get.sh ]; then continue; fi       # get.sh is new; an older tree has none
    bash -n "$script" || { echo "FAIL: $script has a syntax error" >&2; exit 1; }
done
if command -v node >/dev/null 2>&1; then
    for script in accountdeck/web/js/*.js; do
        node --check "$script" || { echo "FAIL: $script has a syntax error" >&2; exit 1; }
    done
    echo "shell and dashboard syntax: PASS"
else
    echo "shell syntax: PASS; dashboard syntax: skipped (no node here)"
fi
# A workflow file that is not valid YAML fails as a run of its own, which no pull request shows; release.yml would
# first say so on a release tag.
if command -v ruby >/dev/null 2>&1; then
    for file in .github/workflows/*.yml .github/ISSUE_TEMPLATE/*.yml; do
        [ -f "$file" ] || continue
        ruby -ryaml -e 'begin; YAML.load_file(ARGV[0]); rescue Psych::SyntaxError => e; warn e.message; exit 1; end' "$file" \
            || { echo "FAIL: $file is not valid YAML" >&2; exit 1; }
    done
    echo "workflow file syntax: PASS"
else
    echo "workflow file syntax: skipped (no ruby here)"
fi

env TMPDIR="$T" ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s - <<'PYTHON'
"""Every scenario's demo: the real service on synthetic accounts, its page, /_health and, through the session
handshake, /api/state against the contract. Then the doctor's report for an empty root and for a running demo."""
import concurrent.futures
import glob
import http.cookiejar
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import urllib.request

from accountdeck import __version__, contract, fixtures

RUN = [sys.executable, "-E", "-s", "run.py"]


class Demo:
    def __init__(self, scenario, env=None):
        self.process = subprocess.Popen([*RUN, "--demo", scenario, "--port", "0", "--no-browser"], env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.timer = threading.Timer(30.0, self.process.kill)   # a demo that prints nothing must not hold the check
        self.timer.start()
        first = self.process.stdout.readline()
        found = re.search(r"(http://127\.0\.0\.1:(\d+))/#token=([\w-]+)", first)
        self.origin, self.port, self.token = found.groups() if found else (None, None, None)
        self.first = first.strip()[:200]

    def close(self):
        self.timer.cancel()
        self.process.terminate()
        try:
            self.process.wait(15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


def smoke(scenario):
    """What is wrong with this scenario's demo, as a list."""
    demo = Demo(scenario)
    try:
        if demo.origin is None:
            return [f"it did not start: {demo.first}"]
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        local.open(demo.origin + "/", timeout=30).read()
        with local.open(demo.origin + "/_health", timeout=30) as answer:
            health = json.load(answer)
        if health.get("app") != "accountdeck2" or health.get("version") != __version__ or "build" not in health:
            return [f"/_health: app {health.get('app')}, version {health.get('version')}, build {'build' in health}"]
        request = urllib.request.Request(demo.origin + "/api/session", data=json.dumps({"token": demo.token}).encode(), headers={
            "Origin": demo.origin, "X-AccountDeck": "1", "Content-Type": "application/json"})
        local.open(request, timeout=30).read()
        with local.open(demo.origin + "/api/state", timeout=60) as answer:
            return contract.validate_state(json.load(answer))
    except Exception as exc:
        return [f"{type(exc).__name__}: {exc}"]
    finally:
        demo.close()


def doctor(root, env=None):
    """What is wrong with the shape of the doctor's report for this root, as a list, and the report. It only has to
    finish: a root is free to be unhealthy, but no check of it may give up."""
    done = subprocess.run([*RUN, "--doctor", "--json", "--synthetic-root", str(root)], env=env, capture_output=True, text=True, timeout=60)
    if done.returncode not in (0, 1):
        return [f"exit {done.returncode}: {done.stderr.strip()[-300:]}"], None
    report = json.loads(done.stdout)
    problems = contract.validate_health({key: report.get(key) for key in ("ok", "checks", "at")})
    if done.returncode != (0 if report.get("ok") else 1):
        problems.append(f"exit {done.returncode} with ok={report.get('ok')}")
    if report.get("version") != __version__ or "build" not in report:
        problems.append("no version or build")
    problems += [check["text"] for check in report.get("checks", ()) if check["id"].endswith(".unchecked")]
    return problems, report


failed = False
scenarios = list(fixtures.SCENARIOS)
with concurrent.futures.ThreadPoolExecutor(8) as pool:
    for scenario, problems in zip(scenarios, pool.map(smoke, scenarios), strict=True):
        problems = problems + contract.validate_state(fixtures.example_state(scenario))
        if problems:
            failed = True
            print(f"FAIL: the demo {scenario}: {'; '.join(map(str, problems[:5]))}", file=sys.stderr)
if not failed:
    print(f"demo smoke: PASS ({len(scenarios)} scenarios, each state valid)")

problems, report = doctor(Path(os.environ["TMPDIR"]) / "doctor-empty")
if problems:
    failed = True
    print(f"FAIL: the doctor on an empty root: {'; '.join(problems[:5])}", file=sys.stderr)

# The doctor beside a running demo: the real /_health, the real database. A demo writes no runtime.json of its own.
folder = Path(os.environ["TMPDIR"]) / "doctor-demo"
folder.mkdir()
env = dict(os.environ, TMPDIR=str(folder))
demo = Demo("terminal", env)
try:
    root = Path(glob.glob(str(folder / "accountdeck2-demo-*"))[0])
    (root / "data" / "runtime.json").write_text(json.dumps({"app": "accountdeck2", "host": "127.0.0.1", "port": int(demo.port), "mode": "demo"}))
    problems, report = doctor(root, env)
    running = {check["id"]: check["level"] for check in report["checks"]}.get("service.running") if report else None
    if running != "ok":
        problems.append(f"the running demo counts as {running}")
finally:
    demo.close()
if problems:
    failed = True
    print(f"FAIL: the doctor beside a demo: {'; '.join(problems[:5])}", file=sys.stderr)
elif not failed:
    print("doctor report: PASS (empty root, running demo)")
sys.exit(1 if failed else 0)
PYTHON
