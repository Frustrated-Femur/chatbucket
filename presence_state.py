"""
presence_state.py — per-user online/offline presence tracking for ChatBucket.

This module is imported by server.py but was missing from the distributed
bundle, which made the entire backend fail to boot (ImportError on
`import presence_state`). It is intentionally small and dependency-free.

Responsibilities:
1. Persist each user's last-known presence ("online" / "offline") as one tiny
   JSON file per user under PRESENCE_DIR, so the state survives a server
   restart.
2. Record the exact epoch (ms) at which a user went fully OFFLINE. This is the
   "unread boundary" the Android app queries via GET /unread-boundary to place
   the "NEW MESSAGES" divider for a cold-open, even when the device was
   completely closed (and therefore never received the live websocket stream).

Design notes:
- All public functions are thread-safe (Flask runs threaded; the websocket
  handler and the /unread-boundary route run on different threads).
- File writes are atomic (write to a .tmp sibling then os.replace) so a crash
  mid-write can never leave a torn JSON file behind.
- Nothing here raises outward: a corrupt file or an OSError degrades to a
  sane default (offline / no boundary) instead of 500-ing a request.
"""

import os
import re
import json
import time
import secrets
import threading
from datetime import datetime, timezone

# Directory holding one <user>.json presence file per user. server.py adds
# this to its startup os.makedirs() loop.
PRESENCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presence")

_lock = threading.RLock()


def _ensure_dir():
    try:
        os.makedirs(PRESENCE_DIR, exist_ok=True)
    except Exception:
        pass


_ensure_dir()


def _sanitize_user(user):
    """Map a username to a safe filesystem leaf (one file per user)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(user or "").strip())
    return cleaned or "anonymous"


def _user_path(user):
    return os.path.join(PRESENCE_DIR, f"{_sanitize_user(user)}.json")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _now_epoch_ms():
    return int(time.time() * 1000)


def write_presence(user, status):
    """
    Record that `user` is now `status` ("online" or "offline").

    On a transition to offline we stamp `last_left_epoch_ms` — this is the
    value the Android client uses to decide "which messages arrived while I
    was gone". On a transition to online we leave the previous boundary in
    place (the client consumes it once at cold-open, then maintains its own
    high-water mark) but refresh `last_seen`.
    """
    if not user:
        return
    user = str(user).strip()
    if not user:
        return

    status = "online" if status == "online" else "offline"
    path = _user_path(user)

    with _lock:
        prev = {}
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    prev = json.load(f)
        except Exception:
            prev = {}

        record = dict(prev) if isinstance(prev, dict) else {}
        record["user"] = user
        record["status"] = status
        record["last_seen"] = _now_iso()

        if status == "offline":
            # Only (re)stamp the boundary on an actual offline transition; the
            # server can emit repeated "offline" writes (e.g. one per socket of
            # the same user closing) and we must not keep pushing the boundary
            # forward or the client would mark everything read.
            if prev.get("status") != "offline":
                record["last_left_epoch_ms"] = _now_epoch_ms()
        else:
            # Came back online: keep whatever last_left_epoch_ms existed so a
            # client that reconnects still sees the correct boundary.
            record.setdefault("last_left_epoch_ms", prev.get("last_left_epoch_ms"))

        tmp = f"{path}.tmp.{secrets.token_hex(4)}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f)
            os.replace(tmp, path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


def _read_record(user):
    if not user:
        return None
    path = _user_path(user)
    with _lock:
        try:
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None


def is_online(user):
    rec = _read_record(user)
    return bool(rec and rec.get("status") == "online")


def last_seen_iso(user):
    rec = _read_record(user)
    return rec.get("last_seen") if rec else None


def last_left_epoch_ms(user):
    """
    Return the epoch (ms) the user last went fully offline, or None when the
    user has never been seen going offline (or no record exists). The Android
    client treats None as "no server boundary — fall back to the locally
    remembered last-read marker".
    """
    rec = _read_record(user)
    if not rec:
        return None
    val = rec.get("last_left_epoch_ms")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None
