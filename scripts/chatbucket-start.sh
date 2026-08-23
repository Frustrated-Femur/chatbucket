#!/usr/bin/env bash
# chatbucket-start — manual start guard for the Arch machine
#                    (front-door architecture).
#
# Arch runs ChatBucket manually (per ChatBucket_Networking_Architecture.md:
# "Arch — manual start only, no autostart"). This script is the one
# supported way to start it: it prevents accidentally launching two
# copies of main.py against the same state/ directory, which under the
# front-door architecture would cause the second copy to fail its
# port-5000 bind and exit — recoverable, but confusing. Catching it at
# the launcher level with a plain pidfile is cheaper than reading a
# port-bind failure out of the logs.
#
# Usage:
#   ./scripts/chatbucket-start.sh
#
# Exit codes:
#   0 — main.py (the front door) exited normally
#   1 — another instance is already running (pidfile present + alive)
#   2 — repo root not found / cd failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || { echo "cannot cd to $REPO_ROOT"; exit 2; }

PIDFILE="$REPO_ROOT/.chatbucket.pid"

# Stale-pidfile handling: if the file exists but the PID isn't alive,
# treat it as stale and clear it. Safe under the front door because a
# previous crashed front-door process leaves host-state.json in whatever
# state it wrote last, and a fresh start-up re-enters arbitration from
# scratch — same recovery semantics the front door itself relies on
# after any restart.
if [[ -f "$PIDFILE" ]]; then
    old_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
        echo "chatbucket already running (pid $old_pid). Refusing to start a second copy."
        exit 1
    fi
    rm -f "$PIDFILE"
fi

# Record our own PID (this shell), then exec main.py — main.py calls
# front_door.run() and stays as one long-lived Python process. Because
# the front door NEVER execv's itself (that was the old design; the
# whole point of the front door is that PID identity is stable), the
# `exec` here transfers the shell's PID to main.py once and it stays
# valid for the entire lifetime of the ChatBucket install. The pidfile
# needs no handoff / update / re-write logic — the PID we recorded
# above IS the PID that will hold port 5000 until the process exits.
echo "$$" > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

exec python3 main.py "$@"
