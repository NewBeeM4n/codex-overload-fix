#!/bin/bash
# Codex Overload Fix - Linux uninstaller.
#
# Stops and disables the systemd --user service and removes the unit. The relay
# itself is a plain file in the repo; nothing else is installed, so there is no
# package to remove.
set -euo pipefail

SERVICE="codex-overload-fix"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$SERVICE.service"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/codex-overload-fix"
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
usage: uninstall-linux.sh [--purge] [--no-config]

  --purge       also delete the log and the state directory
  --no-config   leave config.toml alone; only stop and remove the relay
EOF
            exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

# ---------------------------------------------------------------- config first
# Everything below stops the relay, so the one step that can legitimately refuse
# runs first. If config.toml cannot be restored - because the relay entries were
# edited by hand since install - stopping the relay would leave Codex pointed at
# a dead port.
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

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    if systemctl --user disable --now "$SERVICE" >/dev/null 2>&1; then
        ok "systemctl --user disable --now $SERVICE"
    else
        say "service was not enabled"
    fi
else
    say "systemd --user is not available; skipping stop"
fi

if [ -f "$UNIT_PATH" ]; then
    rm -f "$UNIT_PATH"
    ok "removed $UNIT_PATH"
else
    say "no unit at $UNIT_PATH"
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
fi

# --linger turned lingering on, so --purge turns it back off. Not unconditional:
# lingering is machine-wide logind policy and the user may want it for other
# services that have nothing to do with this relay.
if [ "$PURGE" -eq 1 ]; then
    if command -v loginctl >/dev/null 2>&1; then
        linger_state="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo unknown)"
        if [ "$linger_state" = "yes" ]; then
            if loginctl disable-linger "$USER" 2>/dev/null; then
                ok "loginctl disable-linger $USER"
            else
                say "could not disable lingering; run: loginctl disable-linger $USER"
            fi
        else
            say "lingering was not enabled (nothing to undo)"
        fi
    fi
fi

if [ "$PURGE" -eq 1 ] && [ -d "$STATE_DIR" ]; then
    rm -rf "$STATE_DIR"
    ok "removed $STATE_DIR"
fi

cat <<'EOF'

The relay is stopped and will not start at login again.
Restart Codex to go back to a direct connection.
EOF
