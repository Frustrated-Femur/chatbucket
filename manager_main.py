"""
manager_main.py — Manager entrypoint.

Two modes:
    python3 manager_main.py          -> opens the real pywebview window
    python3 manager_main.py --cli    -> stdout-only probe, no window

Lives at repo root, alongside main.py/arbitration.py/server.py.
"""
import argparse
import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

import psutil

_MANAGER_FILE = os.path.abspath(__file__)


def _find_repo_root(start_path):
    """
    Walk upward from start_path until a directory containing
    arbitration.py, host_state.py, AND main.py together is found —
    the repo root, identified by CONTENT, never by an assumed fixed
    directory depth.
    """
    current = os.path.abspath(start_path)
    if os.path.isfile(current):
        current = os.path.dirname(current)

    markers = ("arbitration.py", "host_state.py", "main.py")
    while True:
        if all(os.path.isfile(os.path.join(current, m)) for m in markers):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            raise RuntimeError(
                f"Could not locate the ChatBucket repo root (looking for "
                f"{markers}) starting from {start_path}"
            )
        current = parent


_REPO_ROOT = _find_repo_root(_MANAGER_FILE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MANAGER_DIR = os.path.dirname(_MANAGER_FILE)
_WEB_INDEX = os.path.join(_MANAGER_DIR, "web", "index.html")
_VERSION_FILE = os.path.join(_REPO_ROOT, "VERSION")
_BACKUP_DIR = os.path.join(_REPO_ROOT, ".update-backup")
# Repo root, deliberately NOT inside manager/: _backup_current_code()
# moves manager/ wholesale on every update (whole-directory match
# against _UPDATE_ALLOWLIST_DIRS, not a per-file suffix check), which
# would silently disappear a user's manually-entered Syncthing API key
# on the very next update if it lived there instead. A repo-root file
# named neither "VERSION" nor ending in .py/.txt is untouched by both
# the backup move and the extraction allow-list, with no changes
# needed to either.
_MANAGER_CONFIG_FILE = os.path.join(_REPO_ROOT, "manager_config.json")

# Was 20s, covering for gunicorn's own default --graceful-timeout
# (30s) never actually completing before we gave up — a WebSocket
# connection via flask_sock never voluntarily closes, so gunicorn's
# graceful wait was pure dead time on every single stop. main.py now
# passes --graceful-timeout 5 to gunicorn itself, so the real shutdown
# should land well under 10s; kept above that (not shaved to the bone)
# so a genuine slow case (e.g. a large in-flight /upload) doesn't get
# force-killed mid-completion.
STOP_GRACE_SECONDS = 10
START_GRACE_SECONDS = 12          # widened: gunicorn boot + arbitration jitter
                                  # regularly ate the previous 8s window on
                                  # slower disks, leaving the badge stuck at
                                  # STARTING even though the launch succeeded.
POLL_INTERVAL = 0.4
ARBITRATION_STALE_SECONDS = 45    # anything reporting "arbitrating" for
                                  # longer than this is treated as a stuck /
                                  # abandoned process, not a live launch — the
                                  # arbitration window itself is bounded by
                                  # main.py's health-check timeout, so a
                                  # multi-minute "arbitrating" state means the
                                  # process is wedged, not still deciding.

# Version-string lookup for the GitHub release check (§14). Kept as a
# constant here rather than a runtime input — the source-of-truth repo
# is fixed by Architecture doc §13 and shouldn't be user-configurable
# from inside the running app.
_GITHUB_REPO = "Frustrated-Femur/chatbucket"
_UPDATE_ALLOWLIST_FILES = ("VERSION",)
_UPDATE_ALLOWLIST_SUFFIXES = (".py", ".txt")
_UPDATE_ALLOWLIST_DIRS = ("manager", "web", "static", "scripts")
# Data directories that MUST NEVER be touched by an update extraction
# (§14 "Replace only code, never data"). Even if the release ZIP
# accidentally contains one of these, extraction skips it. This is a
# deny-list *in addition to* the allow-list above; the two together
# make it impossible for a rogue release archive to clobber chat data.
_UPDATE_DENY_DIRS = (
    "messages", "uploads", "gifs", "stickers", "sfx",
    "state", "presence", ".venv", ".update-backup", "__pycache__",
)

# Tray icon (§17 "not yet built" -> built 2026-07-28). Polls far more
# often than the window's own NORMAL_REFRESH_MS (15s, in web/index.html)
# because the tray's own poll is deliberately cheap — see
# _tray_role_state()'s docstring for exactly why it's safe to poll this
# much faster than the window does.
TRAY_POLL_SECONDS = 5

# Mirrors web/index.html's :root custom properties (--success/--warn/
# --danger/--text/--text-2/--text-3) rather than inventing a separate
# palette — same "reuse ChatBucket's own token values" call already
# made for the web UI per Architecture doc §15, extended here to the
# one UI surface that isn't HTML/CSS. Keys are role_state["state"]
# values, same vocabulary _derive_role_state() already returns.
_TRAY_STATE_COLORS = {
    "host":     "#4ade80",   # --success
    "client":   "#f5f5f5",   # --text
    "starting": "#b5b5b5",   # --text-2
    "idle":     "#7a7a7a",   # --text-3
    "stale":    "#fbbf24",   # --warn
    "conflict": "#ff6b6b",   # --danger
    "unknown":  "#ff6b6b",   # --danger
}
_TRAY_DEFAULT_COLOR = "#7a7a7a"

# Syncthing status (§17 "not yet built" -> built 2026-07-29). Folder ID
# per Architecture doc §10's filesystem tree (state/ IS "sync-state").
# Syncthing's own REST docs flag /rest/db/status as "expensive...use
# sparingly" -- throttled independently of, and slower than, the
# window's own 15s poll (get_status() calls this every poll tick, but
# the cache below skips the actual HTTP call until this many seconds
# have passed).
_SYNCTHING_FOLDER_ID = "sync-state"
_SYNCTHING_CHECK_MIN_INTERVAL_SECONDS = 30

import arbitration
import host_state
import main as cb_main


# ── path helpers ─────────────────────────────────────────────────────

def _norm(p):
    """
    Normalize a filesystem path for equality comparison. Handles case
    (Windows), separators, symlinks resolved, and trailing slashes.
    A plain `os.path.normcase(os.path.normpath(...))` misses the
    symlink case — on Arch, `/home/user` vs `/home/ankit` when one is
    a symlink to the other made cwd-based matching silently fail,
    which is one path by which the process detector went blind and
    the Role badge got stuck reporting STARTING.
    """
    if not p:
        return ""
    try:
        p = os.path.realpath(p)
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(p))


_REPO_ROOT_NORM = _norm(_REPO_ROOT)


# ── status probes ────────────────────────────────────────────────────

def get_host_state():
    try:
        return {"ok": True, "state": host_state.read_state()}
    except host_state.HostStateError as e:
        return {"ok": False, "error": str(e)}


def get_claimed_host_status(claimed_machine, my_machine_name):
    if claimed_machine == my_machine_name:
        return {"self": True}
    try:
        return {"online": arbitration.check_machine_online(claimed_machine)}
    except arbitration.ArbitrationError as e:
        return {"online": None, "error": str(e)}


def get_tailnet_peers():
    try:
        result = arbitration.list_tailnet_peers()
        # arbitration.list_tailnet_peers() now always returns a dict with
        # peers + hidden_count. Older shape (bare list) is tolerated so a
        # partial upgrade never crashes the Manager.
        if isinstance(result, dict):
            return {"ok": True, "peers": result.get("peers", []),
                    "hidden_count": result.get("hidden_count", 0)}
        return {"ok": True, "peers": list(result), "hidden_count": 0}
    except arbitration.ArbitrationError as e:
        return {"ok": False, "error": str(e)}


