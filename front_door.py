"""
front_door.py — Persistent supervisor process for ChatBucket.

Runs once per machine, bound to port 5000 for its ENTIRE lifetime. Never
morphs identity (no execv), never releases port 5000 during role
transitions. Supersedes the old main.py-execs-into-server.py-or-doorman.py
lifecycle per the front-door architecture doc.

Concurrency model:
    accept_loop_thread        : owns the public listen socket; per-accept,
                                spawns a worker thread that reads _pointer
                                under _pointer_rlock and dispatches to the
                                proxy/redirect/unavailable handler.
    control_thread            : the ONLY thread that calls arbitration,
                                spawns/stops the child, mutates _pointer.
                                Drains _control_queue serially. This
                                single-writer discipline is what makes
                                "we cannot accidentally create two children"
                                a structural property rather than an
                                if-else chain to keep straight.
    child_waiter_thread       : dedicated to child.wait(); when the child
                                exits (crash or graceful), posts one
                                CHILD_EXITED intent to _control_queue.
                                Event-driven, not polled.
    liveness_loop_thread      : runs only while pointer=Redirect AND
                                take_host_on_crash=on. Posts a
                                TAKEOVER_ATTEMPT intent after N consecutive
                                failed /health checks. Otherwise idle.
    status_server_thread      : loopback-only HTTP endpoint on 127.0.0.1:5050
                                serving status + control POSTs.

_control_queue is where every "the world changed, decide what to do"
signal goes. That includes boot, child-exit, config-toggle-flipped,
takeover-triggered, and manager-Start/Stop requests. Serializing them
through a single queue is deliberately how we guarantee they can't step
on each other.

What this module does NOT do:
    - Run the ChatBucket app itself (that's the child, still server.py).
    - Do any HTTP parsing on the proxied bytes (that's tcp_proxy.py's job
      to avoid).
    - Duplicate arbitration decision logic (still in arbitration.py, called
      here repeatedly).
    - Understand what the Rust Manager displays (we just expose an interface).
"""

import collections
import http.server
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import arbitration
import host_state
import manager_config
import tcp_proxy

# ── config-y constants (top of file for editability) ─────────────────────
PUBLIC_PORT = 5000
LOCAL_CHILD_PORT = 5001
STATUS_PORT = 5050
TAILNET_SUFFIX = "tail888cf2.ts.net"
REDIRECT_SCHEME = "http"    # matches server.py + arbitration.py — keep in sync

MAX_REDIRECT_HOPS = 2       # unchanged from doorman.py

# Respawn cap (§3.3 of the front-door doc — proposed as a starting point
# and adjusted here after inspecting server.py startup: server.py runs a
# one-time chat.jsonl migration + a per-message scan at import time,
# which on the modest hardware target could take 1-2 real seconds. A
# too-tight window (say 30s) would falsely trip on a machine with a large
# messages/ backlog. 5 minutes is the doc's number and it's the right
# ballpark — leaving it.)
RESPAWN_MAX_ATTEMPTS = 3
RESPAWN_WINDOW_SECONDS = 5 * 60

# Liveness loop (§3.4). 20s interval + 3-consecutive-failure debounce is
# the doc's proposal; leaving it. See also HEALTH_CHECK_TIMEOUT below for
# the per-check timeout — 3s matches arbitration.py's own timeout, so a
# genuinely-dead host reaches its own conclusion in the same time this
# module does.
LIVENESS_INTERVAL_SECONDS = 20
LIVENESS_FAILURES_TO_TAKEOVER = 3
LIVENESS_HEALTH_TIMEOUT = 3

# Graceful stop cap. gunicorn is invoked with --graceful-timeout 5, so
# 8 seconds is a headroom-y ceiling: real gunicorn shutdown always
# finishes well before then, and Windows/Werkzeug TerminateProcess is
# effectively instant. Escalation to kill() past this covers only the
# pathological "child is wedged in an uninterruptible syscall" case.
CHILD_STOP_GRACEFUL_SECONDS = 8

# ── module-level runtime state (all protected as noted per-field) ─────────

# Snapshot of the current external pointer as seen by connection workers.
# {"kind": "local"|"redirect"|"unavailable", "machine": str|None,
#  "child_pid": int|None, "starting": bool}
_pointer = {"kind": "unavailable", "machine": None, "child_pid": None,
            "starting": False, "last_error": None}
_pointer_rlock = threading.RLock()

# Held ONLY by control_thread while mutating lifecycle. Never held by
# accept-path workers — those must never block on lifecycle.
_supervisor_lock = threading.Lock()

# Every lifecycle-triggering signal goes through here. Consumed by
# control_thread, one at a time, in order.
_control_queue = queue.Queue()

# Current child. Set/cleared by control_thread only.
_child_process = None    # type: subprocess.Popen | None
_child_generation = 0    # monotonically increases; child_waiter uses this
                         # to reject stale CHILD_EXITED events

# Restart-cap tracking. Deque of Unix timestamps of recent respawn ATTEMPTS
# (not exits) — a fresh spawn slot within the trailing window costs a slot,
# a crash outside the window does not stack indefinitely.
_respawn_attempts = collections.deque()

