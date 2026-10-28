#!/bin/bash
# Codex Overload Fix - macOS installer.
#
# Registers relay/relay.py as a per-user LaunchAgent. No admin rights, and no
# watchdog: launchd restarts the process itself (KeepAlive), and a LaunchAgent
# has no console window to flash. None of the Windows SW_HIDE machinery is
# needed here.
#
# Verification status: exercised end-to-end on macOS in CI
# (.github/workflows/macos.yml) -- launchctl accepts the plist, the agent
# reports state = running, the relay listens, and uninstall removes it.
set -euo pipefail

PORT=8787
PROXY=""
PYTHON_PATH=""
STATE_DIR="$HOME/Library/Application Support/codex-overload-fix"
NO_AUTOSTART=0
NO_CONFIG=0
DRY_RUN=0
LABEL="com.codex-overload-fix.relay"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  [ok] %s\n' "$*"; }
warn() { printf '  [!!] %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
usage: install-mac.sh [options]

  --port PORT         Local port for the relay (default 8787)
  --proxy URL         Upstream proxy, e.g. http://127.0.0.1:7890
  --python-path PATH  Use this interpreter instead of the first python3 on PATH
  --state-dir DIR     Where the log lives
  --no-autostart      Write the plist but do not load it now
  --no-config         Do not edit config.toml; print the snippet to paste instead
  --dry-run           Print the plist and the commands; change nothing
  -h, --help          This message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port)         [ $# -ge 2 ] || die "--port needs a value";         PORT="$2"; shift 2 ;;
        --proxy)        [ $# -ge 2 ] || die "--proxy needs a value";        PROXY="$2"; shift 2 ;;
        --python-path)  [ $# -ge 2 ] || die "--python-path needs a value";  PYTHON_PATH="$2"; shift 2 ;;
        --state-dir)    [ $# -ge 2 ] || die "--state-dir needs a value";    STATE_DIR="$2"; shift 2 ;;
        --no-autostart) NO_AUTOSTART=1; shift ;;
        --no-config)    NO_CONFIG=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option: $1 (try --help)" ;;
    esac
done

case "$PORT" in
    ''|*[!0-9]*) die "port must be a number: $PORT" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "port out of range: $PORT"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
RELAY="$ROOT/relay/relay.py"
CONFIG_PY="$ROOT/scripts/configure_codex.py"
[ -f "$RELAY" ] || die "relay/relay.py not found under $ROOT"
if [ "$NO_CONFIG" -eq 0 ] && [ ! -f "$CONFIG_PY" ]; then
    warn "configure_codex.py not found under $ROOT; running as if --no-config"
    NO_CONFIG=1
fi

# ---------------------------------------------------------------- find python
if [ -z "$PYTHON_PATH" ]; then
    PYTHON_PATH="$(command -v python3 || true)"
fi
[ -n "$PYTHON_PATH" ] || die "python3 not found. Install Python 3.9+ (brew install python, or python.org) and re-run."
[ -x "$PYTHON_PATH" ] || die "not executable: $PYTHON_PATH"
"$PYTHON_PATH" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9+ required; $PYTHON_PATH is $("$PYTHON_PATH" --version 2>&1)"
ok "Python $("$PYTHON_PATH" -c 'import sys; print("%d.%d" % sys.version_info[:2])') at $PYTHON_PATH"

LOG_PATH="$STATE_DIR/relay.log"
OUT_PATH="$STATE_DIR/relay.out"   # launchd's stdout/stderr, kept apart from the relay's own log

port_open() {
    "$PYTHON_PATH" - "$1" <<'PY' >/dev/null 2>&1
import socket, sys
try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.5):
        pass
except OSError:
    raise SystemExit(1)
PY
}

# ---------------------------------------------------------------- dry run
if [ "$DRY_RUN" -eq 1 ]; then
    say "port:      $PORT"
    say "relay:     $RELAY"
    say "log:       $LOG_PATH"
    say "plist:     $PLIST_PATH"
    say "label:     $LABEL"
    say "proxy:     ${PROXY:-(none)}"
    say ""
    if [ "$NO_CONFIG" -eq 1 ]; then
        say "config:    not edited (--no-config)"
    else
        say "config:    $PYTHON_PATH $CONFIG_PY install --port $PORT"
    fi
    say ""
    say "launchctl bootout gui/$UID/$LABEL  (ignore failure)"
    say "launchctl bootstrap gui/$UID $PLIST_PATH"
    say ""
    say "plist that would be written:"
    say "----------------------------------------"
    DRY=1 LABEL="$LABEL" PLIST_PATH="$PLIST_PATH" PY_PATH="$PYTHON_PATH" RELAY="$RELAY" \
        PORT="$PORT" LOG_PATH="$LOG_PATH" OUT_PATH="$OUT_PATH" STATE_DIR="$STATE_DIR" PROXY="$PROXY" \
        "$PYTHON_PATH" - <<'PY'
