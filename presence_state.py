"""
presence_state.py — Per-user join/leave presence tracking.

Lives in the top-level `presence/` directory — a SEPARATE Syncthing folder
from `state/`. This split is deliberate and must not be collapsed: `state/`
is scoped exclusively to leader-election data (host-state.json) per
ChatBucket_Networking_Architecture.md §3/§9. Presence has nothing to do with
who is hosting — mixing the two would make state/'s sync scope and
conflict-handling assumptions ambiguous for two unrelated concerns.

One file per user: presence/<sanitized_username>.json. Written by whichever
machine is currently host (only the host ever accepts WS connections — see
the doorman/redirect design in the architecture doc), so under normal
operation only one machine writes a given user's file at a time. During a
host handoff, Syncthing propagation could theoretically overlap two
near-simultaneous writes from two machines; this module intentionally does
not attempt to resolve that beyond what the atomic single-file write already
guarantees (each individual write is torn-write-safe; if two different
machines both wrote before Syncthing converged, whichever write Syncthing
settles on last becomes the visible state). This is the same accepted-gap
posture already taken for host-state.json staleness in the architecture doc,
and it fails in the SAFE direction here: a stale presence file makes the
computed unread boundary too OLD, which flags a few extra messages as
unread rather than hiding genuinely-new ones. Proportionate for a 3-person
group; revisit only if it's ever actually observed causing a problem.

Schema written per user file:
    {
      "user":        "<username>",
      "last_joined": "<ISO8601 microsecond UTC>" | null,
      "last_left":   "<ISO8601 microsecond UTC>" | null
    }

This module does no arbitration, no message-store access, no HTTP — same
"keep it dumb and small, let callers own the logic" boundary host_state.py
draws for itself, and for the same reason (trivially testable in isolation).
"""

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone

PRESENCE_DIR = "presence"


class PresenceStateError(Exception):
    """Raised only for programmer-error-type misuse (bad username/status).
    NOT raised for corrupted/missing files on disk — see read_presence()."""
    pass


# ── per-username write locks ─────────────────────────────────────────────
# write_presence() does a read-modify-write (merge the new status into
# whatever's already on disk, so updating last_left doesn't clobber
# last_joined and vice versa). Two threads in THIS process racing on the
# same user's file — e.g. a quick double-connect from two tabs on one
# device — could otherwise interleave that read-modify-write and silently
# drop one of the two updates. A small per-username lock closes that
# window. Different users' writes stay fully parallel; only same-username
# writes ever contend. (Cross-process/cross-machine races during a host
# handoff are the accepted gap described above — a lock here can't help
# with those anyway.)
_write_locks = {}
_write_locks_guard = threading.Lock()


def _lock_for(username):
    key = _safe_filename(username)
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _write_locks[key] = lock
        return lock


def _safe_filename(username):
    safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(username or "")).strip("_")
    return safe or "unknown"


def _path_for(username):
    return os.path.join(PRESENCE_DIR, f"{_safe_filename(username)}.json")


def read_presence(username):
    """
    Read the presence record for `username`.

    Returns a dict {"user", "last_joined", "last_left"} or None if no
    record exists yet (brand new user) OR if the file is corrupted.

    Unlike host_state.py's read_state(), corruption here is NOT raised.
    A bad presence file should never block someone from joining chat — the
    worst-case consequence of treating it as "no record" is a slightly
    wrong unread boundary, not a split-brain hosting failure. That safety
    asymmetry vs. host_state.py is deliberate, not an oversight.
    """
    path = _path_for(username)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None

    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or "user" not in data:
        return None

    return data


def write_presence(username, status):
    """
    Atomically record that `username` just changed to `status`
    ("online" or "offline"), stamping the corresponding timestamp.

    status == "online"  → updates last_joined, leaves last_left untouched.
    status == "offline" → updates last_left,   leaves last_joined untouched.

    Same atomic-write pattern as host_state.py's write_state(): tempfile in
    the same directory + fsync + os.replace. Required here for the same
    reason — this file is Syncthing-synced, so a torn write risks another
    machine reading a corrupted mid-write copy.
    """
    if status not in ("online", "offline"):
        raise PresenceStateError(f"status must be 'online' or 'offline', got {status!r}")
    if not username or not isinstance(username, str):
        raise PresenceStateError(f"username must be a non-empty string, got {username!r}")

    os.makedirs(PRESENCE_DIR, exist_ok=True)

    with _lock_for(username):
        existing = read_presence(username) or {
            "user": username, "last_joined": None, "last_left": None
        }

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if status == "online":
            existing["last_joined"] = now_iso
        else:
            existing["last_left"] = now_iso
        existing["user"] = username

        path = _path_for(username)
        fd, tmp_path = tempfile.mkstemp(
            dir=PRESENCE_DIR, prefix=".presence-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(existing, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return existing


def last_left_epoch_ms(username):
    """
    Convenience: read this user's presence record and return their
    last_left timestamp as epoch milliseconds (matching JS's Date.now()/
    Date.parse() unit convention), or None if there's no record, or no
    recorded leave yet (e.g. a user who has only ever joined once and
    never disconnected — nothing to compute a boundary from).

    Centralizes the ISO-string → epoch-ms conversion here so server.py's
    route doesn't carry its own copy of this parsing logic.
    """
    record = read_presence(username)
    if not record or not record.get("last_left"):
        return None
    try:
        dt = datetime.strptime(record["last_left"], "%Y-%m-%dT%H:%M:%S.%fZ")
        dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None
