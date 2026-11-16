#!/bin/bash
# Checks the built release package (tools/build_mac_package.sh) without touching the real home: no LaunchAgent, no
# login item, no Keychain, no app launch, no real accounts. Everything runs with a temporary HOME and TMPDIR, in safe
# mode, and installs only below temporary --prefix folders.
#
#     tools/mac_package_test.sh [--downloads DIR]
#
# 1. The package holds exactly the release's file set (below) and no data file, and its .sha256 matches.
# 2. Its install.sh installs into an empty folder: bundle, icon, the app's signed python copy, launcher, install
#    record; the installed app folder holds the package's files and the installer's three records, nothing else;
#    the app refuses to run in safe mode.
# 3. dist/get.sh, the release's third file, is the package's own get.sh. Given a file:// release folder made from
#    dist/, it checks the SHA-256 and installs (read from stdin, as `curl …/releases/latest/download/get.sh | bash`
#    runs it), and --check answers 0 or 10. It refuses, with nothing installed: a release that holds no package, a
#    wrong checksum, an archive with a path outside AccountDeck2-mac/ or with "..", a link in the archive, and a
#    release whose version is not the one asked for.
#
# --downloads DIR is passed to install.sh (the pinned runtime files, still SHA-256 checked); without it the
# installer downloads them (about 30 MB) for each of the two installs.
set -euo pipefail
export COPYFILE_DISABLE=1

REPO=$(cd "$(dirname "$0")/.." && pwd)
PACKAGE="$REPO/dist/AccountDeck2-mac.tar.gz"
NAME=AccountDeck2-mac
# PLAN §4, kept here on purpose as well as in build_mac_package.sh: a change to what ships has to change both.
TOP_FILES=(run.py make_icon.py AccountDeck2.icns requirements.txt install.sh uninstall.sh get.sh README.md CHANGELOG.md
           LICENSE THIRD_PARTY_NOTICES.md)
INSTALL_RECORDS='install\.json|build\.json|\.source-sha256'       # what install.sh adds to the app folder
GET="$REPO/dist/get.sh"              # what the one-line installer downloads and runs
[ -f "$PACKAGE" ] && [ -f "$PACKAGE.sha256" ] && [ -f "$GET" ] \
    || { echo "Build the package first: tools/build_mac_package.sh" >&2; exit 1; }
extra=()
if [ "${1-}" = --downloads ] && [ -n "${2-}" ]; then extra=(--downloads "$(cd "$2" && pwd)"); fi

fail() { echo "FAIL: $*" >&2; exit 1; }

T=$(mktemp -d "${TMPDIR:-/tmp}/accountdeck-package-test-XXXXXX")
trap 'rm -rf "$T"' EXIT
mkdir "$T/home" "$T/tmp"
hermetic() { env HOME="$T/home" TMPDIR="$T/tmp" ACCOUNTDECK_SAFE_MODE=1 "$@"; }

# ---- 1. the file set ----
(cd "$REPO/dist" && shasum -a 256 -c "${PACKAGE##*/}.sha256" >/dev/null) || fail "the package does not match its .sha256"
listing=$(tar -tzf "$PACKAGE")
outside=$(grep -v "^$NAME/" <<< "$listing" || true)
[ -z "$outside" ] || fail "entries outside $NAME/: $outside"
expected=$( { printf '%s\n' "${TOP_FILES[@]}"
              (cd "$REPO" && find accountdeck -type f ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name .DS_Store); } \
            | LC_ALL=C sort)
