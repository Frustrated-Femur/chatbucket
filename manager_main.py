"""
manager_main.py — Manager entrypoint.

Two modes:
    python3 manager_main.py          -> opens the real pywebview window
    python3 manager_main.py --cli    -> stdout-only probe, no window

Lives at repo root, alongside main.py/arbitration.py/server.py.
"""
import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time

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

STOP_GRACE_SECONDS = 8
START_GRACE_SECONDS = 8
POLL_INTERVAL = 0.4

import arbitration
import host_state
import main as cb_main


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
        return {"ok": True, "peers": result["peers"], "hidden_count": result["hidden_count"]}
    except arbitration.ArbitrationError as e:
        return {"ok": False, "error": str(e)}


# ── process discovery ─────────────────────────────────────────────────

def find_chatbucket_process():
    """
    Matches on repo cwd, across THREE possible cmdline shapes, in
    lifecycle order:

      1. "main.py"                    -> role "arbitrating" (pre-exec —
         jitter + health-check window; a real, ongoing ChatBucket
         instance, just not yet resolved to host or client)
      2. "server.py" / gunicorn+server:app -> role "host"  (post-exec)
      3. "doorman.py"                 -> role "client" (post-exec)

    All three are the SAME logical instance across os.execv()'s process-
    image replacement (PID is preserved by execv, never changes) — this
    function just has to recognize whichever shape it currently is.
    Missing case 1 was a real bug: it made the duplicate-launch guard
    in start() blind to an instance that's actively arbitrating but
    hasn't exec'd yet, allowing a second launch into the exact race
    chatbucket-start.sh's pidfile guard exists to prevent.
    """
    repo_root_norm = os.path.normcase(os.path.normpath(_REPO_ROOT))

    for proc in psutil.process_iter(["pid", "cwd", "cmdline"]):
        try:
            info = proc.info
            cwd = info.get("cwd") or ""
            cmdline = info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        if not cwd:
            continue
        if os.path.normcase(os.path.normpath(cwd)) != repo_root_norm:
            continue

        joined = " ".join(cmdline).lower()
        if "server.py" in joined or ("gunicorn" in joined and "server:app" in joined):
            return {"pid": info["pid"], "role": "host", "cmdline": cmdline}
        if "doorman.py" in joined:
            return {"pid": info["pid"], "role": "client", "cmdline": cmdline}
        if "main.py" in joined:
            return {"pid": info["pid"], "role": "arbitrating", "cmdline": cmdline}

    return None


def _venv_python():
    if os.name == "nt":
        return os.path.join(_REPO_ROOT, ".venv", "Scripts", "python.exe")
    return os.path.join(_REPO_ROOT, ".venv", "bin", "python")


def _wait_for_exit(pid, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(POLL_INTERVAL)
    return not psutil.pid_exists(pid)


def _wait_for_role(pid, timeout):
    """
    Polls find_chatbucket_process() until it reports OUR pid resolved
    to a real role (host/client), or the timeout elapses. Returns
    "host"/"client" if resolved, None otherwise.

    Matching on the SAME pid we got from Popen is correct across
    main.py's os.execv() — execv replaces the process image, never the
    PID — so this reliably tracks one specific launch through the
    arbitrating -> host-or-client transition, not just "something
    matching ChatBucket exists" (which could be a stale unrelated match
    if timing is unlucky).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = find_chatbucket_process()
        if proc is not None and proc["pid"] == pid and proc["role"] in ("host", "client"):
            return proc["role"]
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


def _derive_role_state(hs, claimed_machine, my_name, process_info):
    """
    Single source of truth for the Role badge. Actively arbitrating
    (role == "arbitrating") takes priority over whatever host-state.json
    currently says — that claim might be stale and about to be
    overwritten any moment. Never shows HOST unless a real host-role
    process is verified running, not just claimed in the file.

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
        }

    def start(self):
        """
        Blocks until the launched instance resolves to a real role
        (host/client) or START_GRACE_SECONDS elapses — mirrors stop()'s
        act-then-verify discipline instead of returning the instant
        Popen() succeeds, which would report "started" before there's
        anything real to show for it (arbitration's own jitter +
        health-check + gunicorn boot can take several real seconds).
        """
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

        try:
            proc = subprocess.Popen([python_path, main_py, self.my_name], **kwargs)
        except OSError as e:
            return {"ok": False, "detail": f"Failed to launch: {e}"}

        resolved_role = _wait_for_role(proc.pid, START_GRACE_SECONDS)
        if resolved_role is None:
            return {
                "ok": True, "action": "started_unconfirmed",
                "detail": (f"Launched (pid {proc.pid}) but couldn't confirm it reached "
                           f"HOST or CLIENT within {START_GRACE_SECONDS}s — it may still "
                           f"be arbitrating. Check again shortly."),
            }
        return {"ok": True, "action": "started",
                "detail": f"Launched and confirmed as {resolved_role.upper()} (pid {proc.pid})."}

    def stop(self):
        proc = find_chatbucket_process()
        if proc is None:
            return {"ok": True, "action": "none", "detail": "ChatBucket is not running."}

        pid = proc["pid"]

        if os.name == "nt":
            signaled = _send_ctrl_break_windows(pid)
            graceful_label = "CTRL_BREAK_EVENT" if signaled else "CTRL_BREAK_EVENT (call itself failed)"
        else:
            try:
                os.kill(pid, signal.SIGTERM)
                graceful_label = "SIGTERM"
            except ProcessLookupError:
                return {"ok": True, "action": "none", "detail": "Process exited before it could be signaled."}

        if _wait_for_exit(pid, STOP_GRACE_SECONDS):
            return {"ok": True, "action": "graceful", "detail": f"Stopped via {graceful_label}."}

        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            return {"ok": True, "action": "graceful", "detail": "Process exited during force-stop check."}

        force_exited = _wait_for_exit(pid, 3)
        return {
            "ok": force_exited, "action": "forced",
            "detail": (f"Graceful stop via {graceful_label} did not complete within "
                       f"{STOP_GRACE_SECONDS}s — force-stopped. host-state.json may be "
                       f"stale until another machine's health check catches it."),
        }


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
    print("  Not running." if proc is None else f"  Running: pid {proc['pid']}, role: {proc['role']}")

    print()
    print("=== reconciled role ===")
    role_state = _derive_role_state(hs, claimed_machine, my_name, process_info)
    print(f"  {role_state['label']} — {role_state['detail']}")

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


def _run_gui():
    import webview
    api = ManagerApi()
    webview.create_window(
        "ChatBucket Manager", _WEB_INDEX, js_api=api,
        width=480, height=760, min_size=(380, 600),
    )
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
