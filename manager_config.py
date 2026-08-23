"""
manager_config.py — Machine-local, unsynced configuration file.

Lives at `manager_config.json` in the project root — this SAME file already
exists per the Rust Manager's own docs (§5 of ChatBucket_Manager_Architecture.md)
for holding the Syncthing API key. This module extends it with two new
booleans without disturbing that existing key or any others the Manager
writes.

CRITICAL: this file is NOT Syncthing-shared. Whether *this* machine wants
to auto-host is a per-machine policy choice, not shared state — syncing it
would risk two machines disagreeing about their own settings during
propagation, exactly the class of Syncthing-lag race this project already
guards against for other files. Do not add this file to any Syncthing
folder.

Boundary discipline (same as host_state.py / presence_state.py): this
module reads and writes a JSON file. It contains NO business logic — the
front door consumes these values, the Rust Manager writes them via the
control endpoint; neither role belongs here.
"""

import json
import os
import tempfile
import threading

CONFIG_FILE = "manager_config.json"

# Defaults if the file is absent or the keys aren't present. Chosen for
# safety, not convenience:
#   auto_host=False: a fresh install on a machine nobody's set up yet
#     should NOT silently start claiming host on the tailnet — that would
#     surprise other participants. Explicit opt-in via the Manager UI
#     (or a manual edit) is the correct posture for a state-changing
#     default. The Arch launcher can override this at start-time (see
#     the launcher note at the bottom of this module) since a manual
#     Arch invocation is itself the explicit opt-in.
#   take_host_on_crash=False: same reasoning, and it's meaningless
#     without auto_host anyway per the doc's truth table.
_DEFAULTS = {
    "auto_host": True,
    "take_host_on_crash": True,
}

# Serializes writes within THIS process. Cross-process/cross-machine
# racing isn't possible for this file since it's unsynced and only the
# front door + Rust Manager on this same machine ever touch it.
_write_lock = threading.Lock()


def _read_raw():
    """Read the whole config dict from disk, or {} if the file is
    missing/empty/corrupt. NEVER raises — a broken config file must not
    stop the front door from starting; it should fall through to defaults
    and continue, letting the user fix the file at their leisure."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def read():
    """
    Return the current config as a dict, filled in with defaults for any
    missing keys. Reads disk every call — this file changes rarely, and
    caching it in memory would just create a second-source-of-truth
    consistency bug the moment the Manager writes to it via a different
    code path (or a human hand-edits it).
    """
    data = _read_raw()
    merged = dict(_DEFAULTS)
    for k in _DEFAULTS:
        if k in data and isinstance(data[k], bool):
            merged[k] = data[k]
    return merged


def update(**changes):
    """
    Update one or more fields, preserving any OTHER keys in the file
    (e.g. the Syncthing API key the Rust Manager stores here). Atomic:
    tempfile + os.replace, same discipline as host_state.write_state.

    Only known keys with bool values are accepted; anything else raises
    ValueError. This is defensive — a stray update() call passing an
    unrecognized key almost certainly indicates a bug, not a feature.
    """
    for k, v in changes.items():
        if k not in _DEFAULTS:
            raise ValueError(f"Unknown config key: {k!r}")
        if not isinstance(v, bool):
            raise ValueError(f"{k} must be bool, got {type(v).__name__}")

    with _write_lock:
        current = _read_raw()
        current.update(changes)

        directory = os.path.dirname(os.path.abspath(CONFIG_FILE)) or "."
        fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=".manager-config-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, CONFIG_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    return read()
