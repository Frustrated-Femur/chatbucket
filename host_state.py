"""
host_state.py — Read/write module for host-state.json (leader election pointer file).

Lives in the `state/` Syncthing folder (synced as `sync-state`, No File Versioning).
See ChatBucket_Networking_Architecture.md §3 for the full design this supports.

This module does exactly two things: read the current claim, and write a new
claim atomically. It contains NO arbitration logic (no tailscale status calls,
no health-check HTTP requests, no jitter/retry) — that logic consumes this
module's output but does not belong inside it. Keeping this file dumb and
small means it's trivially testable in isolation, which is the whole point
of building it before wiring it into anything else.
"""

import json
import os
import tempfile
from datetime import datetime, timezone

STATE_DIR  = "state"
STATE_FILE = os.path.join(STATE_DIR, "host-state.json")

VALID_ACTIONS = {"start", "stop"}


class HostStateError(Exception):
    """Raised when host-state.json exists but is unreadable/malformed."""
    pass


def read_state():
    """
    Read the current host-state.json.

    Returns a dict: {"action": "start"|"stop", "machine": str, "timestamp": str}
    Returns None if the file doesn't exist or is empty — this is a NORMAL
    condition (nobody has ever hosted yet), not an error, and callers should
    treat it the same as {"action": "stop"} per §3 step 2 of the arch doc.

    Raises HostStateError if the file exists but contains invalid JSON or is
    missing required fields — this IS an error state (corruption, partial
    write that somehow slipped past the atomic-write guard, manual edit
    gone wrong) and should NOT be silently treated as "nobody is hosting",
    because that would cause every machine to simultaneously self-elect
    host on top of a host that may actually be fine — the file is just
    unreadable. Callers should surface this loudly, not swallow it.
    """
    if not os.path.exists(STATE_FILE):
        return None

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError as e:
        raise HostStateError(f"Could not read {STATE_FILE}: {e}")

    if not raw:
        return None  # empty file — treat same as "no claim exists"

    try:
        state = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HostStateError(
            f"{STATE_FILE} contains invalid JSON — refusing to guess. "
            f"Parse error: {e}"
        )

    missing = [k for k in ("action", "machine", "timestamp") if k not in state]
    if missing:
        raise HostStateError(
            f"{STATE_FILE} is missing required field(s): {missing}"
        )

    if state["action"] not in VALID_ACTIONS:
        raise HostStateError(
            f"{STATE_FILE} has invalid action {state['action']!r}, "
            f"expected one of {VALID_ACTIONS}"
        )

    return state


def write_state(action, machine):
    """
    Atomically write a new host-state.json claiming `action` for `machine`.

    Atomicity matters here specifically because this file gets rewritten on
    every host transition, and a naive open()-write()-close() on the final
    path leaves a window where a crash (power loss, kill -9, Syncthing
    reading mid-write) can leave a truncated or half-written JSON file that
    every other machine's read_state() then fails to parse — at exactly the
    moment arbitration logic needs a clean answer most (right after a host
    transition). The fix: write to a temp file in the SAME directory, then
    os.replace() it over the target. os.replace is atomic on POSIX (and on
    Windows since Python 3.3+, which matters since Win1/Win2 run this too) —
    the destination file is either the fully-old version or the fully-new
    version, never a partial one, regardless of when a crash happens.

    Same-directory temp file is required, not incidental: os.replace's
    atomicity guarantee only holds within a single filesystem. A temp file
    in /tmp being moved into a Syncthing-synced folder on a different
    mount could silently fall back to non-atomic copy+delete on some
    platforms.
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {VALID_ACTIONS}, got {action!r}")
    if not machine or not isinstance(machine, str):
        raise ValueError(f"machine must be a non-empty string, got {machine!r}")

    os.makedirs(STATE_DIR, exist_ok=True)

    state = {
        "action": action,
        "machine": machine,
        # Microsecond precision, not just seconds: arbitration.py's jitter
        # re-check (§3 step 4) detects "did the claim change while I slept"
        # via exact dict equality against a snapshot taken before jitter.
        # Second-precision timestamps let two genuinely distinct writes of
        # the same machine name within the same wall-clock second collide
        # into byte-identical dicts, which arbitration would then wrongly
        # read as "nothing changed" — a real, if narrow, split-brain risk
        # (most plausible right after a power-outage boot storm, where
        # multiple machines restart within the same second). Microsecond
        # resolution makes that collision astronomically unlikely instead
        # of merely unlikely.
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }

    # NamedTemporaryFile with delete=False: we need the file to survive past
    # the `with` block so os.replace can act on it afterward.
    fd, tmp_path = tempfile.mkstemp(
        dir=STATE_DIR, prefix=".host-state-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())  # force to disk before the rename — without
            # this, os.replace is atomic w.r.t. OTHER PROCESSES seeing a
            # consistent file, but the write itself could still be sitting
            # in OS page cache if the machine loses power before it's
            # flushed, in which case you could lose the write entirely
            # (revert to whatever the old file said) despite replace()
            # having "succeeded" from the calling process's point of view.
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        # Clean up the temp file on any failure so sync-state doesn't
        # accumulate orphaned .tmp files (and so those .tmp files don't
        # get synced by Syncthing as junk that other machines then see).
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return state


def is_claimed_by(state, machine):
    """Convenience check: does this state dict claim the given machine as host?"""
    return state is not None and state.get("action") == "start" and state.get("machine") == machine
