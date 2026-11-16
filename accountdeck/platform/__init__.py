"""macOS integration (AGENTS.md). Process facts and control: `libproc` (read-only libSystem) and `procs_mac` (the
live controller, with procs.py); other machine facts: `facts_mac`; the Keychain: `keychain`, `credstore`; the
owner lock: `locks`; the app shell: `appkit` (Objective-C bridge, NSApplication), `menubar` (what the menu bar
item shows and does), `statusbar_mac` (the NSStatusItem), `dashboard_window` (the dashboard in a WKWebView
window), `notify_mac` (notifications); start at login: `autostart_mac`. Importing the package loads none of them.
"""
