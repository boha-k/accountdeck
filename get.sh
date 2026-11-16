#!/bin/bash
# Installs or updates AccountDeck from its latest GitHub release, keeping every saved account.
#
#     curl -fsSL https://github.com/boha-k/accountdeck/releases/latest/download/get.sh | bash
#     bash "$HOME/Library/Application Support/AccountDeck2/app/get.sh"        (an installed copy updates itself)
#
# This file is published with every release, beside the package (and it is in the package, which is how an installed
# copy has it): the one-line installer runs released code, never whatever is on the main branch.
# It downloads AccountDeck2-mac.tar.gz and its .sha256 from the release, checks the SHA-256, checks that the
# archive holds only AccountDeck2-mac/ and plain files, unpacks it into a temporary folder, runs its install.sh
# and removes the folder. It uses only what every Mac has (curl, tar, shasum, mktemp): no Python, no git.
# The whole body is in main(), called on the last line, so a download cut short runs nothing.
#
# ACCOUNTDECK_RELEASE_BASE (tests and mirrors) is the folder holding the two files, used instead of GitHub:
# https:// or file:/// only. The version it holds is then read from its package.
REPO=boha-k/accountdeck

PACKAGE=AccountDeck2-mac.tar.gz
TOP=AccountDeck2-mac
GET_URL="https://github.com/$REPO/releases/latest/download/get.sh"
MODE=install
WANT=""

usage() {
    cat <<EOF
AccountDeck installer and updater (github.com/$REPO)

  curl -fsSL $GET_URL | bash
  bash "\$HOME/Library/Application Support/AccountDeck2/app/get.sh"

Downloads the latest release, checks its SHA-256 and runs its install.sh. Running it again
updates AccountDeck; your saved accounts stay.

Options:
  --version X.Y.Z   install that release instead of the latest
  --check           only say whether a newer release exists; changes nothing.
                    Exit status: 0 up to date, 10 a newer release exists (or none is
                    installed), 1 could not tell
  -h, --help        show this help

Every other option goes to install.sh:
  --start-at-login  open AccountDeck when you log in
  --no-launch       do not open AccountDeck at the end
  --dry-run         say what would happen and change nothing
  --allow-downgrade install a release older than the installed one
  --skip-tests      skip the installer's test of the new code
  --prefix DIR      install below DIR instead of your home folder (tests; opens nothing)

With curl, put options after "bash -s --":
  curl -fsSL $GET_URL | bash -s -- --start-at-login
EOF
}

say() { printf '%s\n' "$*"; }

fail() {   # the one way out on an error of get.sh's own: install.sh has not run, so nothing was changed
    if [ "$MODE" = check ]; then
        printf 'Could not tell whether a newer AccountDeck exists: %s\n' "$1" >&2
    else
        printf 'Stopped: %s Nothing was changed.\n' "$1" >&2
    fi
    exit 1
}

valid_version() {   # dotted numbers, like 2.2.0
    case "$1" in ''|*[!0-9.]*|.*|*.|*..*) return 1 ;; esac
}

