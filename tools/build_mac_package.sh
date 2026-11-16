#!/bin/bash
# Builds the release's three files: dist/AccountDeck2-mac.tar.gz, dist/AccountDeck2-mac.tar.gz.sha256 and dist/get.sh.
#
#     tools/build_mac_package.sh
#
# The archive holds one folder, AccountDeck2-mac/, with exactly the files in SHIPS below (the release's file set:
# what install.sh copies into the app folder). The .sha256 is one line, "<sha256>  AccountDeck2-mac.tar.gz", the
# form get.sh checks. dist/get.sh is the package's own get.sh, published beside it: the one-line installer
# (curl -fsSL https://github.com/OWNER/REPO/releases/latest/download/get.sh | bash) runs that released copy.
# Names are sorted and times, owners and modes fixed (ustar, no extended attributes, gzip -n), so the same checkout
# gives the same SHA-256. Needs only the stock macOS tools: CI builds the release with it.
#
# The gate: the build stops on a listed file that is missing, and on anything private or unexpected in the staged
# tree, before an archive is written: data files (a database, vault records, runtime.json, logs, keys), links,
# file types the app does not ship, secret-shaped strings, home-folder paths other than the placeholders /Users/me,
# /Users/you and /Users/NAME, and e-mail addresses outside the example domains. It names the file and line, never
# the value.
set -euo pipefail
export COPYFILE_DISABLE=1           # no ._ AppleDouble files from macOS tar

REPO=$(cd "$(dirname "$0")/.." && pwd)
NAME=AccountDeck2-mac
OUT="$REPO/dist/$NAME.tar.gz"
# What ships, and nothing else: docs, tests, tools and the repository's own files stay in the repository.
SHIPS=(accountdeck run.py make_icon.py AccountDeck2.icns requirements.txt install.sh uninstall.sh get.sh
       README.md CHANGELOG.md LICENSE THIRD_PARTY_NOTICES.md)
EXECUTABLE=(install.sh uninstall.sh get.sh)
APP_TYPES=(py js css html svg)      # the only file types below accountdeck/ (code and the dashboard)
STAMP=202601010000                  # every entry's modification time, in UTC (touch -t)
# Names that only data or keys have: never in a package, wherever they sit.
DATA_NAMES=('*.sqlite' '*.sqlite3' '*.sqlite3-*' '*.sqlite-*' '*.db' '*.db-*' '*.dpapi' '*.adeck' runtime.json vault
            '*.log' '*.log.*' service.lock startup-error.txt install.json build.json .source-sha256 auth.json
            '.credentials*' '*.pem' '*.key' '*.p12' '.env' '.env.*' '*.lock')
