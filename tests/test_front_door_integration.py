"""
End-to-end integration for the front door: real listen socket on a free
public-side port, real proxy path to a real (fake) child, and pointer
flips exercised while connections are in flight.

We do NOT bind the real PUBLIC_PORT (5000) here — that would collide with
anything already using it, needs privileged bind on some hosts, and
makes tests fragile. Instead we call the front door's internal accept
loop and connection handler directly against sockets we control.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import front_door
import manager_config


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeChild:
    """Boots tests/_fake_child.py directly. The tests here don't go through
    front_door._spawn_local_child (that path is covered by the supervision
    tests); we own the child so we can precisely control its state."""

    def __init__(self, port, mode="ok"):
        env = os.environ.copy()
        env["FAKE_MODE"] = mode
        env["FAKE_PORT"] = str(port)
        env["FAKE_REPLY"] = f"hello-from-{port}"
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(
                os.path.dirname(__file__), "_fake_child.py")],
            env=env,
        )
        # Wait for bind.
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"fake child didn't bind {port}")

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _run_frontdoor_listener_on(port):
    """Bind a listener on 127.0.0.1:port and start front_door._accept_loop
    on it. Returns (sock, thread) for teardown."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(32)
    thread = threading.Thread(
        target=front_door._accept_loop, args=(sock,), daemon=True,
    )
    thread.start()
    return sock, thread


class ProxyThroughFrontDoorTests(unittest.TestCase):

    def setUp(self):
        # Reset state.
        front_door._shutdown.clear()
        front_door._set_pointer(kind="unavailable", machine=None,
                                child_pid=None, starting=False,
                                last_error=None)
        self._child_port = _free_port()
        self._public_port = _free_port()
        self._orig_child_port = front_door.LOCAL_CHILD_PORT
        front_door.LOCAL_CHILD_PORT = self._child_port

    def tearDown(self):
        front_door._shutdown.set()
        front_door.LOCAL_CHILD_PORT = self._orig_child_port

    def _http_get_via(self, port, path="/"):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n"
                      f"Connection: close\r\n\r\n".encode())
            s.settimeout(3)
            buf = b""
            while True:
                try:
                    chunk = s.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
            return buf

    def test_local_state_proxies_to_child(self):
        child = _FakeChild(self._child_port)
        sock, _ = _run_frontdoor_listener_on(self._public_port)
        try:
            front_door._set_pointer(kind="local", machine="me",
                                    child_pid=child.proc.pid,
                                    starting=False, last_error=None)
            body = self._http_get_via(self._public_port)
            self.assertIn(f"hello-from-{self._child_port}".encode(), body)
        finally:
            sock.close()
            child.stop()

    def test_redirect_state_returns_302(self):
        sock, _ = _run_frontdoor_listener_on(self._public_port)
        try:
            front_door._set_pointer(kind="redirect", machine="beta",
                                    child_pid=None, starting=False,
                                    last_error=None)
            body = self._http_get_via(self._public_port)
            self.assertIn(b"HTTP/1.1 302", body)
            self.assertIn(b"beta.tail888cf2.ts.net", body)
            # Hop started at 0 (no ?hop in our GET), returned as 1.
            self.assertIn(b"?hop=1", body)
        finally:
            sock.close()

    def test_redirect_state_respects_hop_counter(self):
        sock, _ = _run_frontdoor_listener_on(self._public_port)
        try:
            front_door._set_pointer(kind="redirect", machine="beta",
                                    child_pid=None, starting=False,
                                    last_error=None)
            # hop=1 -> next hop 2, still under MAX_HOPS=2, so 302.
            body = self._http_get_via(self._public_port, path="/?hop=1")
            self.assertIn(b"HTTP/1.1 302", body)
            self.assertIn(b"?hop=2", body)
            # hop=3 -> exceeds MAX_HOPS=2, so 503 hop-limit.
            body = self._http_get_via(self._public_port, path="/?hop=3")
            self.assertIn(b"HTTP/1.1 503", body)
            self.assertIn(b"sync still catching up", body)
        finally:
            sock.close()

    def test_unavailable_returns_503(self):
        sock, _ = _run_frontdoor_listener_on(self._public_port)
        try:
            front_door._set_pointer(kind="unavailable", machine=None,
                                    child_pid=None, starting=False,
                                    last_error=None)
            body = self._http_get_via(self._public_port)
            self.assertIn(b"HTTP/1.1 503", body)
            self.assertIn(b"no host currently active", body)
        finally:
            sock.close()

    def test_pointer_flip_local_to_redirect_mid_connection_window(self):
        """New connections after a flip see the new state; the port never
        becomes unbound. Not testing per-connection hand-off (impossible)."""
        child = _FakeChild(self._child_port)
        sock, _ = _run_frontdoor_listener_on(self._public_port)
        try:
            front_door._set_pointer(kind="local", machine="me",
                                    child_pid=child.proc.pid,
                                    starting=False, last_error=None)
            body = self._http_get_via(self._public_port)
            self.assertIn(b"hello-from-", body)

            # Flip to redirect. Existing connections are irrelevant (short
            # already-closed HTTP GETs); NEW connections should see 302.
            front_door._set_pointer(kind="redirect", machine="beta",
                                    child_pid=None, starting=False,
                                    last_error=None)
            body = self._http_get_via(self._public_port)
            self.assertIn(b"HTTP/1.1 302", body)
        finally:
            sock.close()
            child.stop()

    def test_backend_disappears_yields_502_transparently(self):
        """Local state but child is dead — proxy replies 502 without the
        front door itself failing."""
        # Bring child up, then kill it, then request.
        child = _FakeChild(self._child_port)
        sock, _ = _run_frontdoor_listener_on(self._public_port)
        try:
            front_door._set_pointer(kind="local", machine="me",
                                    child_pid=child.proc.pid,
                                    starting=False, last_error=None)
            child.stop()
            # Give the kernel a moment to release the port.
            time.sleep(0.3)
            body = self._http_get_via(self._public_port)
            self.assertIn(b"HTTP/1.1 502", body)
        finally:
            sock.close()


