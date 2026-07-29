"""
main.py — ChatBucket startup entrypoint.

Runs arbitration once, then launches exactly one of:
  - server.py   (full chat app)      — if this machine is host
  - doorman.py  (redirect-only)      — if this machine is client

§4 invariant: port 5000 must ALWAYS end up bound by something that
answers correctly, regardless of host/client result — neither branch
below is allowed to leave nothing bound. See
ChatBucket_Networking_Architecture.md §4 for the wrong-way-to-do-it
example this guards against.

Host launch is cross-platform:
  * POSIX with Gunicorn installed → Gunicorn + gevent worker (prod path).
  * Otherwise (Windows, or POSIX without Gunicorn) → exec server.py
    directly, hitting its own `if __name__ == "__main__"` block, which
    starts the Werkzeug threaded dev server. Werkzeug is one of the
    servers flask_sock documents as WebSocket-compatible, so this is a
    deliberate choice, not a last-resort fallback. Waitress and bare
    gevent.pywsgi are NOT on that list and would silently break the
    entire chat transport (all traffic goes through @sock.route("/ws"),
    there is no HTTP-polling fallback).

Usage:
    python3 main.py [my-machine-name]
"""
import os
import sys
import shutil
import socket
import platform
import arbitration


def normalize(name: str) -> str:
    return name.split(".")[0].strip().lower()


def get_machine_name() -> str:
    name = (
        socket.gethostname()
        or os.getenv("COMPUTERNAME")
        or os.getenv("HOSTNAME")
        or platform.node()
    )
    return normalize(name)


def _exec_host_server():
    """
    Replace this process with the appropriate host-side server.
    Never returns on success — os.execv replaces the process image.

    POSIX: Gunicorn + gevent worker (existing prod path, unchanged).
    Non-POSIX / gunicorn missing: exec server.py directly, so its own
    `if __name__ == "__main__"` block runs the Werkzeug threaded dev
    server. This is the one non-Gunicorn server flask_sock documents as
    compatible; Waitress and bare gevent.pywsgi lack the socket-hijack
    path flask_sock's WS upgrade needs, and would leave chat silently
    broken while port 5000 stayed bound. For a 3-person tailnet-only
    deployment, dev-server hardening is a non-issue per ChatBucket's
    own complexity budget.
    """
    if os.name != "nt":
        gunicorn_bin = shutil.which("gunicorn")
        if gunicorn_bin:
            print("[main] Launching server.py via Gunicorn (gevent worker)")
            os.execv(gunicorn_bin, [
                gunicorn_bin,
                "-k", "gevent",
                "-w", "1",
                "-b", "0.0.0.0:5000",
                # Gunicorn's default --graceful-timeout is 30s: on
                # SIGTERM it waits this long for "in-flight work" to
                # finish before force-killing the worker itself. A
                # WebSocket connection via flask_sock is indefinitely
                # open — it never voluntarily "finishes" — so the
                # default just means every stop silently eats up to
                # 30s for no benefit. 5s is enough for a real in-flight
                # HTTP request (e.g. an /upload) to complete normally;
                # anything still open past that is a WS connection that
                # was never going to close on its own anyway.
                "--graceful-timeout", "5",
                "server:app",
            ])
            # never returns on success

    print("[main] Launching server.py directly (Werkzeug dev server, threaded)")
    server_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "server.py"
    )
    os.execv(sys.executable, [sys.executable, server_path])


def main():
    if len(sys.argv) > 2:
        print("Usage: python3 main.py [my-machine-name]")
        sys.exit(1)
    my_name = normalize(sys.argv[1]) if len(sys.argv) == 2 else get_machine_name()
    print(f"Detected machine name: {my_name}")

    try:
        is_host = arbitration.should_i_be_host(my_name)
    except arbitration.ArbitrationError as e:
        print(f"ARBITRATION FAILED: {e}")
        print(
            "Refusing to start anything. A wrong guess here (host vs. "
            "client) risks either split-brain or an unreachable bookmark — "
            "both worse than not starting. Fix the underlying issue "
            "(check `tailscale status`, check state/host-state.json by "
            "hand) and re-run."
        )
        sys.exit(2)

    if is_host:
        print(f"[{my_name}] Arbitration result: HOST -> starting server.py")
        _exec_host_server()
    else:
        print(f"[{my_name}] Arbitration result: CLIENT -> starting doorman.py")
        os.execv(sys.executable, [sys.executable, "doorman.py", my_name])


if __name__ == "__main__":
    main()