# Liveness loop control. The thread runs forever once started; the event
# gates whether it actually does work each tick. We track the thread
# HANDLE (not just a "started" bool) so _ensure_liveness_thread can
# distinguish "still alive, park it via the event" from "died somehow,
# needs to be restarted." A crashed liveness thread with only a bool
# guard would silently stay dead forever — an anti-pattern the doc's
# whole robustness posture warns against.
_liveness_should_run = threading.Event()
_liveness_thread = None    # type: threading.Thread | None
_liveness_thread_lock = threading.Lock()

# The machine name the user gave us on the CLI (or auto-detected). Set
# once at run(); read from many places.
_my_machine_name = None

# Shutdown flag — flipped by SIGTERM/SIGINT handling; accept_loop and
# control_thread watch it.
_shutdown = threading.Event()


# ── intents queued into _control_queue ────────────────────────────────────

class _Intent:
    BOOT = "boot"
    CHILD_EXITED = "child_exited"       # payload: generation (int)
    CONFIG_CHANGED = "config_changed"   # payload: set of changed keys, or None
    TAKEOVER_ATTEMPT = "takeover"       # payload: reason (str)
    START_HOSTING = "start_hosting"     # from Manager — force claim if allowed
    STOP_HOSTING = "stop_hosting"       # from Manager — release and re-arbitrate
    REARBITRATE = "rearbitrate"         # from Manager — re-run without side effects


# ── pointer helpers (accept-path reads via _pointer_rlock) ────────────────

def _set_pointer(**changes):
    """Atomic update — accept-path reads never see a half-modified pointer."""
    with _pointer_rlock:
        _pointer.update(changes)


def _read_pointer():
    """Cheap snapshot copy for the accept path."""
    with _pointer_rlock:
        return dict(_pointer)


# ── bind address selection ───────────────────────────────────────────────