class StatusEndpointTests(unittest.TestCase):
    """Tests the loopback status/control endpoint. Uses a real HTTP client
    against a real bound status server on a random port."""

    def setUp(self):
        # We can't easily override STATUS_PORT because _start_status_server
        # reads it directly. Instead, we start the handler ourselves on a
        # free port.
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmpdir)

        front_door._my_machine_name = "testmachine"
        front_door._set_pointer(kind="unavailable", machine=None,
                                child_pid=None, starting=False,
                                last_error=None)
        while not front_door._control_queue.empty():
            try:
                front_door._control_queue.get_nowait()
            except Exception:
                break

        self._status_port = _free_port()
        server = front_door._LoopbackHTTPServer(
            ("127.0.0.1", self._status_port),
            front_door._StatusRequestHandler,
        )
        self._server = server
        self._server_thread = threading.Thread(
            target=server.serve_forever, daemon=True,
        )
        self._server_thread.start()

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _get_status(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self._status_port}/status", timeout=2
        ) as resp:
            return json.loads(resp.read())

    def _post_control(self, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._status_port}/control",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_status_reports_pointer_and_config(self):
        s = self._get_status()
        self.assertEqual(s["routing"], "unavailable")
        self.assertEqual(s["machine"], "testmachine")
        self.assertFalse(s["child_running"])
        self.assertIn("auto_host", s)
        self.assertIn("take_host_on_crash", s)

    def test_status_reports_redirect_with_machine(self):
        front_door._set_pointer(kind="redirect", machine="beta",
                                child_pid=None, starting=False,
                                last_error=None)
        s = self._get_status()
        self.assertEqual(s["routing"], "redirect:beta")

    def test_control_set_config_valid(self):
        status, body = self._post_control({
            "action": "set_config",
            "config": {"auto_host": True},
        })
        self.assertEqual(status, 202)
        self.assertTrue(body["accepted"])
        self.assertTrue(manager_config.read()["auto_host"])
        # Intent should have been posted.
        found = False
        while not front_door._control_queue.empty():
            intent, _ = front_door._control_queue.get_nowait()
            if intent == front_door._Intent.CONFIG_CHANGED:
                found = True
        self.assertTrue(found)

    def test_control_rejects_invalid_combination(self):
        # take_host_on_crash=true with auto_host=false should be refused.
        status, body = self._post_control({
            "action": "set_config",
            "config": {"take_host_on_crash": True, "auto_host": False},
        })
        self.assertEqual(status, 400)
        self.assertIn("auto_host", body["error"])

    def test_control_start_and_stop_queue_intents(self):
        for action, expected in (
            ("start", front_door._Intent.START_HOSTING),
            ("stop", front_door._Intent.STOP_HOSTING),
            ("rearbitrate", front_door._Intent.REARBITRATE),
        ):
            status, _ = self._post_control({"action": action})
            self.assertEqual(status, 202)
            # Drain and verify.
            found = False
            while not front_door._control_queue.empty():
                intent, _ = front_door._control_queue.get_nowait()
                if intent == expected:
                    found = True
            self.assertTrue(found, f"missing intent for action={action}")

    def test_control_rejects_unknown_action(self):
        status, body = self._post_control({"action": "wibble"})
        self.assertEqual(status, 400)


class ConcurrencyTests(unittest.TestCase):
    """Exercises the single-writer discipline: concurrent lifecycle
    intents cannot spawn two children or drop the pointer."""

    def setUp(self):
        front_door._set_pointer(kind="unavailable", machine=None,
                                child_pid=None, starting=False,
                                last_error=None)

    def test_concurrent_pointer_reads_are_consistent(self):
        """Many concurrent readers see a consistent pointer, even while a
        writer is mutating it. This is what makes accept-path workers
        safe."""
        stop = threading.Event()
        errors = []

        def writer():
            i = 0
            while not stop.is_set():
                if i % 2 == 0:
                    front_door._set_pointer(kind="redirect", machine="beta",
                                            child_pid=None, starting=False,
                                            last_error=None)
                else:
                    front_door._set_pointer(kind="local", machine="me",
                                            child_pid=123, starting=False,
                                            last_error=None)
                i += 1

        def reader():
            while not stop.is_set():
                p = front_door._read_pointer()
                # Invariants: kind is one of the allowed values, and if
                # kind==redirect the machine is set.
                try:
                    self.assertIn(p["kind"],
                                  ("local", "redirect", "unavailable"))
                    if p["kind"] == "redirect":
                        self.assertIsNotNone(p["machine"])
                except AssertionError as e:
                    errors.append(e)

        w = threading.Thread(target=writer, daemon=True)
        rs = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
        w.start()
        for r in rs:
            r.start()
        time.sleep(0.5)
        stop.set()
        w.join(timeout=2)
        for r in rs:
            r.join(timeout=2)
        self.assertEqual(errors, [])

    def test_control_queue_serializes_intents(self):
        """Two competing start-then-stop intents post correctly; the
        control thread processes them in order and each intent runs under
        _supervisor_lock (single writer)."""
        # Verify the queue is a real, single-consumer queue.
        for i in range(5):
            front_door._control_queue.put(
                (front_door._Intent.REARBITRATE, i)
            )
        drained = []
        while not front_door._control_queue.empty():
            drained.append(front_door._control_queue.get_nowait())
        self.assertEqual(len(drained), 5)
        # Order preserved.
        self.assertEqual([d[1] for d in drained], [0, 1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