compare_versions() {   # prints <, = or > for A against B, as dotted numbers (2.10.0 > 2.9.1; 2.2 = 2.2.0)
    local a=$1 b=$2 x y
    while [ -n "$a" ] || [ -n "$b" ]; do
        x=${a%%.*}; y=${b%%.*}
        if [ "$x" = "$a" ]; then a=""; else a=${a#*.}; fi
        if [ "$y" = "$b" ]; then b=""; else b=${b#*.}; fi
        x=$((10#${x:-0})); y=$((10#${y:-0}))
        if [ "$x" -lt "$y" ]; then say "<"; return 0; fi
        if [ "$x" -gt "$y" ]; then say ">"; return 0; fi
    done
    say "="
}

code_version() {   # the __version__ line of FOLDER/accountdeck/__init__.py, as install.sh reads it
    sed -n 's/^__version__ = "\([^"]*\)".*$/\1/p' "$1/accountdeck/__init__.py" 2>/dev/null | head -n 1
}

curl_options() {   # https:// only over TLS 1.2+, redirects included (GitHub sends assets elsewhere); file:// for tests
    case "$1" in
    https://*) CURL=(curl -fsSL --proto =https --proto-redir =https --tlsv1.2 --retry 2 --connect-timeout 30 --max-time 300) ;;
    file:///*) CURL=(curl -fsS --proto =file) ;;
    *) fail "ACCOUNTDECK_RELEASE_BASE must start with https:// or file:///." ;;
    esac
}

download() {   # download URL OUT WORK: true, or false with why in DOWNLOAD_ERROR ("missing": HTTP 404, or no such file://)
    local code status=0
    code=$("${CURL[@]}" -w '%{http_code}' -o "$2" "$1" 2>"$3/curl.err") || status=$?
    [ "$status" -ne 0 ] || return 0
    case "$code:$status" in
    404:*|*:37) DOWNLOAD_ERROR=missing ;;
    *) DOWNLOAD_ERROR=$(sed '/^[[:space:]]*$/d' "$3/curl.err" | tail -n 1) ;;
    esac
    return 1
}

no_release() {   # no_release BASE: there is no such release, in words
    if [ -n "${ACCOUNTDECK_RELEASE_BASE:-}" ]; then
        say "$1 holds no $PACKAGE."
    elif [ -n "$WANT" ]; then
        say "AccountDeck $WANT was not found: github.com/$REPO has no release v$WANT. The releases are listed at https://github.com/$REPO/releases."
    else
        say "github.com/$REPO has no release of AccountDeck to download: none has been published yet, or the repository is not public."
    fi
}

fetch_package() {   # fetch_package BASE WORK: the two files from BASE, checked, unpacked as WORK/unpacked/AccountDeck2-mac
    local base=$1 work=$2 want name have listing line
    curl_options "$base"
    if ! download "$base/$PACKAGE" "$work/$PACKAGE" "$work"; then
        [ "$DOWNLOAD_ERROR" != missing ] || fail "$(no_release "$base")"
        fail "could not download $base/$PACKAGE (${DOWNLOAD_ERROR:-no answer}). Check the internet connection and try again."
    fi
    if ! download "$base/$PACKAGE.sha256" "$work/$PACKAGE.sha256" "$work"; then
        [ "$DOWNLOAD_ERROR" != missing ] || fail "the release at $base has no $PACKAGE.sha256 to check the download against."
        fail "could not download $base/$PACKAGE.sha256 (${DOWNLOAD_ERROR:-no answer}). Check the internet connection and try again."
    fi

    want="" name=""
    read -r want name < "$work/$PACKAGE.sha256" || true
    want=$(printf '%s' "$want" | tr 'A-F' 'a-f')
    case "$want" in *[!0-9a-f]*) want="" ;; esac
    [ "${#want}" -eq 64 ] && [ "${name#\*}" = "$PACKAGE" ] || fail "$PACKAGE.sha256 is not a SHA-256 line for $PACKAGE."
    have=$(shasum -a 256 "$work/$PACKAGE" | cut -d ' ' -f 1)
    [ "$have" = "$want" ] || fail "the download does not match its SHA-256 (expected $want, got $have)."

    # Only AccountDeck2-mac/ and below, no absolute path, no "..", only plain files and folders (no links).
    listing=$(tar -tzf "$work/$PACKAGE" 2>/dev/null) && [ -n "$listing" ] || fail "the download is not a readable archive."
    while IFS= read -r line; do
        case "$line" in "$TOP"|"$TOP/"|"$TOP/"*) ;; *) fail "the archive holds a path outside $TOP/: $line" ;; esac
        case "/$line/" in */../*|*/./*) fail "the archive holds a path with . or ..: $line" ;; esac
    done <<< "$listing"
    listing=$(tar -tvzf "$work/$PACKAGE" 2>/dev/null) || fail "the download is not a readable archive."
    if grep -q -v '^[-d]' <<< "$listing"; then fail "the archive holds a link or a special file."; fi

    mkdir "$work/unpacked" || fail "no temporary folder could be made."
    tar -xzf "$work/$PACKAGE" -C "$work/unpacked" || fail "the archive could not be unpacked."
    [ -f "$work/unpacked/$TOP/install.sh" ] && [ -f "$work/unpacked/$TOP/accountdeck/__init__.py" ] \
        || fail "the archive is not an AccountDeck package (no install.sh)."
    PACKAGE_VERSION=$(code_version "$work/unpacked/$TOP")
    valid_version "$PACKAGE_VERSION" || fail "the package does not say which version it is."
    PACKAGE_SHA=$have
}

latest_tag() {   # the latest release's version, from where github.com/REPO/releases/latest redirects: no API, no token
    local answer where
    answer=$(curl -sS --proto =https --tlsv1.2 --connect-timeout 30 --max-time 60 -o /dev/null \
                  -w '%{http_code} %{redirect_url}' "https://github.com/$REPO/releases/latest" 2>/dev/null) \
        || fail "github.com did not answer. Check the internet connection and try again."
    where=${answer#* }
    case "$answer" in
    30[0-9]\ */releases/tag/*) ;;
    404\ *) fail "github.com/$REPO was not found (or it is private)." ;;
    30[0-9]\ *) fail "github.com/$REPO has no published release yet." ;;
    *) fail "github.com answered ${answer%% *} for github.com/$REPO/releases/latest." ;;
    esac
    where=${where##*/releases/tag/}
    where=${where#v}
    valid_version "$where" || fail "the latest release has an unexpected name ($where)."
    say "$where"
}