def _preferred_bind_address():
    """
    Try to bind on the Tailscale IPv4 address (§3.1 recommendation).
    Falls back to 0.0.0.0 with a clearly-logged reason on any failure —
    never fails startup over this preference, which is a hardening, not
    a correctness requirement.

    Returns the address string to pass to socket.bind().
    """
    ts_bin = shutil.which("tailscale")
    if not ts_bin:
        print("[front-door] `tailscale` binary not in PATH — binding 0.0.0.0")
        return "0.0.0.0"
    try:
        result = subprocess.run(
            [ts_bin, "ip", "-4"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        print(f"[front-door] `tailscale ip -4` failed ({e}) — binding 0.0.0.0")
        return "0.0.0.0"
    line = (result.stdout or "").strip().splitlines()
    if not line:
        print("[front-door] `tailscale ip -4` returned nothing — binding 0.0.0.0")
        return "0.0.0.0"
    addr = line[0].strip()
    # Sanity check: must look like IPv4. A safety net; if tailscale ever
    # changes the output format we won't bind to a nonsense string.
    parts = addr.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        print(f"[front-door] `tailscale ip -4` returned unexpected {addr!r} — binding 0.0.0.0")
        return "0.0.0.0"
    print(f"[front-door] binding public port {PUBLIC_PORT} on tailnet address {addr}")
    return addr


# ── redirect-serving (absorbed from doorman.py) ──────────────────────────

# Reused connect-body bytes so we don't rebuild them per-request. The
# small-string HTTP responses below are BYTE-BLIND — we choose them
# based on our own pointer state, not on parsing the client's request,
# so they're safe to hand back to any HTTP client, and a WebSocket
# client hitting a non-Local pointer will simply see the 302/503 and
# abort its upgrade attempt (which is the correct behavior anyway —
# there's no local server for it to upgrade to).

def _redirect_response(target_machine, hop):
    location = (
        f"{REDIRECT_SCHEME}://{target_machine}.{TAILNET_SUFFIX}:{PUBLIC_PORT}"
        f"/?hop={hop + 1}"
    )
    body = f"Redirecting to {target_machine}...".encode("utf-8")
    return (
        b"HTTP/1.1 302 Found\r\n"
        + f"Location: {location}\r\n".encode("utf-8")
        + b"Content-Type: text/plain; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("utf-8")
        + b"Connection: close\r\n"
        + b"\r\n"
        + body
    )


def _hop_limit_response():
    body = (b"ChatBucket: sync still catching up between machines. "
            b"Try again in a few seconds.")
    return (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("utf-8")
        + b"Connection: close\r\n"
        + b"\r\n"
        + body
    )


def _unavailable_response():
    body = b"ChatBucket: no host currently active."
    return (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("utf-8")
        + b"Connection: close\r\n"
        + b"\r\n"
        + body
    )


def _peek_hop_from_request(client_sock):
    """
    Best-effort peek for a ?hop=N query param on the request line.
    Reads up to 8 KB, non-consumingly parsed. We do NOT do full HTTP
    parsing — we only look at the first request line, extract the URI,
    and pluck one query param out. Anything unparseable falls back to
    hop=0, which is the safe default (worst case: one extra redirect
    before the loop-protection kicks in).

    Returns (hop:int, raw_bytes_read). The caller has NOT consumed the
    bytes off the socket yet (we peek via MSG_PEEK).
    """
    try:
        client_sock.settimeout(3)
        # MSG_PEEK: read without consuming, so if we ever decided to
        # forward these bytes to a backend they'd still be there. We
        # don't currently — for redirect/unavailable states we close
        # after replying — but not consuming keeps the door open to
        # future changes without a rewrite.
        raw = client_sock.recv(8192, socket.MSG_PEEK)
    except (OSError, TimeoutError):
        return 0
    finally:
        try:
            client_sock.settimeout(None)
        except OSError:
            pass

    try:
        first_line = raw.split(b"\r\n", 1)[0].decode("iso-8859-1")
        # "GET /path?hop=1 HTTP/1.1"
        parts = first_line.split(" ")
        if len(parts) < 2:
            return 0
        uri = parts[1]
        if "?" not in uri:
            return 0
        query = uri.split("?", 1)[1]
        for pair in query.split("&"):
            if pair.startswith("hop="):
                try:
                    return int(pair[len("hop="):])
                except ValueError:
                    return 0
    except Exception:
        return 0
    return 0


# ── connection dispatch (accept-path worker) ─────────────────────────────

def _handle_connection(client_sock):
    """
    Runs in a dedicated worker thread per accepted connection. Reads the
    current pointer and either:
      - Local:       hand off to tcp_proxy.proxy_connection
      - Redirect:    read hop counter, serve 302 (or 503 if MAX_HOPS exceeded)
      - Unavailable: serve 503
    Never holds _supervisor_lock — accept path must not block on
    lifecycle work.
    """
    try:
        snapshot = _read_pointer()
        kind = snapshot["kind"]

        # Race guard: if pointer flipped to Local AFTER our snapshot but
        # BEFORE proxy_connection connects to 5001, proxy_connection's
        # own backend-connect failure branch returns a clean 502 — no
        # extra handling needed here. Same shape covers the reverse
        # (flipped away from Local): connection either lands on a still-
        # running child during the flip and completes, or hits the 502
        # branch. Both fine — the pointer is authoritative for FUTURE
        # connections, in-flight ones are best-effort.

        if kind == "local":
            tcp_proxy.proxy_connection(client_sock, ("127.0.0.1", LOCAL_CHILD_PORT))
            return

        if kind == "redirect":
            target = snapshot.get("machine")
            if not target:
                # Shouldn't happen (pointer.kind=redirect implies a machine
                # was set) but defensively degrade rather than crashing the
                # worker.
                client_sock.sendall(_unavailable_response())
                return
            hop = _peek_hop_from_request(client_sock)
            if hop > MAX_REDIRECT_HOPS:
                client_sock.sendall(_hop_limit_response())
                return
            client_sock.sendall(_redirect_response(target, hop))
            return

        # Unavailable (including transient "starting" — see comment above _Intent).
        client_sock.sendall(_unavailable_response())
    except (BrokenPipeError, ConnectionError, OSError):
        # Client hung up mid-write, or a keepalive timed out under us.
        # Nothing to recover; drop.
        pass
    finally:
        try:
            client_sock.close()
        except OSError:
            pass


# ── accept loop ──────────────────────────────────────────────────────────

def _accept_loop(listen_sock):
    """
    Runs forever (until _shutdown). One accept -> one worker thread. Does
    NOT touch pointer/supervisor state directly.
    """
    while not _shutdown.is_set():
        try:
            client_sock, _addr = listen_sock.accept()
        except OSError:
            # Socket closed on shutdown path.
            return
        threading.Thread(
            target=_handle_connection,
            args=(client_sock,),
            name="fd-conn",
            daemon=True,
        ).start()


# ── child supervision ────────────────────────────────────────────────────

def _spawn_child_command():
    """
    Return the argv list to Popen for the local server child.

    Mirrors the existing platform selection logic from main.py's old
    _exec_host_server(): gunicorn+gevent on POSIX when available (prod
    path), else python server.py directly (Werkzeug threaded dev server,
    the ONE non-gunicorn server flask_sock documents as compatible).

    Bind changes from 0.0.0.0:5000 to 127.0.0.1:5001 — the child must not
    be reachable directly from the tailnet; the front door is the only
    path in.
    """
    server_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "server.py"
    )
    if os.name != "nt":
        gunicorn_bin = shutil.which("gunicorn")
        if gunicorn_bin:
            return [
                gunicorn_bin,
                "-k", "gevent",
                "-w", "1",
                "-b", f"127.0.0.1:{LOCAL_CHILD_PORT}",
                # --graceful-timeout stays 5 for the same reasoning as
                # before: WebSockets never voluntarily "finish," so the
                # default 30 just eats time on every graceful stop.
                "--graceful-timeout", "5",
                "server:app",
            ]

    # Fallback: exec `python server.py`, which starts Werkzeug on the
    # SERVER-side default of 0.0.0.0:5000. server.py has been updated to
    # bind 127.0.0.1:5001 in its __main__ block — see the note in that
    # file. If for any reason that hasn't happened, this WILL fail to
    # start correctly, which is the visible-failure direction we want
    # (rather than binding a wrong port silently).
    return [sys.executable, server_path]


def _wait_for_child_port(child, port, timeout=15):
    """
    Poll-wait for the child to actually bind its port before we flip the
    pointer to Local. This is what keeps a race-in-connections from being
    a real problem: if we flipped to Local the instant we called Popen,
    the very first accepted connection could reach tcp_proxy BEFORE the
    child had bound :5001, hitting the 502 branch even though startup is
    proceeding normally. Polling loopback with a short timeout is cheap
    and eliminates that window.

    If the child dies during startup, child.poll() returns non-None and
    we abort waiting immediately.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if child.poll() is not None:
            return False  # child died during startup
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except (ConnectionRefusedError, TimeoutError, OSError):
            time.sleep(0.15)
    return False


def _spawn_local_child():
    """
    Called by control_thread with _supervisor_lock held. Starts the child,
    waits for it to bind, and (on success) returns the Popen handle plus
    a fresh generation number. Returns (None, None) on failure.
    """
    global _child_generation
    argv = _spawn_child_command()
    print(f"[front-door] spawning local child: {' '.join(argv)}")
    try:
        child = subprocess.Popen(
            argv,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            # Inherit stdout/stderr on purpose so operator sees the child's
            # startup logs interleaved with ours. Same UX as the old execv
            # design.
        )
    except OSError as e:
        print(f"[front-door] Popen failed: {e}")
        return None, None

    if not _wait_for_child_port(child, LOCAL_CHILD_PORT):
        print(f"[front-door] child did not bind {LOCAL_CHILD_PORT} in time")
        # Best-effort cleanup — the child either died on its own or is
        # wedged; either way we're not going to use it.
        try:
            child.terminate()
            child.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                child.kill()
            except OSError:
                pass
        return None, None

    _child_generation += 1
    my_generation = _child_generation
    print(f"[front-door] child up on 127.0.0.1:{LOCAL_CHILD_PORT} "
          f"(pid={child.pid}, gen={my_generation})")

    # Waiter thread — blocks on child.wait() and posts one CHILD_EXITED
    # event when it returns. Event-driven kernel-level notification; no
    # polling.
    def _waiter():
        try:
            child.wait()
        except Exception:
            pass
        # PID reuse guard: post the generation this child had, not just
        # its PID. control_thread ignores stale generations.
        _control_queue.put((_Intent.CHILD_EXITED, my_generation))

    threading.Thread(target=_waiter, name="fd-child-waiter", daemon=True).start()
    return child, my_generation


def _stop_local_child(child, reason):
    """
    Graceful stop — terminate(), wait CHILD_STOP_GRACEFUL_SECONDS,
    escalate to kill() only if needed. Called by control_thread with
    _supervisor_lock held.
    """
    if child is None:
        return
    print(f"[front-door] stopping local child (pid={child.pid}): {reason}")
    try:
        child.terminate()
    except OSError:
        pass
    try:
        child.wait(timeout=CHILD_STOP_GRACEFUL_SECONDS)
        print(f"[front-door] child exited gracefully")
        return
    except subprocess.TimeoutExpired:
        pass
    print(f"[front-door] child did not exit within "
          f"{CHILD_STOP_GRACEFUL_SECONDS}s — escalating to kill")
    try:
        child.kill()
        child.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


# ── liveness loop (client-side takeover trigger) ─────────────────────────

def _health_check(machine_name):
    """
    Same primitive arbitration.py uses for its own tiebreaker health
    check, deliberately duplicated in shape rather than imported: the
    liveness loop's failure mode is "the URL didn't answer 200 in time,"
    and calling into arbitration._real_health_checker would just be one
    more indirection for the same call. Kept explicit here so debugging
    a false-takeover reads straight down.
    """
    url = (f"{REDIRECT_SCHEME}://{machine_name}.{TAILNET_SUFFIX}:"
           f"{PUBLIC_PORT}/health")
    try:
        with urllib.request.urlopen(url, timeout=LIVENESS_HEALTH_TIMEOUT) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _liveness_loop():
    """
    Runs when _liveness_should_run is set. Sleeps LIVENESS_INTERVAL_SECONDS
    between checks; a consecutive-failure counter drives the debounce.

    Re-reads manager_config.read() each iteration so a toggle flip takes
    effect within one interval without requiring a restart or an explicit
    kick. The main loop already fires a CONFIG_CHANGED intent when the
    control endpoint updates config, which sets/clears
    _liveness_should_run — this per-iteration re-read is a belt-and-braces
    safety in case the file was hand-edited without going through the
    endpoint.
    """
    consecutive_failures = 0
    while not _shutdown.is_set():
        # Wait until we're allowed to run. This is what makes "disabled"
        # cost literally zero CPU — the thread parks on this event
        # indefinitely instead of ticking-and-noop'ing each interval.
        if not _liveness_should_run.wait(timeout=1):
            consecutive_failures = 0
            continue

        snapshot = _read_pointer()
        if snapshot["kind"] != "redirect":
            consecutive_failures = 0
            # Not our job to run while local/unavailable. Wait a tick
            # rather than a full interval — pointer transitions should
            # start us watching again promptly.
            time.sleep(1)
            continue

        cfg = manager_config.read()
        if not cfg["take_host_on_crash"]:
            _liveness_should_run.clear()
            consecutive_failures = 0
            continue

        target = snapshot.get("machine")
        if not target:
            consecutive_failures = 0
            time.sleep(LIVENESS_INTERVAL_SECONDS)
            continue

        if _health_check(target):
            if consecutive_failures:
                print(f"[liveness] {target} recovered after "
                      f"{consecutive_failures} miss(es)")
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            print(f"[liveness] {target} health check failed "
                  f"({consecutive_failures}/{LIVENESS_FAILURES_TO_TAKEOVER})")
            if consecutive_failures >= LIVENESS_FAILURES_TO_TAKEOVER:
                print(f"[liveness] triggering takeover attempt")
                consecutive_failures = 0
                _control_queue.put((_Intent.TAKEOVER_ATTEMPT, target))
                # Back off after triggering — the control thread's
                # response (either success -> pointer=local, or failure
                # -> stays redirect) will drive the loop's next state.
                # Sleeping the full interval avoids hammering the queue
                # while arbitration/jitter runs.

        # Sleep in short ticks so shutdown / config change flips take
        # effect within ~1s rather than at the end of a full 20s wait.
        slept = 0
        while slept < LIVENESS_INTERVAL_SECONDS:
            if _shutdown.is_set() or not _liveness_should_run.is_set():
                break
            time.sleep(1)
            slept += 1


def _ensure_liveness_thread():
    """Start the liveness thread if it isn't running, or has died. This is
    idempotent for the common case (already-running thread) and
    self-healing for the pathological one (thread died from an unhandled
    exception, or the process-wide _shutdown flag was cycled by tests).
    """
    global _liveness_thread
    with _liveness_thread_lock:
        if _liveness_thread is not None and _liveness_thread.is_alive():
            return
        t = threading.Thread(
            target=_liveness_loop, name="fd-liveness", daemon=True,
        )
        t.start()
        _liveness_thread = t


def _update_liveness_gate():
    """Enable/disable the loop based on current pointer + config."""
    cfg = manager_config.read()
    snapshot = _read_pointer()
    should_run = (snapshot["kind"] == "redirect"
                  and cfg["take_host_on_crash"]
                  and cfg["auto_host"])   # takeover implies claim-permission
    if should_run:
        _ensure_liveness_thread()
        _liveness_should_run.set()
    else:
        _liveness_should_run.clear()


# ── control thread: the single serialized decision-maker ─────────────────

def _port_5001_free():
    """The port-free check the front door passes into arbitration in place
    of arbitration's default :5000 check. See the note in arbitration.py's
    updated should_i_be_host docstring for why."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", LOCAL_CHILD_PORT))
            return True
        except OSError:
            return False