SECRETS='(^|[^A-Za-z0-9_-])(sk-ant-[A-Za-z0-9_-]{20,}|sk-(proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35})|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
HOME_PATH='/Users/[^/[:space:]"'\''`,;:)(<>|]+'
HOME_ALLOWED=':/Users/(me|you|NAME)$'
EMAIL='[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
EMAIL_ALLOWED='@(([A-Za-z0-9-]+\.)*example\.(com|org|net|test)|[0-9]x\.png)$'   # the icon set's name@2x.png too

fail() { printf 'The package was not built: %s\n' "$1" >&2; exit 1; }

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/accountdeck-package-XXXXXX")
trap 'rm -rf "$STAGE" "$OUT.tmp" "$REPO/dist/get.sh.tmp"' EXIT
ROOT="$STAGE/$NAME"
mkdir "$ROOT"
mkdir -p "$REPO/dist"
rm -f "$OUT" "$OUT.sha256" "$REPO/dist/get.sh"     # a failed build leaves no package behind for a test to pick up

# ---- stage exactly SHIPS ----
for entry in "${SHIPS[@]}"; do
    source="$REPO/$entry"
    [ ! -L "$source" ] || fail "$entry is a link."
    if [ "$entry" = accountdeck ]; then
        [ -d "$source" ] || fail "the accountdeck folder is missing."
        (cd "$REPO" && tar -cf - --no-mac-metadata --no-xattrs --no-acls --no-fflags \
            --exclude __pycache__ --exclude '*.pyc' --exclude .DS_Store accountdeck) | (cd "$ROOT" && tar -xf -)
    else
        [ -f "$source" ] || fail "$entry is missing (every file of the release must be there)."
        cp -X "$source" "$ROOT/$entry"
    fi
done

# ---- the gate ----
problems=$(cd "$ROOT" && {
    set +e +o pipefail                  # a search that finds nothing is the good case here
    args=()
    for pattern in "${DATA_NAMES[@]}"; do args+=(-o -iname "$pattern"); done
    find . \( "${args[@]:1}" \) -print | sed 's/^/a data file: /'
    find . ! -type f ! -type d -print | sed 's/^/not a plain file: /'
    args=()
    for kind in "${APP_TYPES[@]}"; do args+=(! -name "*.$kind"); done
    find ./accountdeck -type f "${args[@]}" -print | sed 's/^/a file type the app does not ship: /'
    grep -rIEn "$SECRETS" . | cut -d : -f 1,2 | sed 's/^/a secret-shaped string: /'
    grep -rIEno "$HOME_PATH" . | grep -vE "$HOME_ALLOWED" | cut -d : -f 1,2 | sed 's/^/a home-folder path: /'
    grep -rIEno "$EMAIL" . | grep -vE "$EMAIL_ALLOWED" | cut -d : -f 1,2 | sed 's/^/an e-mail address: /'
    if grep -rlI $'\r' --include='*.sh' . | grep -q .; then echo "CRLF line ends in a shell script (bash stops at a CR)"; fi
    [ "$(head -c 4 AccountDeck2.icns)" = icns ] || echo "AccountDeck2.icns is not an icon file"
} | LC_ALL=C sort -u)
if [ -n "$problems" ]; then
    printf '%s\n' "$problems" | sed 's|: \./|: |' >&2
    echo "(Home paths in shipped files may only be /Users/me, /Users/you or /Users/NAME, and e-mail addresses only at" >&2
    echo " example.com, .org, .net or .test; a public address that has to ship goes into EMAIL_ALLOWED here.)" >&2
    fail "the staged tree holds something that must not ship (above)."
fi
version=$(sed -n 's/^__version__ = "\([^"]*\)".*$/\1/p' "$ROOT/accountdeck/__init__.py")
[ -n "$version" ] || fail "accountdeck/__init__.py has no __version__ line."

# ---- a reproducible archive ----
find "$ROOT" -type d -exec chmod 0755 {} +
find "$ROOT" -type f -exec chmod 0644 {} +
for entry in "${EXECUTABLE[@]}"; do chmod 0755 "$ROOT/$entry"; done
(cd "$STAGE" && find "$NAME" -exec env TZ=UTC touch -h -t "$STAMP" {} + \
    && find "$NAME" -print | LC_ALL=C sort > files.txt \
    && tar --format ustar --no-mac-metadata --no-xattrs --no-acls --no-fflags --uid 0 --gid 0 --numeric-owner \
           -n -cf - -T files.txt) | gzip -n -9 > "$OUT.tmp"
sha=$(shasum -a 256 "$OUT.tmp" | cut -d ' ' -f 1)
mv "$OUT.tmp" "$OUT"
printf '%s  %s\n' "$sha" "${OUT##*/}" > "$OUT.sha256"
cp -X "$ROOT/get.sh" "$REPO/dist/get.sh.tmp"       # the staged copy: byte for byte the one in the archive
chmod 0755 "$REPO/dist/get.sh.tmp"
mv "$REPO/dist/get.sh.tmp" "$REPO/dist/get.sh"
echo "Package: $OUT (AccountDeck $version, $(wc -c < "$OUT" | tr -d ' ') bytes, $(tar -tzf "$OUT" | grep -vc '/$') files)"
echo "SHA-256: $sha (also in ${OUT##*/}.sha256)"
echo "Installer: $REPO/dist/get.sh (published beside the package; the same file as the package's get.sh)"
