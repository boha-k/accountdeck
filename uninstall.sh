#!/bin/bash
# Removes AccountDeck 2 from this Mac user (the reverse of install.sh).
#
#     bash uninstall.sh [--delete-data] [--prefix DIR]
#
# Removes ~/Applications/AccountDeck 2.app, the runtime, the app code and the earlier app code install.sh kept
# (app-backups/) in ~/Library/Application Support/AccountDeck2, and the Open at login LaunchAgent. Your data
# (accounts, settings, history, and the sign-in snapshots install.sh took before updates, update-backups/) stays
# in ~/Library/Application Support/AccountDeck2 unless you pass --delete-data, which asks you to type "delete"
# first; there is no way back from that.
#
# Options:
#   --delete-data   also delete the data folder (update-backups/ with it), AccountDeck's logs, its lock file and
#                   the app's own macOS state (window position, dashboard web storage); asks first
#   --prefix DIR    uninstall what install.sh --prefix DIR installed (launchctl and Finder are left alone)
set -euo pipefail

LABEL=local.accountdeck2
BUNDLE_ID=local.accountdeck2
APP_NAME="AccountDeck 2"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

say() { printf '%s\n' "$*"; }
die() { printf 'AccountDeck 2 was not removed: %s\n' "$*" >&2; exit 1; }

main() {
    local delete_data=0 prefix="" manage=true base
    while [ $# -gt 0 ]; do
        case "$1" in
        --delete-data) delete_data=1 ;;
        --prefix) [ $# -ge 2 ] && [ -n "$2" ] || die "--prefix needs a folder."; prefix=$2; shift ;;
        --prefix=*) prefix=${1#--prefix=}; [ -n "$prefix" ] || die "--prefix needs a folder." ;;
        -h|--help) awk 'NR == 1 { next } /^set -euo pipefail/ { exit } { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "unknown option $1 (see bash uninstall.sh --help)." ;;
        esac
        shift
    done
    [ "$(uname -s)" = Darwin ] || die "this script is for macOS."
    [ "$(id -u)" -ne 0 ] || die "run it as yourself, not with sudo."
    if [ -n "$prefix" ]; then
        [ -d "$prefix" ] || die "$prefix does not exist."
        base=$(cd "$prefix" && pwd -P)
        manage=false
    else
        base=${HOME:-}
        [ -n "$base" ] && [ -d "$base" ] || die "your home folder was not found."
    fi
    local apps="$base/Applications"
    local support="$base/Library/Application Support/AccountDeck2"
    local agent="$base/Library/LaunchAgents/$LABEL.plist"
    local logs="$base/Library/Logs/AccountDeck2"
    local lock="$base/Library/Application Support/AccountDeck2-Owner.lock"
    # What macOS keeps for the app itself: its preferences (the dashboard window's place) and the web view's
    # storage and caches.
    local app_state=("$base/Library/Preferences/$BUNDLE_ID.plist" "$base/Library/WebKit/$BUNDLE_ID"
                     "$base/Library/Caches/$BUNDLE_ID" "$base/Library/HTTPStorages/$BUNDLE_ID")
    local app="$apps/$APP_NAME.app"
    local appdir="$support/app"

    local answer=""
    if [ "$delete_data" = 1 ]; then
        say "This deletes AccountDeck 2's data folder for good: every saved account sign-in, the settings, the"
        say "usage history and the sign-in snapshots in $support, and the logs in $logs."
        say "Accounts that are not signed in anywhere else cannot be recovered afterwards."
        printf 'Type delete to confirm: '
        read -r answer || true
        [ "$answer" = delete ] || die "you did not type delete. Nothing was changed."
    fi

    # Open at login: unloading the job also stops a copy that launchd started at login.
    if [ "$manage" = true ]; then
        launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    fi

    local waited=0
    while pgrep -U "$(id -u)" -f -- "$appdir/run.py" >/dev/null 2>&1; do
        [ "$waited" -lt 30 ] || die "$APP_NAME is running. Choose Quit in its menu bar menu, then run uninstall.sh again."
        sleep 1
        waited=$((waited + 1))
    done
    # Only now: an uninstall that stops above leaves AccountDeck installed, and it still opens at login.
    rm -f "$agent"

    if [ -e "$app" ]; then
        if [ "$manage" = true ] && [ -x "$LSREGISTER" ]; then
            "$LSREGISTER" -u "$app" >/dev/null 2>&1 || true
        fi
        rm -rf "$app"
        say "Removed $app"
    fi
    rm -rf "$apps/.$APP_NAME.app.new" "$app.old"
    if [ -d "$support" ]; then
        # app-backups/ holds only earlier app code; update-backups/ holds saved sign-ins, which are data.
        rm -rf "$support/runtime" "$support/runtime.new" "$support/runtime.old" \
               "$support/app" "$support/app.new" "$support/app.old" "$support/app-backups" "$support"/.install-* \
               "$support/.install.lock" "$support/update-backups"/.partial-*
        say "Removed the runtime, the app code and the earlier app code (app-backups) from $support"
    fi

    if [ "$delete_data" = 1 ]; then
        rm -rf "$support" "$logs" "${app_state[@]}"
        rm -f "$lock"
        say "Deleted the data folder, the logs and the app's own macOS state."
        say "The vault key stays in your login keychain as \"AccountDeck2 vault\"; it unlocks nothing without"
        say "the deleted data. Keychain Access can delete it."
    elif [ -d "$support" ]; then
        say "Your data was kept in $support (bash uninstall.sh --delete-data removes it)."
        if [ -d "$support/update-backups" ]; then
            say "The sign-in snapshots taken before updates were kept with it, in $support/update-backups."
        fi
    fi
    say "$APP_NAME is uninstalled."
}

main "$@"
exit 0
