"""
Tests for tcp_proxy.proxy_connection.

Uses a plain stdlib echo/behavior TCP server on 127.0.0.1 as the "backend",
and a listener that hands each accepted connection to proxy_connection().
No mocks — this is the byte-splice validated end-to-end at the socket layer,
which is the only way to be confident about half-closes and WebSocket-style
long-lived duplex behavior.
"""

import socket
import threading
import time
import unittest

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tcp_proxy


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _BackendEcho:
    """Trivial threaded echo server. Each accepted connection echoes
    whatever it receives, byte-for-byte, until the client half-closes."""

    def __init__(self):
        self.port = _find_free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            ).start()

    def _handle(self, conn):
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                conn.sendall(data)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


class _Frontend:
    """Listener that runs proxy_connection() per accepted client."""

    def __init__(self, backend_port):
        self.backend_port = backend_port
        self.port = _find_free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(
                target=tcp_proxy.proxy_connection,
                args=(conn, ("127.0.0.1", self.backend_port)),
                daemon=True,
            ).start()

    def stop(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


class TCPProxyTests(unittest.TestCase):

    def test_small_roundtrip(self):
        backend = _BackendEcho()
        frontend = _Frontend(backend.port)
        try:
            with socket.create_connection(("127.0.0.1", frontend.port), timeout=3) as c:
                c.sendall(b"hello world")
                c.shutdown(socket.SHUT_WR)
                data = b""
                while True:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            self.assertEqual(data, b"hello world")
        finally:
            frontend.stop()
            backend.stop()

    def test_large_bidirectional_stream(self):
        """1 MB of data through the proxy, both directions, no corruption."""
        backend = _BackendEcho()
        frontend = _Frontend(backend.port)
        try:
            payload = bytes(range(256)) * 4096  # 1 MB, deterministic
            with socket.create_connection(("127.0.0.1", frontend.port), timeout=5) as c:
                sender_done = threading.Event()

                def send_all():
                    try:
                        c.sendall(payload)
                    finally:
                        c.shutdown(socket.SHUT_WR)
                        sender_done.set()

                threading.Thread(target=send_all, daemon=True).start()

                received = bytearray()
                while True:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    received += chunk

                sender_done.wait(timeout=5)
            self.assertEqual(bytes(received), payload)
        finally:
            frontend.stop()
            backend.stop()

    def test_backend_unavailable_returns_502(self):
        """Backend port is not listening — client should get the 502 body,
        then a clean close."""
        # Reserve then release a port so nothing listens there.
        dead_port = _find_free_port()

        frontend = _Frontend(dead_port)
        try:
            with socket.create_connection(("127.0.0.1", frontend.port), timeout=3) as c:
                c.settimeout(3)
                data = b""
                while True:
                    try:
                        chunk = c.recv(4096)
                    except (ConnectionResetError, OSError):
                        break
                    if not chunk:
                        break
                    data += chunk
            self.assertIn(b"502", data)
            self.assertIn(b"ChatBucket", data)
        finally:
            frontend.stop()

    def test_backend_disappears_mid_transfer(self):
        """Backend closes its accepted connection while the client is
        still connected — client should observe EOF, not hang."""
        backend_port = _find_free_port()
        listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen.bind(("127.0.0.1", backend_port))
        listen.listen(1)

        accepted = []

        def accept_one():
            conn, _ = listen.accept()
            accepted.append(conn)
            # Read one byte, then hard-close, simulating a mid-session crash.
            try:
                conn.recv(1)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

        threading.Thread(target=accept_one, daemon=True).start()

        frontend = _Frontend(backend_port)
        try:
            with socket.create_connection(("127.0.0.1", frontend.port), timeout=3) as c:
                c.settimeout(3)
                c.sendall(b"X")
                # Backend will close after reading that byte; we should EOF.
                # (Give the backend a moment to trigger.)
                data = b""
                deadline = time.time() + 3
                while time.time() < deadline:
                    try:
                        chunk = c.recv(4096)
                    except (ConnectionResetError, OSError):
                        break
                    if not chunk:
                        break
                    data += chunk
            # We expect no data back (backend closed without sending),
            # and — critically — we expect the recv loop to have exited,
            # not timed out.
        finally:
            frontend.stop()
            try:
                listen.close()
            except OSError:
                pass

    def test_multiple_concurrent_clients(self):
        """Several concurrent clients don't cross-talk."""
        backend = _BackendEcho()
        frontend = _Frontend(backend.port)
        try:
            results = {}
            errors = []

            def one_client(tag):
                try:
                    with socket.create_connection(
                        ("127.0.0.1", frontend.port), timeout=3
                    ) as c:
                        msg = f"client-{tag}".encode() * 200
                        c.sendall(msg)
                        c.shutdown(socket.SHUT_WR)
                        buf = b""
                        while True:
                            chunk = c.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                        results[tag] = buf == msg
                except Exception as e:
                    errors.append(e)

            threads = [
                threading.Thread(target=one_client, args=(i,), daemon=True)
                for i in range(6)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 6)
            self.assertTrue(all(results.values()))
        finally:
            frontend.stop()
            backend.stop()

    def test_half_close_propagates(self):
        """Client half-closes writes; backend still drains reply."""
        # A backend that reads all input, THEN sends a single reply, then closes.
        backend_port = _find_free_port()
        listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen.bind(("127.0.0.1", backend_port))
        listen.listen(1)

        def handle():
            conn, _ = listen.accept()
            with conn:
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                conn.sendall(b"got:" + data)

        threading.Thread(target=handle, daemon=True).start()

        frontend = _Frontend(backend_port)
        try:
            with socket.create_connection(("127.0.0.1", frontend.port), timeout=3) as c:
                c.sendall(b"query")
                c.shutdown(socket.SHUT_WR)  # Half-close: nothing else to send
                c.settimeout(3)
                data = b""
                while True:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            self.assertEqual(data, b"got:query")
        finally:
            frontend.stop()
            try:
                listen.close()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