def _do_arbitrate_and_apply(force_claim_attempt=False):
    """
    Called by control_thread WITH _supervisor_lock HELD. Reads config,
    calls arbitration if permitted, applies the result by flipping the
    pointer and (if needed) spawning/stopping the child.

    force_claim_attempt: from START_HOSTING — asks arbitration to try
    even when auto_host is nominally off; used by the Manager's "Start"
    button. Distinguished from the normal auto_host=on path because the
    doc's truth table forbids passive claiming when auto_host is off,
    but the user pressing Start is EXPLICIT permission.
    """
    global _child_process, _respawn_attempts

    cfg = manager_config.read()

    # No permission to host? Read state, decide redirect vs unavailable.
    if not cfg["auto_host"] and not force_claim_attempt:
        _decide_client_pointer()
        return

    _set_pointer(kind="unavailable", machine=None, child_pid=None,
                 starting=True, last_error=None)

    try:
        is_host = arbitration.should_i_be_host(
            _my_machine_name,
            port_free_checker=_port_5001_free,
        )
    except arbitration.ArbitrationError as e:
        print(f"[control] arbitration error: {e}")
        _set_pointer(kind="unavailable", machine=None, child_pid=None,
                     starting=False, last_error=str(e))
        _update_liveness_gate()
        return

    if is_host:
        # Arbitration wrote {"action":"start", "machine": me}. Spawn child.
        # If the child spawn fails, we walk it back — we're the machine
        # that just claimed and we cannot back that claim.
        child, _gen = _spawn_local_child()
        if child is None:
            _set_pointer(kind="unavailable", machine=None, child_pid=None,
                         starting=False,
                         last_error="failed to start local ChatBucket server")
            # Walk back the claim so someone else can pick it up. Same
            # spirit as arbitration._port_is_free — don't leave a claim
            # you can't back.
            try:
                host_state.write_state("stop", _my_machine_name)
            except host_state.HostStateError as e:
                print(f"[control] could not walk back claim: {e}")
            _update_liveness_gate()
            return
        _child_process = child
        _respawn_attempts.clear()  # fresh cycle
        _set_pointer(kind="local", machine=_my_machine_name,
                     child_pid=child.pid, starting=False, last_error=None)
        _update_liveness_gate()
        return

    # Arbitration says defer. Read state to find who to point at.
    _decide_client_pointer()


