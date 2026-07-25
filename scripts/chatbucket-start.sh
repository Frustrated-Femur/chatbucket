#!/usr/bin/env bash
# chatbucket-start — manual start guard for the Arch machine.
#
# Arch runs ChatBucket manually (per ChatBucket_Networking_Architecture.md:
# "Arch — manual start only, no autostart, no doorman"). This script is
# the one supported way to start it: it prevents accidentally launching
# two copies of main.py against the same state/ directory (which would
# race on arbitration and produce duplicate host claims into
# host-state.json before losing one via Syncthing conflict resolution).
#
# Usage:
#   ./scripts/chatbucket-start.sh
#
# Exit codes:
#   0 — main.py exited normally
#   1 — another instance is already running (pidfile present + alive)
#   2 — repo root not found / cd failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || { echo "cannot cd to $REPO_ROOT"; exit 2; }

PIDFILE="$REPO_ROOT/.chatbucket.pid"

# Stale-pidfile handling: if the file exists but the PID isn't alive,
# treat it as stale and clear it. This is safe because arbitration.py
# already handles the "previous host crashed" case correctly — a fresh
# start-up here just re-enters arbitration.
if [[ -f "$PIDFILE" ]]; then
    old_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
        echo "chatbucket already running (pid $old_pid). Refusing to start a second copy."
        exit 1
    fi
    rm -f "$PIDFILE"
fi

# Record our own PID (this shell), then exec main.py — main.py will
# os.execv into gunicorn/doorman, inheriting this PID, so the pidfile
# stays valid across the process-image replacement.
echo "$$" > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

exec python3 main.py "$@"
