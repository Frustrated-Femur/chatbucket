"""
_fake_child.py — Tiny stand-in for server.py used by tests.

Behavior modes controlled by env vars:
  FAKE_MODE=ok        : bind loopback port and stay up (default)
  FAKE_MODE=crash     : bind, then exit(1) after FAKE_DELAY seconds
  FAKE_MODE=nobind    : never bind — used to test the wait-for-bind timeout
  FAKE_MODE=slow_stop : bind, then on SIGTERM ignore for FAKE_STOP_DELAY sec
                        before finally exiting cleanly

FAKE_PORT: which loopback port to bind (default 5001)
FAKE_DELAY: seconds before crash (default 2)
FAKE_STOP_DELAY: seconds to stall SIGTERM (default 15 for slow_stop mode)

Deliberately no Flask — the front-door supervision path only cares that
SOMETHING is listening on the requested loopback port and eventually
exits. This decouples supervision tests from the real ChatBucket app.
"""

import os
import signal
import socket
import sys
import time


def _serve(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(8)
    return s


def _accept_and_reply(sock, reply_body):
    """Accept one connection, drain its request, reply with a tiny HTTP
    200. Used so test connections through the front-door proxy get a
    recognisable, deterministic body back."""
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            # Drain briefly — the client sent an HTTP request line + headers.
            conn.settimeout(0.5)
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    if b"\r\n\r\n" in chunk:
                        break
            except OSError:
                pass
            body = reply_body.encode()
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + body
            )
            try:
                conn.sendall(resp)
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def main():
    mode = os.environ.get("FAKE_MODE", "ok")
    port = int(os.environ.get("FAKE_PORT", "5001"))
    delay = float(os.environ.get("FAKE_DELAY", "2"))
    stop_delay = float(os.environ.get("FAKE_STOP_DELAY", "15"))
    reply_body = os.environ.get("FAKE_REPLY", "fake-child-ok")

    if mode == "nobind":
        # Simulate a child that never comes up.
        time.sleep(60)
        return

    if mode == "crash":
        sock = _serve(port)
        # Serve accepts in the background briefly, then die.
        import threading
        threading.Thread(target=_accept_and_reply,
                         args=(sock, reply_body), daemon=True).start()
        time.sleep(delay)
        sys.exit(1)

    if mode == "slow_stop":
        sock = _serve(port)
        stop_at = [None]

        def _handler(signum, frame):
            stop_at[0] = time.time() + stop_delay

        signal.signal(signal.SIGTERM, _handler)
        # Accept in background.
        import threading
        threading.Thread(target=_accept_and_reply,
                         args=(sock, reply_body), daemon=True).start()
        while True:
            if stop_at[0] is not None and time.time() >= stop_at[0]:
                return
            time.sleep(0.1)

    # Default 'ok' mode.
    sock = _serve(port)
    _accept_and_reply(sock, reply_body)


if __name__ == "__main__":
    main()