def _decide_client_pointer():
    """
    Not-hosting branch: consult host-state.json (which arbitration may
    have just healed) and flip pointer to Redirect(host) or Unavailable.
    """
    try:
        state = host_state.read_state()
    except host_state.HostStateError as e:
        print(f"[control] host-state corrupted: {e}")
        _set_pointer(kind="unavailable", machine=None, child_pid=None,
                     starting=False, last_error=str(e))
        _update_liveness_gate()
        return

    if state is None or state.get("action") != "start":
        _set_pointer(kind="unavailable", machine=None, child_pid=None,
                     starting=False, last_error=None)
    elif state["machine"] == _my_machine_name:
        # State says I'm host, but arbitration told us to defer (or we're
        # not permitted) — treat as unavailable rather than pointing at
        # ourselves. Extremely rare in practice; belt-and-braces.
        _set_pointer(kind="unavailable", machine=None, child_pid=None,
                     starting=False,
                     last_error="host-state names this machine but hosting is not active")
    else:
        _set_pointer(kind="redirect", machine=state["machine"],
                     child_pid=None, starting=False, last_error=None)

    _update_liveness_gate()


def _release_hosting():
    """
    Stop being host. Called for STOP_HOSTING and for auto_host-flipped-off.
    Held with _supervisor_lock. Sequence per the doc §5.8:
      1. write "stop" to host-state.json
      2. gracefully stop local child
      3. re-arbitrate (either transitions to Redirect or Unavailable)
    Port 5000 never drops.
    """
    global _child_process
    try:
        host_state.write_state("stop", _my_machine_name)
    except host_state.HostStateError as e:
        print(f"[control] could not write stop claim: {e}")

    if _child_process is not None:
        _stop_local_child(_child_process, "hosting released")
        _child_process = None

    # Immediately transition to unavailable so in-flight accept-path
    # workers see the change; then re-arbitrate to find whether someone
    # else picks up. Order matters: don't leave a stale Local pointer
    # between the child dying and re-arbitration deciding.
    _set_pointer(kind="unavailable", machine=None, child_pid=None,
                 starting=True, last_error=None)

    # Now re-arbitrate without forcing a claim — we JUST released, we
    # don't want to immediately re-claim on the same tick. If no one
    # else takes over, some future trigger (their liveness loop, a
    # reboot, a manager click) will eventually resolve. That's the
    # documented behavior.
    _decide_client_pointer()