check() {   # check BASE WORK PREFIX: installed against latest, never contacting the running service
    local home=${3:-${HOME:-}} installed latest order update
    [ -n "$home" ] || fail "the home folder is not known (HOME is empty)."
    installed=$(code_version "$home/Library/Application Support/AccountDeck2/app")
    if [ -n "${ACCOUNTDECK_RELEASE_BASE:-}" ]; then
        fetch_package "$1" "$2"
        latest=$PACKAGE_VERSION
    else
        latest=$(latest_tag) || exit 1
    fi
    update='bash "$HOME/Library/Application Support/AccountDeck2/app/get.sh"'
    [ -z "$3" ] || update="bash \"$3/Library/Application Support/AccountDeck2/app/get.sh\" --prefix \"$3\""
    if ! valid_version "$installed"; then
        say "Installed: none"
        say "Latest:    $latest"
        say "AccountDeck is not installed. Install it with:"
        say "  curl -fsSL $GET_URL | bash"
        return 10
    fi
    say "Installed: $installed"
    say "Latest:    $latest"
    order=$(compare_versions "$installed" "$latest")
    case "$order" in
    "<") say "A newer release exists. Update with:"
         say "  $update"
         return 10 ;;
    "=") say "Up to date." ;;
    *)   say "Up to date (the installed version is newer than the latest release)." ;;
    esac
    return 0
}

main() {
    set -euo pipefail
    umask 022
    local want="" prefix="" base="" where="github.com/$REPO" others=0 status=0
    local pass=()
    while [ $# -gt 0 ]; do
        case "$1" in
        --version) [ $# -ge 2 ] || fail "--version needs a version, like 2.2.0."; want=$2; shift ;;
        --version=*) want=${1#--version=} ;;
        --check) MODE=check ;;
        -h|--help) usage; return 0 ;;
        --prefix)       # install.sh's options that take a folder: the folder travels with them
            [ $# -ge 2 ] || fail "--prefix needs a folder."
            prefix=$2; pass+=("$1" "$2"); shift ;;
        --prefix=*) prefix=${1#--prefix=}; pass+=("$1") ;;
        --downloads)
            [ $# -ge 2 ] || fail "--downloads needs a folder."
            pass+=("$1" "$2"); others=$((others + 1)); shift ;;
        *) pass+=("$1"); others=$((others + 1)) ;;
        esac
        shift
    done
    if [ "$MODE" = check ]; then
        [ -z "$want" ] && [ "$others" -eq 0 ] || fail "--check takes no other option but --prefix."
    fi
    want=${want#v}
    [ -z "$want" ] || valid_version "$want" || fail "--version needs a version like 2.2.0, not $want."
    WANT=$want

    [ "$(uname -s)" = Darwin ] || fail "AccountDeck runs on macOS only."
    [ "$(id -u)" -ne 0 ] || fail "run it as yourself, not with sudo."
    # hw.optional.arm64 is 1 on Apple silicon, also from a shell running under Rosetta.
    [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = 1 ] \
        || fail "AccountDeck needs a Mac with Apple silicon (M1 or newer); this Mac has an Intel processor."

    if [ -n "${ACCOUNTDECK_RELEASE_BASE:-}" ]; then
        base=${ACCOUNTDECK_RELEASE_BASE%/}
        where=$base
        curl_options "$base"
    elif [ -n "$want" ]; then
        base="https://github.com/$REPO/releases/download/v$want"
    else
        base="https://github.com/$REPO/releases/latest/download"
    fi

    WORK=$(mktemp -d "${TMPDIR:-/tmp}/accountdeck-get-XXXXXX") || fail "no temporary folder could be made."
    trap 'rm -rf "$WORK"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP

    if [ "$MODE" = check ]; then
        check "$base" "$WORK" "$prefix" || status=$?
        return "$status"
    fi

    if [ -n "$want" ]; then say "Downloading AccountDeck $want from $where..."
    else say "Downloading the latest AccountDeck from $where..."; fi
    fetch_package "$base" "$WORK"
    if [ -n "$want" ] && [ "$(compare_versions "$PACKAGE_VERSION" "$want")" != "=" ]; then
        fail "the release for $want holds version $PACKAGE_VERSION."
    fi
    say "Checked: AccountDeck $PACKAGE_VERSION, SHA-256 $PACKAGE_SHA."

    # install.sh says what it does, and on a failure whether anything changed (it puts the previous version back).
    # Piped from curl, stdin is the pipe: nothing of the installer may read from it.
    if [ -t 0 ]; then
        bash "$WORK/unpacked/$TOP/install.sh" ${pass[@]+"${pass[@]}"} || status=$?
    else
        bash "$WORK/unpacked/$TOP/install.sh" ${pass[@]+"${pass[@]}"} < /dev/null || status=$?
    fi
    return "$status"
}

main "$@"; exit