import os, plistlib
e = os.environ
args = [e["PY_PATH"], e["RELAY"], "--port", e["PORT"], "--log", e["LOG_PATH"],
        "--max-attempts", "30", "--backoff-base", "1", "--backoff-max", "8",
        "--total-budget", "300", "--commit-deadline", "120"]
if e.get("PROXY"):
    args += ["--proxy", e["PROXY"]]
plist = {
    "Label": e["LABEL"],
    "ProgramArguments": args,
    "WorkingDirectory": e["STATE_DIR"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "ProcessType": "Background",
    "StandardOutPath": e["OUT_PATH"],
    "StandardErrorPath": e["OUT_PATH"],
}
print(plistlib.dumps(plist).decode())
PY
    exit 0
fi

# ---------------------------------------------------------------- config check
# Runs before the plist is written or launchctl is touched. configure_codex.py
# refuses anything it does not fully understand - a provider table nested under
# a header, a hand-edited relay table, a file it cannot parse - and if it
# refuses, nothing should have been installed. A relay running with Codex still
# pointed at the old route is worse than no install at all: it looks like it
# worked.
if [ "$NO_CONFIG" -eq 0 ]; then
    say "Checking config.toml..."
    if ! "$PYTHON_PATH" "$CONFIG_PY" check --port "$PORT"; then
        die "config.toml was not changed (see above). Fix that, or re-run with --no-config to install the relay only."
    fi
    ok "config.toml can be edited safely"
fi

mkdir -p "$STATE_DIR" "$(dirname "$PLIST_PATH")"

# ---------------------------------------------------------------- write plist
LABEL="$LABEL" PLIST_PATH="$PLIST_PATH" PY_PATH="$PYTHON_PATH" RELAY="$RELAY" \
    PORT="$PORT" LOG_PATH="$LOG_PATH" OUT_PATH="$OUT_PATH" STATE_DIR="$STATE_DIR" PROXY="$PROXY" \
    "$PYTHON_PATH" - <<'PY'
import os, plistlib
e = os.environ
args = [e["PY_PATH"], e["RELAY"], "--port", e["PORT"], "--log", e["LOG_PATH"],
        "--max-attempts", "30", "--backoff-base", "1", "--backoff-max", "8",
        "--total-budget", "300", "--commit-deadline", "120"]
if e.get("PROXY"):
    args += ["--proxy", e["PROXY"]]
plist = {
    "Label": e["LABEL"],
    "ProgramArguments": args,
    "WorkingDirectory": e["STATE_DIR"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "ProcessType": "Background",
    "StandardOutPath": e["OUT_PATH"],
    "StandardErrorPath": e["OUT_PATH"],
}
with open(e["PLIST_PATH"], "wb") as fh:
    plistlib.dump(plist, fh)
PY
ok "plist      $PLIST_PATH"

# ---------------------------------------------------------------- load it
if [ "$NO_AUTOSTART" -eq 0 ]; then
    launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
    if launchctl bootstrap "gui/$UID" "$PLIST_PATH" >/dev/null 2>&1; then
        ok "launchctl bootstrap gui/$UID"
    else
        warn "launchctl bootstrap failed, trying the legacy loader"
        launchctl load -w "$PLIST_PATH" || die "could not load $PLIST_PATH"
        ok "launchctl load -w"
    fi

    # A cold start on a slow or freshly booted machine can take well over five
    # seconds: this waits up to 30.
    i=0
    while [ "$i" -lt 120 ]; do
        if port_open "$PORT"; then
            ok "listening on 127.0.0.1:$PORT"
            break
        fi
        i=$((i + 1))
        sleep 0.25
    done
    if [ "$i" -ge 120 ]; then
        warn "not listening after 30s - check $LOG_PATH"
        launchctl print "gui/$UID/$LABEL" 2>/dev/null | sed -n '1,5p' >&2 || true
    fi
else
    say "plist written; not loaded (--no-autostart)"
fi

# ---------------------------------------------------------------- config
# CODEX_HOME is what Codex itself honours; configure_codex.py resolves it too.
CONFIG_PATH="${CODEX_HOME:-$HOME/.codex}/config.toml"
MANUAL="$NO_CONFIG"
if [ "$MANUAL" -eq 0 ]; then
    say "Pointing Codex at the relay..."
    if "$PYTHON_PATH" "$CONFIG_PY" install --port "$PORT"; then
        ok "config.toml points at 127.0.0.1:$PORT"
    else
        warn "could not edit config.toml automatically; add the block below by hand"
        MANUAL=1
    fi
fi

if [ "$MANUAL" -eq 1 ]; then
    cat <<EOF

Add this to $CONFIG_PATH, then restart Codex:

model_provider = "openai-retry"

[model_providers.openai-retry]
name = "OpenAI (retry)"
base_url = "http://127.0.0.1:$PORT"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 20
request_max_retries = 10

Keep a backup of config.toml before editing.
EOF
else
    printf '\nThen fully restart Codex.\n'
fi

cat <<EOF

Log:     $LOG_PATH
Stop:    bash scripts/uninstall-mac.sh

If you move or delete this folder, re-run the installer; launchd points at the
absolute path of relay/relay.py.
EOF