def _within_respawn_cap():
    """Return True if we can attempt another respawn under the cap."""
    now = time.time()
    # Drop attempts outside the trailing window.
    while _respawn_attempts and (now - _respawn_attempts[0]) > RESPAWN_WINDOW_SECONDS:
        _respawn_attempts.popleft()
    return len(_respawn_attempts) < RESPAWN_MAX_ATTEMPTS


def _handle_child_exited(exited_generation):
    """
    Runs on control_thread with _supervisor_lock HELD.
    """
    global _child_process
    # Stale event guard: an OLD child's waiter thread could fire this after
    # we've already replaced the child. If the generation doesn't match
    # what's currently live, ignore.
    if _child_process is None:
        # We already tore down. Nothing to do.
        return
    if exited_generation != _child_generation:
        print(f"[control] ignoring stale CHILD_EXITED gen={exited_generation} "
              f"(current gen={_child_generation})")
        return

    old_pid = _child_process.pid
    _child_process = None
    print(f"[control] local child (pid={old_pid}) exited unexpectedly")

    _set_pointer(kind="unavailable", machine=_my_machine_name,
                 child_pid=None, starting=True,
                 last_error="local server exited; respawning")

    if not _within_respawn_cap():
        print(f"[control] respawn cap ({RESPAWN_MAX_ATTEMPTS} in "
              f"{RESPAWN_WINDOW_SECONDS}s) exceeded — re-arbitrating instead")
        _respawn_attempts.clear()
        _set_pointer(last_error=(
            f"local server crashed {RESPAWN_MAX_ATTEMPTS} times within "
            f"{RESPAWN_WINDOW_SECONDS}s; giving up on local hosting"))
        # Release cleanly so someone else can pick up.
        try:
            host_state.write_state("stop", _my_machine_name)
        except host_state.HostStateError as e:
            print(f"[control] could not walk back claim after respawn cap: {e}")
        _decide_client_pointer()
        return

    _respawn_attempts.append(time.time())
    child, _gen = _spawn_local_child()
    if child is None:
        # Failed spawn counts as a used attempt (already appended). If we
        # still have budget, the NEXT trigger (typically another
        # CHILD_EXITED if we did briefly spawn, or a future manual
        # kick) can try again; here, without a running child, we don't
        # get another automatic try until something else changes.
        # Concretely: post ourselves a re-check so we don't stall.
        _set_pointer(kind="unavailable", machine=None, child_pid=None,
                     starting=False, last_error="respawn failed")
        _decide_client_pointer()
        return

    _child_process = child
    _set_pointer(kind="local", machine=_my_machine_name,
                 child_pid=child.pid, starting=False, last_error=None)
    _update_liveness_gate()