actual=$(grep -v '/$' <<< "$listing" | sed "s|^$NAME/||" | LC_ALL=C sort)
if [ "$expected" != "$actual" ]; then
    { diff <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") || true; } | sed 's/^</missing:/; s/^>/unexpected:/' | grep -v '^[0-9]' >&2
    fail "the package does not hold exactly the release's file set"
fi
private=$(grep -iE '\.(sqlite3?|db|dpapi|adeck|log|pem|key|p12)($|[-.])|(^|/)(runtime\.json|vault|auth\.json)(/|$)' <<< "$listing" || true)
[ -z "$private" ] || fail "data files in the package: $private"
echo "file set: exactly the $(grep -c . <<< "$actual") release files, no data file, SHA-256 as in the .sha256"

# ---- 2. install.sh from the unpacked package into an empty folder ----
tar -xzf "$PACKAGE" -C "$T"
hermetic bash "$T/$NAME/install.sh" --prefix "$T/root" --no-launch ${extra[@]+"${extra[@]}"}

SUPPORT="$T/root/Library/Application Support/AccountDeck2"
APP="$T/root/Applications/AccountDeck 2.app"
installed=$(cd "$SUPPORT/app" && find . -type f ! -path '*/__pycache__/*' | sed 's|^\./||' | grep -vxE "$INSTALL_RECORDS" \
            | LC_ALL=C sort)
if [ "$installed" != "$actual" ]; then
    { diff <(printf '%s\n' "$actual") <(printf '%s\n' "$installed") || true; } | sed 's/^</not installed:/; s/^>/extra:/' | grep -v '^[0-9]' >&2
    fail "the installed app folder does not hold exactly the package's files"
fi
echo "installed app folder: the package's files and the installer's records, nothing else"
env HOME="$T/home" TMPDIR="$T/tmp" "$SUPPORT/runtime/python/bin/python3.14" -I - "$T/root" <<'PYTHON'
import json, pathlib, plistlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
support = root / "Library/Application Support/AccountDeck2"
app = root / "Applications/AccountDeck 2.app"
info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
assert info["CFBundleIdentifier"] == "local.accountdeck2" and info["LSUIElement"] is True
launcher = app / "Contents/MacOS" / info["CFBundleExecutable"]
assert launcher.is_file() and launcher.stat().st_mode & 0o111
text = launcher.read_text()
assert "--app" in text and "run.py" in text and str(support / "app") in text
python = app / "Contents/MacOS/python3"
assert python.is_file() and not python.is_symlink(), "the app's python must be a signed copy, not a link"
assert f"home = {support / 'runtime/python/bin'}" in (app / "Contents/pyvenv.cfg").read_text()
assert (app / "Contents/Resources/AccountDeck2.icns").read_bytes()[:4] == b"icns"
record = json.loads((support / "app/install.json").read_text())
assert record["manage"] is False and pathlib.Path(record["launcher"]) == launcher
assert not (root / "Library/LaunchAgents/local.accountdeck2.plist").exists()
assert not (support / "accounts.sqlite3").exists()
print("bundle, icon, python copy, launcher and isolated install record: OK")
PYTHON
identity=$(codesign -dv "$APP/Contents/MacOS/python3" 2>&1 || true)
case "$identity" in *$'\nIdentifier=local.accountdeck2\n'*) ;;
    *) fail "the app's python is not signed with the bundle id" ;; esac
echo "code identity of the app's python: local.accountdeck2"
set +e
hermetic "$APP/Contents/MacOS/AccountDeck2" --app --no-browser > "$T/app.log" 2>&1
status=$?
set -e
[ "$status" -eq 3 ] || { cat "$T/app.log"; fail "--app returned $status, expected 3 (safe mode)"; }
[ ! -e "$SUPPORT/accounts.sqlite3" ]
echo "safe-mode --app refusal: exit 3; no database created"

# ---- 3. get.sh from a file:// release folder ----
cmp -s "$GET" "$T/$NAME/get.sh" || fail "dist/get.sh is not the get.sh in the package"
version=$(sed -n 's/^__version__ = "\([^"]*\)".*$/\1/p' "$T/$NAME/accountdeck/__init__.py")
release() {   # release FOLDER ARCHIVE [SHA256]: a release folder holding ARCHIVE and its .sha256 (or the SHA given)
    mkdir -p "$1"
    cp "$2" "$1/AccountDeck2-mac.tar.gz"
    printf '%s  AccountDeck2-mac.tar.gz\n' "${3:-$(shasum -a 256 "$2" | cut -d ' ' -f 1)}" > "$1/AccountDeck2-mac.tar.gz.sha256"
}
get() {   # get BASE ARGS…: the release's get.sh read from stdin by macOS's bash 3.2, as `curl … | bash -s -- ARGS` runs it
    local base=$1; shift
    hermetic env ACCOUNTDECK_RELEASE_BASE="file://$base" /bin/bash -s -- "$@" < "$GET" > "$T/get.log" 2>&1
}
refused() {   # refused WHAT BASE PREFIX TEXT [ARGS…]: get.sh exits 1 saying TEXT, and nothing was installed below PREFIX
    local what=$1 base=$2 prefix=$3 text=$4 status=0
    shift 4
    get "$base" --prefix "$prefix" --no-launch ${extra[@]+"${extra[@]}"} "$@" || status=$?
    [ "$status" -eq 1 ] || { cat "$T/get.log"; fail "get.sh with $what returned $status, expected 1"; }
    grep -qF "$text" "$T/get.log" || { cat "$T/get.log"; fail "get.sh with $what did not say: $text"; }
    grep -qF "Nothing was changed." "$T/get.log" || { cat "$T/get.log"; fail "get.sh with $what did not say nothing changed"; }
    [ ! -e "$prefix" ] || fail "get.sh with $what left something in $prefix"
    echo "get.sh refuses $what; nothing installed"
}

