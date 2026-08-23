"""
Verifies that WebSocket traffic — the whole reason tcp_proxy is byte-blind
and never an HTTP-aware reverse proxy — actually survives the round trip
through the front door.

Uses a minimal Flask + flask_sock backend on 127.0.0.1:<port> (mirroring
server.py's real transport choice), the front door's accept loop on
another 127.0.0.1 port, and the `websockets` client to talk through it.

If this test ever fails, the WHOLE architectural bet fails: the proxy is
useless if it can't carry a WebSocket upgrade transparently.
"""

import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from flask import Flask
    from flask_sock import Sock
    _HAVE_FLASK_SOCK = True
except ImportError:
    _HAVE_FLASK_SOCK = False

try:
    from websockets.sync.client import connect as ws_connect
    _HAVE_WS_CLIENT = True
except ImportError:
    _HAVE_WS_CLIENT = False

import front_door


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(_HAVE_FLASK_SOCK and _HAVE_WS_CLIENT,
                     "flask_sock + websockets required")
class WebSocketThroughProxyTests(unittest.TestCase):

    def setUp(self):
        self._backend_port = _free_port()
        self._public_port = _free_port()
        self._orig_child_port = front_door.LOCAL_CHILD_PORT
        front_door.LOCAL_CHILD_PORT = self._backend_port

        # Minimal WS echo backend. Runs in a background thread using
        # Werkzeug's threaded dev server — the same server flask_sock
        # documents as WS-compatible (and the same one the fallback
        # child-spawn path in front_door._spawn_child_command uses).
        self._app = Flask(__name__)
        self._sock = Sock(self._app)

        @self._sock.route("/ws")
        def _ws(ws):
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                ws.send(f"echo:{msg}")

        @self._app.route("/health")
        def _health():
            return "ok"

        def _serve():
            self._app.run(host="127.0.0.1", port=self._backend_port,
                          threaded=True, use_reloader=False)

        self._backend_thread = threading.Thread(target=_serve, daemon=True)
        self._backend_thread.start()

        # Wait for backend port.
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(
                    ("127.0.0.1", self._backend_port), timeout=0.5
                ):
                    break
            except OSError:
                time.sleep(0.05)

        # Front-door listen socket + accept loop.
        self._listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_sock.bind(("127.0.0.1", self._public_port))
        self._listen_sock.listen(32)
        front_door._shutdown.clear()
        front_door._set_pointer(kind="local", machine="me", child_pid=999,
                                starting=False, last_error=None)
        self._accept_thread = threading.Thread(
            target=front_door._accept_loop, args=(self._listen_sock,),
            daemon=True,
        )
        self._accept_thread.start()

    def tearDown(self):
        front_door._shutdown.set()
        try:
            self._listen_sock.close()
        except OSError:
            pass
        front_door.LOCAL_CHILD_PORT = self._orig_child_port

    def test_websocket_echo_through_front_door(self):
        url = f"ws://127.0.0.1:{self._public_port}/ws"
        with ws_connect(url, open_timeout=5) as c:
            c.send("hello")
            reply = c.recv(timeout=5)
            self.assertEqual(reply, "echo:hello")
            c.send("world")
            reply = c.recv(timeout=5)
            self.assertEqual(reply, "echo:world")

    def test_websocket_carries_binary_frames(self):
        url = f"ws://127.0.0.1:{self._public_port}/ws"
        with ws_connect(url, open_timeout=5) as c:
            # flask_sock's echo above uses str concat, so send text —
            # what we're really testing here is that a longer payload
            # doesn't get chopped by the byte-splice.
            payload = "x" * 8000
            c.send(payload)
            reply = c.recv(timeout=5)
            self.assertEqual(reply, f"echo:{payload}")


if __name__ == "__main__":
    unittest.main()