def _control_thread():
    """
    The single serialized lifecycle-decision loop. All lifecycle
    mutations happen here under _supervisor_lock.
    """
    # Boot intent kicks off the initial arbitration.
    _control_queue.put((_Intent.BOOT, None))

    while not _shutdown.is_set():
        try:
            intent, payload = _control_queue.get(timeout=1)
        except queue.Empty:
            continue

        with _supervisor_lock:
            try:
                if intent == _Intent.BOOT:
                    _do_arbitrate_and_apply()
                elif intent == _Intent.CHILD_EXITED:
                    _handle_child_exited(payload)
                elif intent == _Intent.CONFIG_CHANGED:
                    _react_to_config_change(payload)
                elif intent == _Intent.TAKEOVER_ATTEMPT:
                    _do_arbitrate_and_apply()
                elif intent == _Intent.START_HOSTING:
                    _do_arbitrate_and_apply(force_claim_attempt=True)
                elif intent == _Intent.STOP_HOSTING:
                    snapshot = _read_pointer()
                    if snapshot["kind"] == "local":
                        _release_hosting()
                    else:
                        # Nothing to release — but the user pressing Stop
                        # is a strong signal they want re-evaluation.
                        _decide_client_pointer()
                elif intent == _Intent.REARBITRATE:
                    _do_arbitrate_and_apply()
                else:
                    print(f"[control] unknown intent: {intent!r}")
            except Exception as e:
                # A bug in ANY branch above must never take the control
                # thread down — that would strand the pointer and the
                # child. Log loudly and continue.
                import traceback
                print(f"[control] intent {intent!r} raised: {e}")
                traceback.print_exc()
                _set_pointer(last_error=f"internal error: {e}")


def _react_to_config_change(changed_keys=None):
    """
    Auto_host and/or take_host_on_crash flipped. Rules per the truth
    table in §3.5:
      - auto_host flipped OFF while currently local -> release hosting
      - auto_host flipped ON while currently redirect/unavailable
        -> re-arbitrate (may claim if nobody's live)
      - take_host_on_crash flipped -> just update the liveness gate

    changed_keys: set of config keys whose values actually changed on
    this update. When None (a manual/hand-edited-file kick from outside
    the control endpoint), we assume both keys may have changed and
    fall back to the more conservative "react to auto_host too" path.
    When provided (the normal path — control endpoint tells us exactly
    which keys the caller wrote), we only run auto_host-related logic
    when auto_host actually flipped. Without this discrimination, a
    take_host_on_crash-only flip while auto_host=True would incorrectly
    trigger re-arbitration and could bounce a healthy redirect pointer
    off to unavailable if arbitration transiently failed.
    """
    cfg = manager_config.read()
    snapshot = _read_pointer()
    auto_host_changed = changed_keys is None or "auto_host" in changed_keys

    if auto_host_changed and snapshot["kind"] == "local" and not cfg["auto_host"]:
        print("[control] auto_host was disabled while hosting — releasing")
        _release_hosting()
        return

    if auto_host_changed and snapshot["kind"] != "local" and cfg["auto_host"]:
        # Might now be able to claim. Re-arbitrate.
        _do_arbitrate_and_apply()
        return

    # Only the takeover toggle changed (or nothing relevant); just refresh
    # the liveness gate.
    _update_liveness_gate()


# ── loopback status / control endpoint ───────────────────────────────────

class _StatusRequestHandler(http.server.BaseHTTPRequestHandler):
    """
    Loopback-only interface for the future Rust Manager. Two endpoints
    exposed, kept deliberately small:
      GET /status  — current pointer, child pid, toggles, last error
      POST /control  — {"action": "start"|"stop"|"rearbitrate"|"set_config",
                        "config": {"auto_host": bool,
                                   "take_host_on_crash": bool}}

    We do not build any UI or manager client here — this is only the
    interface that will let the existing Rust Manager (integrated later)
    ask "what's going on" and issue "please do this" without needing to
    scan ps output.
    """

    # Silence the default per-request log line; we log via prints above.
    def log_message(self, fmt, *args):
        return

    def _write_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/status":
            self._write_json(404, {"error": "not found"})
            return
        snapshot = _read_pointer()
        cfg = manager_config.read()
        state_kind = snapshot["kind"]
        if state_kind == "redirect":
            routing = f"redirect:{snapshot.get('machine') or ''}"
        elif state_kind == "local":
            routing = "local"
        else:
            routing = "unavailable"
        self._write_json(200, {
            "routing": routing,
            "machine": _my_machine_name,
            "child_running": snapshot["kind"] == "local"
                             and snapshot.get("child_pid") is not None,
            "child_pid": snapshot.get("child_pid"),
            "starting": snapshot.get("starting", False),
            "last_error": snapshot.get("last_error"),
            "auto_host": cfg["auto_host"],
            "take_host_on_crash": cfg["take_host_on_crash"],
        })

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/control":
            self._write_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._write_json(400, {"error": "invalid JSON"})
            return
        if not isinstance(body, dict):
            self._write_json(400, {"error": "body must be object"})
            return
        action = body.get("action")

        if action == "start":
            _control_queue.put((_Intent.START_HOSTING, None))
            self._write_json(202, {"accepted": True})
            return
        if action == "stop":
            _control_queue.put((_Intent.STOP_HOSTING, None))
            self._write_json(202, {"accepted": True})
            return
        if action == "rearbitrate":
            _control_queue.put((_Intent.REARBITRATE, None))
            self._write_json(202, {"accepted": True})
            return
        if action == "set_config":
            cfg_updates = body.get("config") or {}
            allowed = {}
            for k in ("auto_host", "take_host_on_crash"):
                if k in cfg_updates and isinstance(cfg_updates[k], bool):
                    allowed[k] = cfg_updates[k]
            if not allowed:
                self._write_json(400, {"error": "no valid config keys provided"})
                return
            # Enforce the truth-table invariant server-side even if the
            # UI let it through: take_host_on_crash cannot be true while
            # auto_host is false.
            new_cfg = dict(manager_config.read())
            new_cfg.update(allowed)
            if new_cfg["take_host_on_crash"] and not new_cfg["auto_host"]:
                self._write_json(400, {
                    "error": "take_host_on_crash requires auto_host=true",
                })
                return
            # Compute which keys actually changed value (idempotent
            # writes should not trigger re-arbitration — the doc's
            # truth-table transitions fire on genuine flips only).
            before = manager_config.read()
            changed_keys = {k for k, v in allowed.items() if before.get(k) != v}
            try:
                manager_config.update(**allowed)
            except (ValueError, OSError) as e:
                self._write_json(500, {"error": f"config write failed: {e}"})
                return
            if changed_keys:
                _control_queue.put((_Intent.CONFIG_CHANGED, changed_keys))
            self._write_json(202, {"accepted": True, "config": manager_config.read()})
            return

        self._write_json(400, {"error": f"unknown action {action!r}"})