release "$T/release" "$PACKAGE"
get "$T/release" --prefix "$T/get-root" --no-launch ${extra[@]+"${extra[@]}"} || { cat "$T/get.log"; fail "get.sh did not install"; }
grep -qF "Checked: AccountDeck $version, SHA-256 $(cut -d ' ' -f 1 < "$PACKAGE.sha256")." "$T/get.log" \
    || { cat "$T/get.log"; fail "get.sh did not report the checked package"; }
GET_APP="$T/get-root/Library/Application Support/AccountDeck2/app"
[ "$(sed -n 's/^__version__ = "\([^"]*\)".*$/\1/p' "$GET_APP/accountdeck/__init__.py")" = "$version" ] \
    || fail "get.sh did not install version $version"
[ -f "$GET_APP/get.sh" ] && [ -d "$T/get-root/Applications/AccountDeck 2.app" ] || fail "get.sh's install is incomplete"
[ -z "$(ls -A "$T/tmp" | grep '^accountdeck-get-' || true)" ] || fail "get.sh left its temporary folder behind"
echo "dist/get.sh (the package's own, from stdin): SHA-256 checked, $version installed, temporary folder removed"

home_before=$(find "$T/home" | LC_ALL=C sort)
status=0; get "$T/release" --check --prefix "$T/get-root" || status=$?
[ "$status" -eq 0 ] && grep -q "Up to date" "$T/get.log" || { cat "$T/get.log"; fail "--check on $version returned $status, expected 0"; }
sed -i '' 's/^__version__ = ".*"/__version__ = "0.0.1"/' "$GET_APP/accountdeck/__init__.py"
status=0; get "$T/release" --check --prefix "$T/get-root" || status=$?
[ "$status" -eq 10 ] || { cat "$T/get.log"; fail "--check on 0.0.1 returned $status, expected 10"; }
status=0; get "$T/release" --check || status=$?
[ "$status" -eq 10 ] && grep -q "Installed: none" "$T/get.log" || { cat "$T/get.log"; fail "--check with nothing installed returned $status, expected 10"; }
[ "$(find "$T/home" | LC_ALL=C sort)" = "$home_before" ] || fail "--check wrote to the home folder"
echo "get.sh --check: 0 when up to date, 10 when older or not installed"

# The refusals: each with a fresh prefix that must still not exist afterwards.
mkdir "$T/no-release"
refused "a release that holds no package" "$T/no-release" "$T/no-0" "holds no AccountDeck2-mac.tar.gz"
release "$T/bad-sha" "$PACKAGE" "$(printf '0%.0s' {1..64})"
refused "a wrong checksum" "$T/bad-sha" "$T/no-1" "does not match its SHA-256"

mkdir -p "$T/evil/$NAME"
cp "$T/$NAME/install.sh" "$T/$NAME/get.sh" "$T/evil/$NAME/"
mkdir "$T/evil/$NAME/accountdeck"
cp "$T/$NAME/accountdeck/__init__.py" "$T/evil/$NAME/accountdeck/"
echo outside > "$T/evil/outside.txt"
(cd "$T/evil" && tar -czf "$T/evil-outside.tar.gz" "$NAME" outside.txt)
release "$T/bad-outside" "$T/evil-outside.tar.gz"
refused "a path outside $NAME/" "$T/bad-outside" "$T/no-2" "outside $NAME/"
(cd "$T/evil" && tar -czf "$T/evil-dotdot.tar.gz" -s ",^$NAME/get.sh\$,$NAME/../escape.sh," "$NAME")
tar -tzf "$T/evil-dotdot.tar.gz" | grep -qxF "$NAME/../escape.sh" || fail "the test archive with .. was not made"
release "$T/bad-dotdot" "$T/evil-dotdot.tar.gz"
refused "a path with .." "$T/bad-dotdot" "$T/no-3" "with . or .."
ln -s /etc/hosts "$T/evil/$NAME/hosts"
(cd "$T/evil" && tar -czf "$T/evil-link.tar.gz" "$NAME")
release "$T/bad-link" "$T/evil-link.tar.gz"
refused "a link in the archive" "$T/bad-link" "$T/no-4" "a link or a special file"
[ ! -e "$T/escape.sh" ] && [ ! -e "$T/tmp/escape.sh" ] && [ ! -e "$T/tmp/outside.txt" ] || fail "an archive was unpacked"
refused "another version than asked for" "$T/release" "$T/no-5" "holds version $version" --version 9.9.9

echo "package installation and get.sh: PASS ($(cut -d ' ' -f 1 < "$PACKAGE.sha256"))"