def _read_manager_config():
    """
    manager_config.json at repo root (see _MANAGER_CONFIG_FILE). A
    missing file is the expected default (nothing configured yet, per
    §5's "real, non-zero setup friction — budgeted honestly"), not an
    error — every caller treats a missing file the same as an empty {}.
    """
    if not os.path.isfile(_MANAGER_CONFIG_FILE):
        return {}
    try:
        with open(_MANAGER_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


_syncthing_cache = {"checked_at": 0.0, "result": None}


def get_syncthing_status():
    """
    Answers exactly one question, per Architecture doc §5: is the
    "sync-state" folder (the one holding host-state.json) actually in
    sync right now. One REST call (GET /rest/db/status?folder=sync-state),
    no transfer queues, no per-file progress, no device list — Syncthing
    already has a correct GUI for that at 127.0.0.1:8384.

    Response field meanings confirmed against Syncthing's own REST API
    docs (docs.syncthing.net/rest/db-status-get.html) rather than
    guessed: "state" is the folder's own reported state ("idle" when
    caught up); "needFiles"/"needBytes" are what's still out of sync;
    "pullErrors" is failed-sync count from the last operation.

    Returns one of:
        {"state": "not_configured"} — no manager_config.json, or no
            "syncthing_api_key" key in it. Expected default, not an
            error.
        {"state": "in_sync"}
        {"state": "syncing"}
        {"state": "error", "detail": "..."} — covers both a Syncthing-
            reported problem (pull errors / folder error state) and
            this call itself failing (wrong port, Syncthing not
            running, bad/revoked key) — one bucket on purpose, mirroring
            how role_state already collapses several distinct problems
            into CONFLICT/UNKNOWN rather than growing a field per cause.
    """
    now = time.time()
    cached = _syncthing_cache["result"]
    if cached is not None and now - _syncthing_cache["checked_at"] < _SYNCTHING_CHECK_MIN_INTERVAL_SECONDS:
        return cached

    def _cache_and_return(result):
        _syncthing_cache["result"] = result
        _syncthing_cache["checked_at"] = now
        return result

    config = _read_manager_config()
    api_key = config.get("syncthing_api_key")
    if not api_key:
        return _cache_and_return({"state": "not_configured"})

    base_url = config.get("syncthing_url", "http://127.0.0.1:8384").rstrip("/")
    url = f"{base_url}/rest/db/status?folder={_SYNCTHING_FOLDER_ID}"
    req = urllib.request.Request(url, headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return _cache_and_return({"state": "error", "detail": str(e)})

    pull_errors = data.get("pullErrors", 0)
    if pull_errors or data.get("state") == "error":
        detail = f"{pull_errors} pull error(s)" if pull_errors else "folder error"
        return _cache_and_return({"state": "error", "detail": detail})
    if data.get("state") == "idle" and data.get("needFiles", 0) == 0 and data.get("needBytes", 0) == 0:
        return _cache_and_return({"state": "in_sync"})
    return _cache_and_return({"state": "syncing"})


# ── process discovery ─────────────────────────────────────────────────

def _cmdline_matches(joined_lower, script_basename):
    """
    True iff the given cmdline actually invokes `script_basename` (e.g.
    `main.py`) — not merely mentions it as a substring. This closes a
    subtle blind spot the previous plain-substring check had: any tool
    that happened to reference `main.py` in an argument (an editor's
    open-file list, a linter run, a `grep main.py` piped into `ps`)
    would have matched. Requiring the token to appear as its own word
    (bounded by whitespace or the string edges, and either a bare
    basename or the tail of a path) is what makes this a discovery of
    the ChatBucket process specifically, not "anything mentioning the
    file."
    """
    if script_basename not in joined_lower:
        return False
    # Split on whitespace — cmdline tokens are argv, which psutil joins
    # with spaces. For each token, treat as either the exact basename or
    # a path ending in that basename.
    for token in joined_lower.split():
        tail = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if tail == script_basename:
            return True
    return False


def _classify_cmdline(joined_lower):
    """
    Bucket a joined lowercased cmdline into ChatBucket lifecycle role
    plus a finer sub-shape (gunicorn master vs worker, direct python
    server, doorman, arbitrating). Sub-shape matters for stop() —
    signalling a gunicorn worker is a no-op because the master just
    respawns it, so stop() must find the master.

    Returns (role, subshape) where:
        role     ∈ {"host", "client", "arbitrating", None}
        subshape ∈ {
            "gunicorn_master", "gunicorn_worker",
            "python_server", "doorman", "arbitrating", None
        }
    """
    is_gunicorn = "gunicorn" in joined_lower and "server:app" in joined_lower
    if is_gunicorn:
        # Gunicorn workers include "worker" in their argv (they are
        # renamed via setproctitle to include a marker) — but the most
        # reliable, always-there indicator is "--worker-class" NOT being
        # in the worker's argv. The master invokes with -k/-w/-b flags;
        # the workers just show `gunicorn: worker [server:app]`. The
        # cheap, robust discriminator: look for the boot flags.
        looks_like_master = ("-k" in joined_lower.split()
                             or "--worker-class" in joined_lower
                             or "-w" in joined_lower.split()
                             or "-b" in joined_lower.split()
                             or "--bind" in joined_lower)
        return ("host", "gunicorn_master" if looks_like_master else "gunicorn_worker")
    if "server.py" in joined_lower:
        return ("host", "python_server")
    if _cmdline_matches(joined_lower, "doorman.py"):
        return ("client", "doorman")
    if _cmdline_matches(joined_lower, "main.py"):
        return ("arbitrating", "arbitrating")
    return (None, None)


def _iter_chatbucket_procs():
    """
    Yield every psutil process that looks like a ChatBucket lifecycle
    process — including workers, the arbitrating pre-execv shape, and
    any duplicates. Higher-level callers (find_chatbucket_process,
    stop) then reduce this set to a single canonical target.

    Yields dicts: {pid, role, subshape, cmdline, create_time, cwd, ppid}
    """
    for proc in psutil.process_iter(["pid", "ppid", "cwd", "cmdline", "create_time"]):
        try:
            info = proc.info
            cwd = info.get("cwd") or ""
            cmdline = info.get("cmdline") or []
            create_time = info.get("create_time") or 0
            ppid = info.get("ppid") or 0
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        if not cmdline:
            continue

        cwd_matches = bool(cwd) and _norm(cwd) == _REPO_ROOT_NORM
        joined = " ".join(cmdline).lower()

        script_under_repo = False
        for token in cmdline:
            if not token:
                continue
            if "/" in token or "\\" in token or token.endswith(".py"):
                abs_token = token if os.path.isabs(token) else os.path.join(cwd or _REPO_ROOT, token)
                if _norm(abs_token).startswith(_REPO_ROOT_NORM + os.sep) or _norm(abs_token) == _REPO_ROOT_NORM:
                    script_under_repo = True
                    break

        if not (cwd_matches or script_under_repo):
            continue

        role, subshape = _classify_cmdline(joined)
        if role is None:
            continue

        yield {
            "pid": info["pid"],
            "ppid": ppid,
            "role": role,
            "subshape": subshape,
            "cmdline": cmdline,
            "create_time": create_time,
            "cwd": cwd,
        }


def find_chatbucket_process():
    """
    Matches on repo cwd OR a cmdline that clearly references a script
    under the repo root — checked in lifecycle order:

      1. "main.py"                    -> role "arbitrating" (pre-exec —
         jitter + health-check window; a real, ongoing ChatBucket
         instance, just not yet resolved to host or client)
      2. "server.py" / gunicorn+server:app -> role "host"  (post-exec)
      3. "doorman.py"                 -> role "client" (post-exec)

    All three are the SAME logical instance across os.execv()'s process-
    image replacement (PID is preserved by execv, never changes) — this
    function just has to recognize whichever shape it currently is.

    When multiple gunicorn-shaped processes exist, prefer the MASTER
    (the process with `-b`/`-k`/`-w` flags in its argv) over a worker.
    stop() needs the master's pid — signalling a worker is a no-op
    because gunicorn respawns it, which is exactly the "Graceful stop
    did not complete within 8s" symptom seen in the wild.

    A process reporting the "arbitrating" role for longer than
    ARBITRATION_STALE_SECONDS is treated as stale and hidden from the
    result.
    """
    candidates = []
    now = time.time()

    for proc in _iter_chatbucket_procs():
        # Suppress stuck-arbitrating leftovers.
        if proc["role"] == "arbitrating" and proc["create_time"]:
            if now - proc["create_time"] > ARBITRATION_STALE_SECONDS:
                continue
        candidates.append(proc)

    if not candidates:
        return None

    # Ranking:
    #   priority 0 = gunicorn master or direct python server or doorman
    #   priority 1 = gunicorn worker  (visible but not the canonical target)
    #   priority 2 = arbitrating
    def rank(c):
        role = c["role"]
        sub = c["subshape"]
        if role in ("host", "client"):
            if sub == "gunicorn_worker":
                return 1
            return 0
        if role == "arbitrating":
            return 2
        return 3

    candidates.sort(key=lambda c: (rank(c), -c["create_time"]))
    winner = candidates[0]
    return {
        "pid": winner["pid"],
        "role": winner["role"],
        "cmdline": winner["cmdline"],
        "subshape": winner["subshape"],
    }


def _venv_python():
    if os.name == "nt":
        return os.path.join(_REPO_ROOT, ".venv", "Scripts", "python.exe")
    return os.path.join(_REPO_ROOT, ".venv", "bin", "python")


def _wait_for_exit(pid, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not psutil.pid_exists(pid):
            return True
        try:
            # Reap zombie state: a psutil.Process in ZOMBIE status still
            # answers pid_exists() as True on Linux until the parent
            # wait()s it, which would make the stop() verification
            # falsely time out even though the process is functionally
            # gone. Treating zombie as "exited" matches how a user
            # perceives it.
            p = psutil.Process(pid)
            if p.status() == psutil.STATUS_ZOMBIE:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return True
        time.sleep(POLL_INTERVAL)
    return not psutil.pid_exists(pid)


def _wait_for_all_gone(pids, timeout):
    """
    Wait until every pid in `pids` is either gone or a zombie. Used
    after signalling a process group so stop() can verify the ENTIRE
    lifecycle group is down, not just the master — a lingering worker
    with port 5000 still bound is functionally "still running" to the
    user even if the master has exited.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = []
        for pid in pids:
            if not psutil.pid_exists(pid):
                continue
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    continue
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            remaining.append(pid)
        if not remaining:
            return True
        time.sleep(POLL_INTERVAL)
    return all(not psutil.pid_exists(pid) for pid in pids)


def _wait_for_role(pid, timeout):
    """
    Polls find_chatbucket_process() until it reports OUR pid resolved
    to a real role (host/client), or the timeout elapses. Returns
    "host"/"client" if resolved, None otherwise.

    Also succeeds when find_chatbucket_process() returns a DIFFERENT
    pid whose create_time is close to ours — this handles the edge
    case where the launched python process spawns a supervisor (or
    gunicorn's master forks a worker) and the visible "ChatBucket"
    process is the child, not the pid Popen originally returned.
    """
    deadline = time.time() + timeout
    try:
        my_create = psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        my_create = None

    while time.time() < deadline:
        proc = find_chatbucket_process()
        if proc is not None and proc["role"] in ("host", "client"):
            if proc["pid"] == pid:
                return proc["role"]
            if my_create is not None:
                try:
                    other_create = psutil.Process(proc["pid"]).create_time()
                    if abs(other_create - my_create) < START_GRACE_SECONDS:
                        return proc["role"]
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        time.sleep(POLL_INTERVAL)
    return None


def _send_ctrl_break_windows(pid):
    """
    Only reliably works if the target was created with
    CREATE_NEW_PROCESS_GROUP (true for anything start() spawns). Return
    value is NOT trusted as proof — stop() always verifies actual exit
    and force-kills with a visible warning if this didn't work.
    Unverified on real Windows (no Windows test target available).
    """
    CTRL_BREAK_EVENT = 1
    try:
        result = ctypes.windll.kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)
        return bool(result)
    except Exception:
        return False


def _kill_stale_arbitrators():
    """
    Sweep for main.py processes under this repo whose age exceeds
    ARBITRATION_STALE_SECONDS — leftovers from a previous crashed /
    force-stopped launch that will otherwise re-poison every fresh
    Start attempt by making the duplicate-launch guard fire against a
    ghost.

    Deliberately narrow: matches ONLY the wedged-arbitrating shape,
    never a real host/client process.
    """
    now = time.time()
    for proc in psutil.process_iter(["pid", "cwd", "cmdline", "create_time"]):
        try:
            info = proc.info
            cwd = info.get("cwd") or ""
            cmdline = info.get("cmdline") or []
            create_time = info.get("create_time") or 0
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if not cmdline:
            continue

        joined = " ".join(cmdline).lower()
        if not _cmdline_matches(joined, "main.py"):
            continue
        if "server.py" in joined or "doorman.py" in joined or "gunicorn" in joined:
            continue
        if cwd and _norm(cwd) != _REPO_ROOT_NORM:
            continue
        if create_time and (now - create_time) <= ARBITRATION_STALE_SECONDS:
            continue

        try:
            psutil.Process(info["pid"]).kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def _resolve_stop_target(proc):
    """
    Turn find_chatbucket_process()'s result into the concrete pid that
    should actually receive the graceful-shutdown signal.

    Cases handled:
      1. Gunicorn master   → itself (best case).
      2. Gunicorn worker   → walk up to the master (its parent).
         Signalling the worker alone is a no-op — the master respawns
         it. This is the direct cause of the "did not complete within
         8s" symptom in the logs.
      3. Direct python server / doorman → itself.
      4. Arbitrating main.py → itself.

    Also returns the process-group id (POSIX) so a fallback can send
    the signal to the entire group in case walking to the master
    fails or the master is somehow detached.

    Returns (target_pid, related_pids, pgid_or_None) where
    `related_pids` are every ChatBucket-lifecycle process discovered
    (master, workers, arbitrator leftovers) — used later to verify
    the WHOLE group is gone, not just the pid that was signalled.
    """
    pid = proc["pid"]
    subshape = proc.get("subshape")

    target_pid = pid

    if subshape == "gunicorn_worker":
        # Walk up parents until we find a ChatBucket gunicorn master,
        # or exhaust the chain. Bounded walk (max 6 hops) so a broken
        # parent chain can't spin forever.
        try:
            cursor = psutil.Process(pid)
            for _ in range(6):
                parent = cursor.parent()
                if parent is None:
                    break
                p_cmdline = " ".join(parent.cmdline() or []).lower()
                p_role, p_sub = _classify_cmdline(p_cmdline)
                if p_role == "host" and p_sub == "gunicorn_master":
                    target_pid = parent.pid
                    break
                cursor = parent
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Collect every related ChatBucket lifecycle process so verification
    # can confirm the whole group is down.
    related = {target_pid, pid}
    try:
        target_proc = psutil.Process(target_pid)
        for child in target_proc.children(recursive=True):
            related.add(child.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    # Also fold in every ChatBucket-shaped process discovered globally
    # — catches an orphaned worker whose parent isn't the master
    # anymore (double-fork, or master died first).
    for other in _iter_chatbucket_procs():
        related.add(other["pid"])

    pgid = None
    if os.name != "nt":
        try:
            candidate_pgid = os.getpgid(target_pid)
            # Only trust killpg when the target IS its own group leader
            # (pgid == its own pid). Guaranteed true for anything
            # start() launched (start_new_session=True creates a fresh
            # session containing only that process tree) — NOT
            # guaranteed for an externally-started instance (e.g. run
            # interactively via chatbucket-start.sh without its own new
            # session), which could share its terminal's process group
            # with other, unrelated jobs in the same shell. killpg
            # against a shared group would signal those too. When the
            # target isn't its own leader, fall back to signalling the
            # already-enumerated `related` pids individually instead —
            # still correct, just not a single blanket group signal.
            if candidate_pgid == target_pid:
                pgid = candidate_pgid
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None

    return target_pid, related, pgid


def _derive_role_state(hs, claimed_machine, my_name, process_info):
    """
    Single source of truth for the Role badge. Actively arbitrating
    (role == "arbitrating") takes priority over whatever host-state.json
    currently says. Never shows HOST unless a real host-role process
    is verified running, not just claimed in the file.

    Returns {"state": str, "label": str, "detail": str}.
    """
    if not hs["ok"]:
        return {"state": "unknown", "label": "UNKNOWN", "detail": "host-state.json is corrupted."}

    running = process_info["running"]
    proc_role = process_info.get("role")

    if running and proc_role == "arbitrating":
        return {"state": "starting", "label": "STARTING",
                "detail": "Arbitrating host/client role — this takes a few seconds…"}

    if claimed_machine is None:
        return {"state": "idle", "label": "IDLE",
                "detail": "No claim on record — nobody has ever hosted."}

    if claimed_machine == my_name:
        if running and proc_role == "host":
            return {"state": "host", "label": "HOST",
                    "detail": "This machine is currently serving ChatBucket."}
        return {"state": "stale", "label": "STALE CLAIM",
                "detail": ("host-state.json claims this machine as host, but no live "
                           "server process was found — it likely crashed right after "
                           "claiming. Click Start to relaunch.")}

    if running and proc_role == "client":
        return {"state": "client", "label": "CLIENT", "detail": f"Host is currently: {claimed_machine}"}
    if running and proc_role == "host":
        return {"state": "conflict", "label": "CONFLICT",
                "detail": (f"host-state.json claims {claimed_machine} as host, but THIS "
                           f"machine is ALSO running as host locally — should not happen "
                           f"under normal arbitration; treat as a bug, not noise.")}
    return {"state": "client", "label": "CLIENT",
            "detail": f"Host is currently: {claimed_machine} (no local doorman running)."}


def _mark_stopped_if_mine(my_name):
    """
    Writes {"action": "stop"} to host-state.json after a stop
    completes — done HERE, in the Manager, not inside server.py.
    ChatBucket_Networking_Architecture.md §9 previously claimed
    server.py registers its own SIGINT/SIGTERM handler for this;
    checked directly against the actual file — no such handler exists
    anywhere in it. That was a stale claim in the doc, not a stale
    claim in the code, but the practical effect was the same: nothing
    was writing "stop" on ANY shutdown path, graceful or forced, which
    is why every stop left host-state.json claiming this machine as
    host with no live process behind it.

    Deliberately not fixed by adding a signal handler inside server.py
    instead: gunicorn's gevent worker already owns SIGTERM for its own
    graceful in-flight-request draining, and a second handler for the
    same signal in the same process risks shadowing or racing
    gunicorn's own handler rather than cooperating with it. The
    Manager sidesteps that entirely — it's a separate process that
    VERIFIES the target is actually gone before ever calling this. It's
    also the only thing that CAN correctly record a stop after a
    force-kill: SIGKILL can't be caught, so nothing inside a killed
    process could ever do this in that path regardless of where the
    handler lived.

    Only writes if the claim still names this machine — if it doesn't,
    something else already changed the claim in the meantime (very
    unlikely for one machine stopping its own process, but cheap to
    guard against) and it isn't this call's place to overwrite it.
    """
    hs = get_host_state()
    if not hs["ok"] or hs["state"] is None:
        return
    if hs["state"].get("machine") != my_name:
        return
    if hs["state"].get("action") == "stop":
        return
    try:
        host_state.write_state("stop", my_name)
    except Exception:
        pass


# ── Update check (§14) ───────────────────────────────────────────────

def _read_local_version():
    """
    Read the plain-text VERSION file at repo root. Returns None if the
    file is missing — matches the doc's stance that a repo without a
    VERSION file has "nothing concrete to compare against," so a
    missing local version is a real, surfaceable state, not an error
    to swallow.
    """
    try:
        with open(_VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def _parse_version_tuple(v):
    """
    Best-effort semver-ish tuple: strip a leading 'v', split on '.',
    keep only leading-digit runs. Non-numeric suffixes (e.g. "1.2.0-rc1")
    collapse to their leading integer so "1.2.0" > "1.2.0-rc1" per
    convention. Returns (0,) on parse failure so an unparseable version
    is treated as older than anything real — deliberately conservative.
    """
    if not v:
        return (0,)
    s = v.strip().lstrip("vV")
    parts = []
    for chunk in s.split("."):
        digits = ""
        for c in chunk:
            if c.isdigit():
                digits += c
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def _fetch_latest_release():
    """
    Query GitHub's Releases API for the latest stable release of the
    ChatBucket repo. Returns a dict with `tag_name`, `zipball_url`,
    and `html_url`, or raises ManagerUpdateError.

    Deliberately unauthenticated — anonymous GitHub API traffic is
    rate-limited but sufficient for a manual, button-triggered check
    from three machines. Adding a PAT would introduce a secret to
    ship, which per Architecture doc §12 the friend-installable
    footprint shouldn't require.
    """
    url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "chatbucket-manager",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        raise ManagerUpdateError(
            f"GitHub returned {e.code} when checking for updates. "
            f"If this is 403, the rate limit was hit — try again in a bit."
        )
    except urllib.error.URLError as e:
        raise ManagerUpdateError(f"Could not reach GitHub: {e.reason}")
    except (ValueError, TimeoutError) as e:
        raise ManagerUpdateError(f"GitHub response was unusable: {e}")

    tag = (payload.get("tag_name") or "").strip()
    zipball = payload.get("zipball_url")
    html_url = payload.get("html_url")
    if not tag or not zipball:
        raise ManagerUpdateError(
            "Latest release exists but is missing tag_name/zipball_url — "
            "release may still be publishing."
        )
    return {"tag_name": tag, "zipball_url": zipball, "html_url": html_url}


class ManagerUpdateError(Exception):
    """Raised by the update pipeline. Kept distinct from
    ArbitrationError — an update failure and an arbitration failure
    have very different remediation."""
    pass


def _is_path_safe(target_root, candidate):
    """
    Reject any extraction path that resolves outside `target_root`.
    Guards against the classic zip-slip vector (`../../etc/passwd`
    entries) — mandatory for extraction of an archive fetched from
    the network, even one from a source we trust, because "trust the
    source" is a policy that breaks silently the first time it's
    wrong. The check is realpath-based so a symlink inside
    target_root can't be used as a springboard either.
    """
    real_root = os.path.realpath(target_root)
    real_candidate = os.path.realpath(candidate)
    return (real_candidate == real_root
            or real_candidate.startswith(real_root + os.sep))


def _allowed_update_path(rel_path):
    """
    Decide whether a single archive member should be extracted.
    Three-layer check, all of which must pass:

      1. It's not inside a data directory (deny-list wins over
         everything else — this is the data-loss guard the doc calls
         the highest-severity failure mode).
      2. It's a plain code file (allow-listed suffix) OR inside an
         allow-listed code subdirectory OR one of the explicit
         allow-listed root files.
      3. It doesn't contain any suspicious segment (empty, `.`, `..`,
         absolute path).

    Anything failing any of these three is silently skipped — the
    extractor loop must not raise for a rejected file, otherwise a
    single unexpected member (e.g. a `.github/` workflow file) aborts
    the whole update mid-extract, leaving the repo half-updated.
    """
    if not rel_path:
        return False
    rel_path = rel_path.replace("\\", "/").lstrip("./")
    if not rel_path:
        return False
    parts = [p for p in rel_path.split("/") if p]
    if not parts:
        return False
    if any(p in ("", ".", "..") for p in parts):
        return False
    if os.path.isabs(rel_path):
        return False

    top = parts[0].lower()
    if top in _UPDATE_DENY_DIRS:
        return False

    if len(parts) == 1:
        return parts[0] in _UPDATE_ALLOWLIST_FILES or any(
            parts[0].endswith(suf) for suf in _UPDATE_ALLOWLIST_SUFFIXES
        )

    if top in _UPDATE_ALLOWLIST_DIRS:
        return True

    return any(parts[-1].endswith(suf) for suf in _UPDATE_ALLOWLIST_SUFFIXES)


def _strip_github_top(rel_path):
    """
    GitHub zipball entries are prefixed with a synthesized directory
    (`Frustrated-Femur-chatbucket-<sha>/…`). Strip that so extraction
    lands at repo root, not a nested folder.
    """
    rel_path = rel_path.replace("\\", "/")
    if "/" in rel_path:
        _, rest = rel_path.split("/", 1)
        return rest
    # Top-level entry with no subpath — probably the container dir
    # itself; nothing to extract for it.
    return ""


def _backup_current_code():
    """
    Move (not copy) every allow-listed code path currently in the repo
    into a sibling `.update-backup/` directory. Move is chosen over
    copy on purpose:

      - Move is atomic per file on the same filesystem — no
        half-copied intermediate state to reason about.
      - Move guarantees the old file is out of the way before the
        new one is written, so a partial extraction can't leave a
        mixed-vintage tree of new and old files interleaved.
      - Restore is symmetric: move back on failure.

    Data directories are never touched.
    """
    if os.path.isdir(_BACKUP_DIR):
        shutil.rmtree(_BACKUP_DIR, ignore_errors=True)
    os.makedirs(_BACKUP_DIR, exist_ok=True)

    moved = []
    for entry in os.listdir(_REPO_ROOT):
        if entry in _UPDATE_DENY_DIRS:
            continue
        full = os.path.join(_REPO_ROOT, entry)
        rel = entry
        is_code = False
        if os.path.isfile(full) and any(entry.endswith(suf) for suf in _UPDATE_ALLOWLIST_SUFFIXES):
            is_code = True
        elif os.path.isfile(full) and entry in _UPDATE_ALLOWLIST_FILES:
            is_code = True
        elif os.path.isdir(full) and entry.lower() in _UPDATE_ALLOWLIST_DIRS:
            is_code = True
        if not is_code:
            continue
        dest = os.path.join(_BACKUP_DIR, rel)
        try:
            shutil.move(full, dest)
            moved.append(rel)
        except (OSError, shutil.Error):
            # Individual move failure isn't fatal here — the caller
            # verifies startup afterwards and rolls back the whole
            # thing if anything went wrong. Better to try the rest
            # than abort mid-backup and leave the repo half-moved.
            continue
    return moved


def _restore_backup():
    """
    Undo _backup_current_code(). Copies each backed-up path back over
    whatever's currently at that location — the reverse of the move-
    then-extract flow. Called on any startup-verification failure
    (§14 step 5).
    """
    if not os.path.isdir(_BACKUP_DIR):
        return False
    for entry in os.listdir(_BACKUP_DIR):
        src = os.path.join(_BACKUP_DIR, entry)
        dst = os.path.join(_REPO_ROOT, entry)
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        elif os.path.isfile(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        try:
            shutil.move(src, dst)
        except (OSError, shutil.Error):
            continue
    shutil.rmtree(_BACKUP_DIR, ignore_errors=True)
    return True


def _extract_release_zip(zip_path):
    """
    Extract a release ZIP into the repo root, honouring the allow-list
    and zip-slip protection. Returns the number of files actually
    written.
    """
    written = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            name = member.filename
            if not name or name.endswith("/"):
                continue
            stripped = _strip_github_top(name)
            if not stripped:
                continue
            if not _allowed_update_path(stripped):
                continue
            dest = os.path.join(_REPO_ROOT, stripped)
            if not _is_path_safe(_REPO_ROOT, dest):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(member, "r") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            written += 1
    return written


# ── Manager API (js_api bridge) ──────────────────────────────────────

class ManagerApi:
    def __init__(self):
        self.my_name = cb_main.get_machine_name()

    def get_status(self):
        hs = get_host_state()
        claimed_machine = None
        if hs["ok"] and hs["state"] is not None:
            claimed_machine = hs["state"]["machine"]

        claimed_status = None
        if claimed_machine is not None:
            claimed_status = get_claimed_host_status(claimed_machine, self.my_name)

        tp = get_tailnet_peers()

        proc = find_chatbucket_process()
        process_info = (
            {"running": False} if proc is None
            else {"running": True, "pid": proc["pid"], "role": proc["role"]}
        )

        role_state = _derive_role_state(hs, claimed_machine, self.my_name, process_info)

        return {
            "my_name": self.my_name,
            "host_state": hs,
            "claimed_machine": claimed_machine,
            "claimed_status": claimed_status,
            "tailnet_peers": tp,
            "process": process_info,
            "role_state": role_state,
            "version": _read_local_version(),
            "syncthing": get_syncthing_status(),
        }

    def start(self):
        """
        Blocks until the launched instance resolves to a real role
        (host/client) or START_GRACE_SECONDS elapses.
        """
        _kill_stale_arbitrators()

        existing = find_chatbucket_process()
        if existing is not None:
            return {"ok": True, "action": "none",
                    "detail": f"Already running (pid {existing['pid']}, role: {existing['role']})."}

        python_path = _venv_python()
        main_py = os.path.join(_REPO_ROOT, "main.py")
        if not os.path.exists(python_path):
            return {"ok": False, "detail": f"venv Python not found at {python_path}."}
        if not os.path.exists(main_py):
            return {"ok": False, "detail": f"main.py not found at {main_py}."}

        kwargs = {"cwd": _REPO_ROOT}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # New session so a SIGTERM aimed at our pid can, if needed,
            # be turned into a process-group signal without also
            # signalling the Manager itself.
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen([python_path, main_py, self.my_name], **kwargs)
        except OSError as e:
            return {"ok": False, "detail": f"Failed to launch: {e}"}

        resolved_role = _wait_for_role(proc.pid, START_GRACE_SECONDS)
        if resolved_role is None:
            final = find_chatbucket_process()
            if final is not None and final["role"] in ("host", "client"):
                return {"ok": True, "action": "started",
                        "detail": f"Launched and confirmed as {final['role'].upper()} (pid {final['pid']})."}
            return {
                "ok": True, "action": "started_unconfirmed",
                "detail": (f"Launched (pid {proc.pid}) but couldn't confirm it reached "
                           f"HOST or CLIENT within {START_GRACE_SECONDS}s — it may still "
                           f"be arbitrating. Check again shortly."),
            }
        return {"ok": True, "action": "started",
                "detail": f"Launched and confirmed as {resolved_role.upper()} (pid {proc.pid})."}

    def stop(self):
        """
        Multi-stage graceful stop that survives gunicorn's master/
        worker split — the exact failure mode reproduced in the log
        the user attached:

            [7876] gunicorn master
              └── [7925] worker (respawned; find_chatbucket_process
                                 returned THIS pid, SIGTERM to it was
                                 a no-op because master resurrected it)

        Fix:
          1. Resolve the discovered pid to the graceful-stop target
             (walk up to gunicorn master; workers alone are no-op
             signal targets).
          2. Signal the process group (killpg on POSIX) rather than
             just one pid — start() spawns with start_new_session so
             the group id equals the master's pid, and the master's
             own SIGTERM handler drains the workers cleanly. On
             Windows, still CTRL_BREAK_EVENT — same as before.
          3. Verify the WHOLE lifecycle group is gone, not just one
             pid. A stopped master with a lingering worker still
             holding port 5000 is "still running" to the user.
          4. Fall back to SIGKILL / TerminateProcess only after
             STOP_GRACE_SECONDS, and surface the force-stop clearly.
        """
        proc = find_chatbucket_process()
        if proc is None:
            _kill_stale_arbitrators()
            _mark_stopped_if_mine(self.my_name)
            return {"ok": True, "action": "none", "detail": "ChatBucket is not running."}

        target_pid, related, pgid = _resolve_stop_target(proc)

        # ── graceful signal ───────────────────────────────────────
        if os.name == "nt":
            signaled = _send_ctrl_break_windows(target_pid)
            graceful_label = "CTRL_BREAK_EVENT" if signaled else "CTRL_BREAK_EVENT (call itself failed)"
        else:
            # Prefer the process group — one signal cleanly drains
            # master + all workers together, matching gunicorn's own
            # graceful-shutdown expectation. Fall back to a plain
            # per-pid SIGTERM if we couldn't determine a pgid.
            graceful_label = "SIGTERM"
            sent = False
            if pgid:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    graceful_label = f"SIGTERM to pgid {pgid}"
                    sent = True
                except (ProcessLookupError, PermissionError, OSError):
                    sent = False
            if not sent:
                try:
                    os.kill(target_pid, signal.SIGTERM)
                except ProcessLookupError:
                    return {"ok": True, "action": "none",
                            "detail": "Process exited before it could be signaled."}
                except PermissionError as e:
                    return {"ok": False, "action": "error",
                            "detail": f"Not permitted to signal pid {target_pid}: {e}"}

        # ── verify whole group is down, not just one pid ──────────
        if _wait_for_all_gone(list(related), STOP_GRACE_SECONDS):
            _kill_stale_arbitrators()
            _mark_stopped_if_mine(self.my_name)
            return {"ok": True, "action": "graceful",
                    "detail": f"Stopped via {graceful_label} (target pid {target_pid})."}

        # ── force stop path ───────────────────────────────────────
        # Kill in two phases:
        #   Phase A: the master itself (so it stops spawning workers).
        #   Phase B: every remaining lifecycle pid (workers/orphans).
        # Doing it in this order avoids the phase-B pids being
        # replaced by fresh worker respawns between our kill and our
        # verify.
        try:
            psutil.Process(target_pid).kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        for pid in related:
            if pid == target_pid:
                continue
            try:
                psutil.Process(pid).kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        force_exited = _wait_for_all_gone(list(related), 5)
        _kill_stale_arbitrators()
        if force_exited:
            _mark_stopped_if_mine(self.my_name)
        return {
            "ok": force_exited, "action": "forced",
            "detail": (f"Graceful stop via {graceful_label} did not complete within "
                       f"{STOP_GRACE_SECONDS}s — force-stopped {len(related)} "
                       f"process(es). host-state.json may be stale until another "
                       f"machine's health check catches it."),
        }

    # ── §14: Check for Updates ─────────────────────────────────────

    def check_for_updates(self):
        """
        Compare the repo's `VERSION` file against the latest GitHub
        release. Never installs — this is the read-only comparison
        step. The user must click Install separately (per §14: the
        whole update flow is manual, never automatic).

        Returns:
            {
                "ok": bool,
                "current": str | None,        # local VERSION file
                "latest":  str | None,        # release tag
                "newer_available": bool,
                "release_url": str | None,
                "detail": str,
            }
        """
        current = _read_local_version()
        try:
            release = _fetch_latest_release()
        except ManagerUpdateError as e:
            return {"ok": False, "current": current, "latest": None,
                    "newer_available": False, "release_url": None,
                    "detail": str(e)}

        latest = release["tag_name"]
        release_url = release.get("html_url")

        if current is None:
            # Missing VERSION file — the doc says an install without
            # VERSION has "nothing concrete to compare against." Show
            # the latest and let the user decide.
            return {
                "ok": True, "current": None, "latest": latest,
                "newer_available": True, "release_url": release_url,
                "detail": ("No local VERSION file found. Latest release is "
                           f"{latest}. Install to set the baseline."),
            }

        newer = _parse_version_tuple(latest) > _parse_version_tuple(current)
        if newer:
            detail = f"Update available: {current} → {latest}."
        else:
            detail = f"Up to date (installed {current}, latest {latest})."
        return {"ok": True, "current": current, "latest": latest,
                "newer_available": newer, "release_url": release_url,
                "detail": detail}

    def install_update(self):
        """
        Perform the full §14 workflow, in order and atomically-ish:

          1. Confirm a newer release exists (re-check, don't trust
             the last check_for_updates cached result — the release
             could have been yanked in between).
          2. Stop ChatBucket gracefully — reuses stop() so all its
             gunicorn-master/worker handling applies here too.
          3. Move current allow-listed code into .update-backup/.
          4. Download the release ZIP to a temp path.
          5. Extract via allow-list + zip-slip guard into repo root.
          6. Verify the new code even *parses* (compile main.py and
             arbitration.py) — this catches "release ZIP truncated
             mid-download" and "wrong branch tagged" before we hand
             it to Popen and only discover the syntax error minutes
             later when arbitration fails.
          7. Do NOT auto-restart. Per §14, restart is by re-invoking
             main.py from the Manager's own Start button — the user
             clicks Start when they're ready. Auto-relaunch here
             would bypass the discipline of "restart is a conscious
             re-arbitration," which is exactly what §14 warns
             against.
          8. On any failure between steps 3 and 6: roll back via
             _restore_backup().
        """
        pre_check = self.check_for_updates()
        if not pre_check["ok"]:
            return {"ok": False, "step": "check", "detail": pre_check["detail"]}
        if not pre_check["newer_available"]:
            return {"ok": True, "step": "noop", "detail": pre_check["detail"]}

        stop_result = self.stop()
        if not stop_result["ok"]:
            return {"ok": False, "step": "stop",
                    "detail": f"Refusing to update while ChatBucket is still running: {stop_result['detail']}"}

        release = None
        try:
            release = _fetch_latest_release()
        except ManagerUpdateError as e:
            return {"ok": False, "step": "fetch", "detail": str(e)}

        # Step 3: backup.
        try:
            backed_up = _backup_current_code()
        except OSError as e:
            return {"ok": False, "step": "backup",
                    "detail": f"Could not back up current code: {e}"}
        if not backed_up:
            # Nothing to back up would be extremely weird — refuse
            # to overwrite anything if that's the state, better to
            # error out than to extract into a directory whose
            # contents we couldn't move out first.
            return {"ok": False, "step": "backup",
                    "detail": "No code files were backed up — refusing to extract on top of an unrecognised repo layout."}

        # Step 4: download.
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", prefix="chatbucket-update-")
        tmp_path = tmp.name
        tmp.close()
        try:
            req = urllib.request.Request(release["zipball_url"], headers={
                "Accept": "application/zip",
                "User-Agent": "chatbucket-manager",
            })
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp_path, "wb") as out:
                shutil.copyfileobj(resp, out)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
            _restore_backup()
            _cleanup_tempfile(tmp_path)
            return {"ok": False, "step": "download",
                    "detail": f"Failed to download release ZIP: {e}"}

        # Step 5: extract.
        try:
            written = _extract_release_zip(tmp_path)
        except (zipfile.BadZipFile, OSError) as e:
            _restore_backup()
            _cleanup_tempfile(tmp_path)
            return {"ok": False, "step": "extract",
                    "detail": f"Release ZIP could not be extracted: {e}"}
        finally:
            _cleanup_tempfile(tmp_path)

        if written == 0:
            _restore_backup()
            return {"ok": False, "step": "extract",
                    "detail": "Release ZIP contained no allow-listed files — treated as broken; rolled back."}

        # Step 6: sanity-compile.
        problem = _sanity_compile_code_root()
        if problem is not None:
            _restore_backup()
            return {"ok": False, "step": "verify",
                    "detail": f"New code failed sanity check ({problem}). Rolled back to previous version."}

        # Success — remove backup.
        shutil.rmtree(_BACKUP_DIR, ignore_errors=True)

        new_version = _read_local_version() or release["tag_name"]
        return {"ok": True, "step": "done",
                "detail": (f"Updated to {new_version}. Click Start to relaunch — "
                           f"restart goes through arbitration on purpose, not "
                           f"straight back into the prior role.")}


def _cleanup_tempfile(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _sanity_compile_code_root():
    """
    Compile every .py file at repo root using py_compile — cheap
    smoke test that the new code isn't syntactically broken. Returns
    None on success, or a short string describing the first failure.
    Only checks root-level Python files (main.py/arbitration.py/etc.);
    manager/ subtree lives under manager/ and gets compiled implicitly
    when the Manager next restarts — which happens outside the update
    flow.
    """
    import py_compile
    for entry in os.listdir(_REPO_ROOT):
        if not entry.endswith(".py"):
            continue
        full = os.path.join(_REPO_ROOT, entry)
        if not os.path.isfile(full):
            continue
        try:
            py_compile.compile(full, doraise=True)
        except py_compile.PyCompileError as e:
            return f"{entry}: {e.msg.strip()}"
        except OSError as e:
            return f"{entry}: {e}"
    return None


# ── CLI probe ─────────────────────────────────────────────────────────

def _print_cli_report():
    my_name = cb_main.get_machine_name()
    print(f"Detected machine name: {my_name}")
    print(f"Repo root: {_REPO_ROOT}")
    print()

    print("=== host-state.json ===")
    hs = get_host_state()
    claimed_machine = None
    if hs["ok"]:
        state = hs["state"]
        if state is None:
            print("  No claim on record (nobody has ever hosted).")
        else:
            print(f"  action:    {state['action']}")
            print(f"  machine:   {state['machine']}")
            print(f"  timestamp: {state['timestamp']}")
            claimed_machine = state["machine"]
    else:
        print(f"  CORRUPTED: {hs['error']}")

    print()
    print("=== process ===")
    proc = find_chatbucket_process()
    process_info = {"running": False} if proc is None else {"running": True, "pid": proc["pid"], "role": proc["role"]}
    if proc is None:
        print("  Not running.")
    else:
        print(f"  Running: pid {proc['pid']}, role: {proc['role']}, subshape: {proc.get('subshape')}")

    print()
    print("=== reconciled role ===")
    role_state = _derive_role_state(hs, claimed_machine, my_name, process_info)
    print(f"  {role_state['label']} — {role_state['detail']}")

    print()
    print("=== version ===")
    v = _read_local_version()
    print(f"  local VERSION: {v if v else '(missing)'}")

    print()
    print("=== claimed host status ===")
    if not hs["ok"]:
        print("  (host-state.json is corrupted — see above; nothing to check)")
    elif claimed_machine is None:
        print("  (no claim on record — nothing to check)")
    else:
        info = get_claimed_host_status(claimed_machine, my_name)
        if info.get("self"):
            print(f"  {claimed_machine}: (this machine)")
        elif info.get("error"):
            print(f"  {claimed_machine}: ERROR — {info['error']}")
        else:
            print(f"  {claimed_machine}: {'online' if info['online'] else 'offline'}")

    print()
    print("=== tailnet peers (dynamic, infra hidden) ===")
    tp = get_tailnet_peers()
    if not tp["ok"]:
        print(f"  ERROR — {tp['error']}")
    else:
        if not tp["peers"]:
            print("  (no peers found)")
        for peer in sorted(tp["peers"], key=lambda p: p["name"]):
            print(f"  {peer['name']}: {'online' if peer['online'] else 'offline'}")
        if tp["hidden_count"]:
            print(f"  (+{tp['hidden_count']} infrastructure peer(s) hidden — no DNSName, likely Tailscale Funnel)")

    print()
    print("=== syncthing (sync-state folder) ===")
    st = get_syncthing_status()
    if st["state"] == "not_configured":
        print("  not configured (no manager_config.json / syncthing_api_key)")
    elif st["state"] == "error":
        print(f"  ERROR — {st['detail']}")
    else:
        print(f"  {st['state']}")


# ── System tray (§17 "not yet built": tray icon) ───────────────────────
#
# pystray/Pillow were already a declared dependency (manager_smoke_test.py's
# 7-point check has covered "pystray+Pillow" since before this section
# existed) — this closes that out. Nothing below is imported at module
# level, matching how `webview` itself is only imported inside _run_gui():
# --cli mode must keep working in an environment with no display at all,
# and importing pystray unconditionally would break that (see
# _select_tray_backend()'s docstring for a concrete case where a bare
# `import pystray` can crash outright, not just fail to find an icon).

def _select_tray_backend():
    """
    Must be set before `import pystray` — it resolves its backend once,
    at import time, not via a constructor kwarg. Windows: left alone
    (win32 is pystray's own unconditional pick there). Non-Windows:
    forced to "xorg" — letting pystray auto-probe appindicator/gtk
    first can raise an uncaught ValueError on a box missing the Gtk
    typelib (confirmed empirically), instead of falling through to the
    next candidate. xorg is pure Xlib, sidesteps that, and can't
    collide with pywebview's own Gtk.main() loop either. Dev/test only
    (i3 + polybar — confirmed working); Windows is what actually ships.
    """
    if os.name != "nt":
        os.environ.setdefault("PYSTRAY_BACKEND", "xorg")


def _tray_role_state(my_name):
    """
    A cheap subset of ManagerApi.get_status() sized for the tray's own
    TRAY_POLL_SECONDS=5s loop — host-state.json read + local process
    scan only. Deliberately does NOT call
    arbitration.check_machine_online() or arbitration.list_tailnet_peers():
    those shell out to `tailscale`, and the tray never displays peer or
    claimed-host detail, only role_state. Running that CLI shellout on
    a second, faster, independent timer alongside the window's own 15s
    poll (web/index.html's NORMAL_REFRESH_MS) would double the
    `tailscale status` call rate for a value the tray doesn't show.

    Reuses get_host_state() / find_chatbucket_process() /
    _derive_role_state() — the exact same functions ManagerApi.get_status()
    itself calls — never a re-derivation. Same "one source of truth"
    discipline already on record for the tailscale-peer logic and the
    Role badge (§16.5).

    Returns (role_state, process_info) — process_info is what the
    Start/Stop menu items' `enabled=` callables check.
    """
    hs = get_host_state()
    claimed_machine = None
    if hs["ok"] and hs["state"] is not None:
        claimed_machine = hs["state"]["machine"]

    proc = find_chatbucket_process()
    process_info = (
        {"running": False} if proc is None
        else {"running": True, "pid": proc["pid"], "role": proc["role"]}
    )

    role_state = _derive_role_state(hs, claimed_machine, my_name, process_info)
    return role_state, process_info


def _tray_icon_image(hex_color):
    """
    Draws a filled circle on a transparent background — no static
    asset file to ship or locate. Colored per _TRAY_STATE_COLORS so
    the tray is glanceable (HOST vs CLIENT vs a problem state) without
    opening the window. 64x64 scales down cleanly to both a Linux
    panel's ~22px and Windows' notification-area sizing.

    Imported lazily (PIL, not pystray) for the same reason the rest of
    this section defers its imports — see the section banner above.
    """
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 6
    draw.ellipse([pad, pad, size - pad, size - pad], fill=hex_color)
    return img


def _build_tray_icon(api, window):
    """
    Builds — but does not start — the tray Icon. Returns None if
    pystray/Pillow/the current display can't actually produce one; the
    Manager runs exactly as it did before this section existed in that
    case (window opens normally, closing it quits, same as always).
    Never a hard requirement — see _run_gui() for how that fallback is
    wired.

    Threading model, confirmed by reading pystray's own backend source
    rather than assumed:

    - icon.run_detached() is used, never the blocking run(). On both
      backends actually in play here (win32 always on Windows; xorg
      forced on Linux, see _select_tray_backend()), run_detached()
      does the exact same thing: spin the icon's own event loop on a
      plain background thread and return immediately
      (`threading.Thread(target=lambda: self._run()).start()`,
      verbatim in both _win32.py and _xorg.py). Every menu-item action
      below therefore executes on THAT background thread, not the
      thread that called run_detached().

    - window.show()/window.hide()/window.destroy() are safe to call
      from that thread. Confirmed by reading pywebview's own platform
      backends: gtk.py marshals every Window method through
      `glib.idle_add(...)`, winforms.py through `self.Invoke(...)` —
      both are the standard thread-marshaling idiom for their
      respective toolkits, dispatching the actual call onto
      pywebview's real GUI thread regardless of which thread invoked
      it. This is the same mechanism that already makes calling
      window.* from inside a js_api method (also a non-GUI thread)
      safe today — nothing new is being relied on here.

    - Start/Stop dispatch ManagerApi.start()/stop() onto their OWN
      short-lived thread rather than calling directly, so a
      START_GRACE_SECONDS/STOP_GRACE_SECONDS-long wait never blocks
      the tray's event thread. A tray that stops responding to hovers
      for up to 20s while ChatBucket is merely still stopping would
      look identical to a hang.
    """
    _select_tray_backend()
    try:
        import pystray
        from pystray import Menu, MenuItem
    except Exception as e:
        print(f"[tray] pystray unavailable — running without a tray icon: {e}")
        return None

    my_name = api.my_name
    stop_polling = threading.Event()

    def _status_text(item):
        role_state, _ = _tray_role_state(my_name)
        return f"Role: {role_state['label']}"

    def _is_running(item):
        _, process_info = _tray_role_state(my_name)
        return process_info["running"]

    def _is_not_running(item):
        return not _is_running(item)

    def _on_show(icon, item):
        window.show()

    def _on_start(icon, item):
        threading.Thread(target=api.start, daemon=True).start()

    def _on_stop(icon, item):
        threading.Thread(target=api.stop, daemon=True).start()

    def _on_quit(icon, item):
        stop_polling.set()
        icon.stop()   # up to 5s slower if no systray host is running at
                      # all (pystray's own SETUP_THREAD_TIMEOUT) — not
                      # the case on i3+polybar, confirmed.
        window.destroy()

    menu = Menu(
        MenuItem(_status_text, None, enabled=False),
        Menu.SEPARATOR,
        MenuItem("Show ChatBucket Manager", _on_show, default=True),
        Menu.SEPARATOR,
        MenuItem("Start ChatBucket", _on_start, enabled=_is_not_running),
        MenuItem("Stop ChatBucket", _on_stop, enabled=_is_running),
        Menu.SEPARATOR,
        MenuItem("Quit Manager", _on_quit),
    )

    role_state, _ = _tray_role_state(my_name)
    try:
        icon = pystray.Icon(
            "chatbucket-manager",
            icon=_tray_icon_image(_TRAY_STATE_COLORS.get(role_state["state"], _TRAY_DEFAULT_COLOR)),
            title=f"ChatBucket Manager - {role_state['label']}",
            menu=menu,
        )
    except Exception as e:
        print(f"[tray] failed to construct tray icon — running without one: {e}")
        return None

    def _poll_loop():
        # stop_polling.wait() doubles as the sleep AND the early-exit
        # check, so Quit stops this promptly instead of after up to
        # TRAY_POLL_SECONDS of a pointless extra tick.
        while not stop_polling.wait(TRAY_POLL_SECONDS):
            try:
                rs, _ = _tray_role_state(my_name)
                icon.icon = _tray_icon_image(_TRAY_STATE_COLORS.get(rs["state"], _TRAY_DEFAULT_COLOR))
                icon.title = f"ChatBucket Manager - {rs['label']}"
                icon.update_menu()
            except Exception:
                # A single bad tick (e.g. host-state.json mid-write)
                # should never take the tray down — try again next time.
                pass

    threading.Thread(target=_poll_loop, daemon=True).start()
    return icon


def _run_gui():
    import webview
    api = ManagerApi()
    window = webview.create_window(
        "ChatBucket Manager", _WEB_INDEX, js_api=api,
        width=480, height=760, min_size=(380, 600),
    )

    tray = _build_tray_icon(api, window)
    if tray is not None:
        def _on_closing():
            window.hide()
            return False   # False here == "cancel the close" once collected
                            # by pywebview's Event.set(): confirmed by reading
                            # webview/event.py (any handler returning False
                            # makes set() return should_cancel=True) and
                            # platforms/gtk.py's close_window() (`if
                            # should_cancel: return True`, which is GTK's own
                            # delete-event convention for "don't destroy me").
        window.events.closing += _on_closing
        tray.run_detached()
    # else: no tray in this environment — leave the window's default
    # close behaviour (destroy -> process exits) completely untouched.
    # Wiring closing-hides-instead-of-closes with no tray to bring the
    # window back would trap the user with no way to quit.

    try:
        webview.start()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", action="store_true")
    args = parser.parse_args()

    if args.cli:
        _print_cli_report()
    else:
        _run_gui()