class _LoopbackHTTPServer(http.server.ThreadingHTTPServer):
    # Bind to loopback ONLY — never the tailnet interface. If this ever
    # starts binding 0.0.0.0 by mistake we've silently exposed a control
    # channel to the whole tailnet.
    address_family = socket.AF_INET


def _start_status_server():
    server = _LoopbackHTTPServer(("127.0.0.1", STATUS_PORT), _StatusRequestHandler)
    thread = threading.Thread(
        target=server.serve_forever, name="fd-status", daemon=True,
    )
    thread.start()
    print(f"[front-door] status/control endpoint on 127.0.0.1:{STATUS_PORT}")
    return server


# ── entry point ──────────────────────────────────────────────────────────

def run(my_machine_name):
    """
    Blocking. Binds public port 5000 first, then starts all subsystems.
    Returns only when _shutdown is set (SIGTERM/SIGINT).
    """
    global _my_machine_name
    _my_machine_name = my_machine_name

    # 1. Bind public port FIRST. Nothing else runs if this fails —
    # that's the correct posture (the whole point of the front door is
    # that this port is bound).
    bind_addr = _preferred_bind_address()
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listen_sock.bind((bind_addr, PUBLIC_PORT))
    except OSError as e:
        print(f"[front-door] FATAL: could not bind {bind_addr}:{PUBLIC_PORT}: {e}")
        print(f"[front-door] most common cause: a leftover ChatBucket process. "
              f"Check `ss -ltnp | grep {PUBLIC_PORT}` (or on Windows, "
              f"`netstat -ano | findstr :{PUBLIC_PORT}`) and kill it, then retry.")
        return 2
    listen_sock.listen(64)
    print(f"[front-door] public port {PUBLIC_PORT} bound on {bind_addr}")

    # 2. Kick off the status/control endpoint EARLY — the Rust Manager
    # should see /status respond even during the initial arbitration.
    try:
        _start_status_server()
    except OSError as e:
        # Non-fatal: the front door itself keeps running; only the
        # Manager interface goes dark.
        print(f"[front-door] status endpoint failed to start: {e}")

    # 3. Accept loop.
    threading.Thread(
        target=_accept_loop, args=(listen_sock,),
        name="fd-accept", daemon=True,
    ).start()

    # 4. Liveness thread (created lazily on first enable — see
    # _ensure_liveness_thread — but the module never leaves it running
    # while disabled anyway).

    # 5. Control thread. This is where arbitration first runs (via BOOT).
    control = threading.Thread(
        target=_control_thread, name="fd-control", daemon=True,
    )
    control.start()

    # 6. Wait for shutdown. On SIGTERM/SIGINT, close the listen socket
    # (breaks accept) and gracefully stop the child if any.
    import signal

    def _on_signal(signum, frame):
        print(f"[front-door] received signal {signum}, shutting down")
        _shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            # signal() only usable from main thread on Windows; we ARE
            # main thread here so this normally succeeds. Fall through
            # silently on the rare exception.
            pass

    try:
        while not _shutdown.is_set():
            _shutdown.wait(timeout=1)
    finally:
        print("[front-door] shutdown: closing listen socket")
        try:
            listen_sock.close()
        except OSError:
            pass
        # Best-effort child stop under supervisor lock so a mid-shutdown
        # CHILD_EXITED event doesn't race a respawn attempt.
        with _supervisor_lock:
            if _child_process is not None:
                _stop_local_child(_child_process, "front door shutting down")

    return 0
