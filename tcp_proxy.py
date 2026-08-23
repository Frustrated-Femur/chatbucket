"""
tcp_proxy.py — Per-connection bidirectional TCP byte-splice.

Not a listener. This module provides one function — proxy_connection() —
that takes an already-accepted client socket and a backend (host, port)
tuple, connects to the backend, and copies bytes in both directions until
either side closes. See ChatBucket_Networking_Architecture (front-door
supersession) §3.2.

Deliberately NOT an HTTP/WebSocket-aware proxy: it never parses any bytes
it copies. That's the whole point — flask_sock's WebSocket transport relies
on the underlying WSGI server's socket-hijacking path, and any HTTP-aware
reverse proxy layer between them is a documented source of WebSocket-
upgrade fragility across arbitrary backends. A byte-blind splice sidesteps
that entire category of bug by construction.

Why per-connection, not a self-contained listener:
The front door owns port 5000 for its entire lifetime (that's the whole
architectural point of the front door). It runs one accept() loop and
dispatches each accepted connection to either this proxy — when the
pointer is Local — or to a redirect/unavailable handler — otherwise. If
this module ran its OWN listener, transitioning between Local and Redirect
would require rebinding 5000, which reopens the exact "port briefly goes
dark" window the front door design exists to close.

Thread model:
Two threads per active connection (one per direction). Not asyncio — for
a 2-3 user tailnet the extra concurrency infra is pure complexity we don't
need, and thread-per-connection is trivial to reason about when debugging
a stuck WebSocket at 2am.
"""

import errno
import socket
import threading

# Enough to move a WebSocket frame or an /upload chunk without either
# starving the loop or gulping suspiciously large amounts of memory per
# connection. This is a copy buffer, not an application-layer buffer;
# larger values do not translate to higher throughput at this scale.
_BUFSIZE = 65536

# Applied to both the accepted client socket and the backend socket. A
# wedged half-open TCP (peer power-cycled without TCP FIN reaching us)
# would otherwise leave the forwarder thread parked in recv() forever.
_KEEPALIVE_IDLE = 60      # seconds of inactivity before probes start
_KEEPALIVE_INTVL = 15     # seconds between probes
_KEEPALIVE_CNT = 4        # unanswered probes before the kernel gives up


# 502 written back to the client ONLY when the backend connect itself
# failed and we have not yet forwarded any bytes in either direction.
# This is a courtesy — a bare TCP close would also be correct — and it
# gives the caller's browser a visible error instead of an unexplained
# connection reset. Kept short and text/plain to avoid pretending to be
# an HTTP-aware layer we're not; a real HTTP client will just show the
# body, a WebSocket client will see garbage and close, both fine.
_BACKEND_UNAVAILABLE_RESPONSE = (
    b"HTTP/1.1 502 Bad Gateway\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Connection: close\r\n"
    b"Content-Length: 62\r\n"
    b"\r\n"
    b"ChatBucket: local server is not reachable right now.\r\n"
)


def _apply_keepalive(sock):
    """Best-effort TCP keepalive. Silently no-ops on platforms lacking
    the per-socket knobs (macOS pre-10.13 style, some minimal Windows
    builds). The base SO_KEEPALIVE is universally supported; the
    per-connection tuning is a bonus, not a requirement."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    for opt_name, value in (
        ("TCP_KEEPIDLE", _KEEPALIVE_IDLE),
        ("TCP_KEEPINTVL", _KEEPALIVE_INTVL),
        ("TCP_KEEPCNT", _KEEPALIVE_CNT),
    ):
        opt = getattr(socket, opt_name, None)
        if opt is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError:
            pass


def _shutdown_side(sock, how):
    """Half-shutdown that ignores 'already closed' / 'not connected'
    errors — those are expected racing with the other direction's
    forwarder tearing everything down."""
    try:
        sock.shutdown(how)
    except OSError as e:
        if e.errno not in (errno.ENOTCONN, errno.EBADF, errno.EPIPE):
            # Anything else is genuinely unexpected — surface via
            # logging but don't raise; we're on a teardown path.
            pass


def _forward(src, dst, done_event):
    """
    Copy bytes src -> dst until src EOFs or errors. On finish, half-close
    dst for writing so the other-direction forwarder learns the peer is
    done sending (WebSocket close frame propagation depends on this — a
    hard-close of the whole socket instead would truncate any in-flight
    reply the other side hasn't finished writing yet).
    """
    try:
        while True:
            try:
                chunk = src.recv(_BUFSIZE)
            except (ConnectionError, TimeoutError, OSError):
                break
            if not chunk:
                break
            try:
                dst.sendall(chunk)
            except (ConnectionError, BrokenPipeError, OSError):
                break
    finally:
        _shutdown_side(dst, socket.SHUT_WR)
        done_event.set()


def proxy_connection(client_sock, backend_addr, backend_connect_timeout=5.0):
    """
    Splice bytes between `client_sock` (already-accepted) and a fresh
    connection to `backend_addr` (an (ip, port) tuple, typically
    ("127.0.0.1", 5001)).

    Blocks in the CALLER's thread until both directions finish. The front
    door dispatches each accepted connection to its own worker thread and
    calls this from there; nothing here spawns a thread for the connection
    itself, only for the second copy direction.

    Failure modes handled:
    - backend refuses/times out: send a small 502-ish reply, close, return.
    - backend closes first: client's outbound half is half-shut, client's
      final write drains, then teardown.
    - client closes first: mirror.
    - either side errors mid-transfer: close both, return.

    Never re-raises: the front door's accept loop calls this in a worker
    thread and treats every completion as "this connection is done." An
    unhandled exception escaping here would kill the worker thread with
    a stack trace but not the front door itself; still, silence + close
    is the correct posture here since the caller has no useful recovery
    for a mid-transfer TCP failure.
    """
    _apply_keepalive(client_sock)

    try:
        backend_sock = socket.create_connection(
            backend_addr, timeout=backend_connect_timeout,
        )
    except (ConnectionRefusedError, TimeoutError, OSError):
        # Backend is down (child not started yet, or already exited).
        # Send a courtesy 502; ignore write errors here — the client may
        # have hung up already, and either way we're closing next.
        try:
            client_sock.sendall(_BACKEND_UNAVAILABLE_RESPONSE)
        except OSError:
            pass
        try:
            client_sock.close()
        except OSError:
            pass
        return

    # Clear the connect-timeout on the backend socket; forwarding uses
    # blocking recv() with no per-call deadline. Keepalive above is what
    # bounds a hung idle socket.
    backend_sock.settimeout(None)
    _apply_keepalive(backend_sock)

    c2b_done = threading.Event()
    b2c_done = threading.Event()

    # One of the two directions runs in the caller's thread; the other
    # gets its own worker. Fewer threads per connection than spawning two
    # workers, and the caller thread is already dedicated to this
    # connection anyway.
    b2c_thread = threading.Thread(
        target=_forward, args=(backend_sock, client_sock, b2c_done),
        name="tcp-proxy-b2c", daemon=True,
    )
    b2c_thread.start()

    _forward(client_sock, backend_sock, c2b_done)

    # Wait for the other direction to drain — either it EOFs naturally
    # because the peer FIN'd back at us, or the SHUT_WR from _forward
    # above will cause its recv() to return b"" on the next read.
    b2c_done.wait()

    for s in (client_sock, backend_sock):
        try:
            s.close()
        except OSError:
            pass
