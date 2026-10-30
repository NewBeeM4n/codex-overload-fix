#!/bin/bash
# Codex Overload Fix - macOS uninstaller.
#
# Unloads the LaunchAgent and removes the plist. The relay itself is a plain
# file in the repo; nothing else is installed, so there is no package to remove.
set -euo pipefail

LABEL="com.codex-overload-fix.relay"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_DIR="$HOME/Library/Application Support/codex-overload-fix"
PURGE=0
NO_CONFIG=0

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  [ok] %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --purge)     PURGE=1; shift ;;
        --no-config) NO_CONFIG=1; shift ;;
        -h|--help)
            cat <<'EOF'
usage: uninstall-mac.sh [--purge] [--no-config]

  --purge       also delete the log and the state directory
  --no-config   leave config.toml alone; only stop and remove the relay
EOF
            exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

# ---------------------------------------------------------------- config first
# Everything below unloads the agent, so the one step that can legitimately
# refuse runs first. If config.toml cannot be restored - because the relay
# entries were edited by hand since install - unloading would leave Codex
# pointed at a dead port.
if [ "$NO_CONFIG" -eq 0 ]; then
    ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
    CONFIG_PY="$ROOT/scripts/configure_codex.py"
    PYTHON_PATH="$(command -v python3 || true)"
    if [ -n "$PYTHON_PATH" ] && [ -f "$CONFIG_PY" ]; then
        say "Restoring config.toml..."
        if ! "$PYTHON_PATH" "$CONFIG_PY" remove; then
            printf '\nconfig.toml was not restored, so the relay is still running.\n' >&2
            printf 'Codex is still pointed at it; stopping the relay now would break Codex.\n' >&2
            printf 'Reconcile the entries above (or pass --no-config to leave config.toml\n' >&2
            printf 'alone), then run this uninstaller again.\n' >&2
            exit 1
        fi
    else
        die "python3 or configure_codex.py is missing; config.toml and the relay were left untouched"
    fi
fi

if launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1; then
    ok "launchctl bootout gui/$UID/$LABEL"
elif launchctl unload -w "$PLIST_PATH" >/dev/null 2>&1; then
    ok "launchctl unload -w"
else
    say "agent was not loaded"
fi

if [ -f "$PLIST_PATH" ]; then
    rm -f "$PLIST_PATH"
    ok "removed $PLIST_PATH"
else
    say "no plist at $PLIST_PATH"
fi

if [ "$PURGE" -eq 1 ] && [ -d "$STATE_DIR" ]; then
    rm -rf "$STATE_DIR"
    ok "removed $STATE_DIR"
fi

cat <<'EOF'

The relay is stopped and will not start at login again.
Restart Codex to go back to a direct connection.
EOF