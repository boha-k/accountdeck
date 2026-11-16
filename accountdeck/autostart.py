"""Open AccountDeck 2 at login: the user LaunchAgent of platform/autostart_mac.py.

The engine (the `start_on_login` setting), the menu bar's Start at Login item and `run.py --set-autostart`
use these five names; the implementation and its test seams (`Layout.sandbox`, a fake `launchctl=`) live in
platform/autostart_mac.py.
"""
from __future__ import annotations

from .platform.autostart_mac import Status, is_enabled, repair, set_enabled, status

__all__ = ["Status", "is_enabled", "repair", "set_enabled", "status"]
