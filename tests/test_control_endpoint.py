"""
Tests against the actual loopback HTTP control/status endpoint.

Unlike the queue-level assertions in test_front_door_integration.py's
StatusEndpointTests (which spot-check that intents get posted), these
tests exercise the endpoint the way the Rust Manager will: real HTTP
requests to 127.0.0.1:<port>, real JSON bodies, real response shapes.

Also explicitly verifies the endpoint is NOT reachable on non-loopback
interfaces — a silent misbind here would expose control-plane commands
to anyone on the tailnet.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import front_door
import manager_config


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _local_non_loopback_addr():
    """Return this host's primary non-loopback IPv4 if one exists.
    Returns None on hosts with only loopback (some CI sandboxes)."""
    try:
        # Trick: connecting a UDP socket to a public IP causes the kernel
        # to pick the outbound-interface address without actually sending.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            addr = s.getsockname()[0]
            if addr and not addr.startswith("127."):
                return addr
    except OSError:
        pass
    return None


class _EndpointTestBase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmp)

        # Reset relevant front_door globals so /status reflects a known state.
        front_door._my_machine_name = "alpha"
        front_door._set_pointer(kind="unavailable", machine=None,
                                child_pid=None, starting=False,
                                last_error=None)
        while not front_door._control_queue.empty():
            try:
                front_door._control_queue.get_nowait()
            except Exception:
                break

        self._port = _free_port()
        self._server = front_door._LoopbackHTTPServer(
            ("127.0.0.1", self._port),
            front_door._StatusRequestHandler,
        )
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._server_thread.start()

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def _get(self, path):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self._port}{path}", timeout=2
        ) as r:
            return r.status, json.loads(r.read())

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class GetStatusShapeTests(_EndpointTestBase):

    def test_status_shape_when_unavailable(self):
        status, body = self._get("/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["routing"], "unavailable")
        self.assertEqual(body["machine"], "alpha")
        self.assertFalse(body["child_running"])
        self.assertIsNone(body["child_pid"])
        self.assertFalse(body["starting"])
        self.assertIsNone(body["last_error"])
        # Config keys present with sane defaults.
        self.assertIn("auto_host", body)
        self.assertIn("take_host_on_crash", body)
        self.assertIsInstance(body["auto_host"], bool)
        self.assertIsInstance(body["take_host_on_crash"], bool)

    def test_status_shape_when_local(self):
        front_door._set_pointer(kind="local", machine="alpha",
                                child_pid=4242, starting=False,
                                last_error=None)
        status, body = self._get("/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["routing"], "local")
        self.assertTrue(body["child_running"])
        self.assertEqual(body["child_pid"], 4242)

    def test_status_shape_when_redirect(self):
        front_door._set_pointer(kind="redirect", machine="beta",
                                child_pid=None, starting=False,
                                last_error=None)
        status, body = self._get("/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["routing"], "redirect:beta")
        self.assertFalse(body["child_running"])

    def test_status_reports_last_error_when_set(self):
        front_door._set_pointer(kind="unavailable", machine=None,
                                child_pid=None, starting=False,
                                last_error="tailscale CLI missing")
        status, body = self._get("/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["last_error"], "tailscale CLI missing")

    def test_status_reports_starting_flag(self):
        front_door._set_pointer(kind="unavailable", machine=None,
                                child_pid=None, starting=True,
                                last_error=None)
        status, body = self._get("/status")
        self.assertTrue(body["starting"])

    def test_unknown_get_returns_404(self):
        try:
            self._get("/no-such-path")
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


class PostControlShapeTests(_EndpointTestBase):

    def _drain_intents(self):
        seen = []
        while not front_door._control_queue.empty():
            try:
                seen.append(front_door._control_queue.get_nowait())
            except Exception:
                break
        return seen

    def test_start_action(self):
        status, body = self._post("/control", {"action": "start"})
        self.assertEqual(status, 202)
        self.assertTrue(body["accepted"])
        intents = self._drain_intents()
        self.assertTrue(any(i[0] == front_door._Intent.START_HOSTING
                            for i in intents))

    def test_stop_action(self):
        status, body = self._post("/control", {"action": "stop"})
        self.assertEqual(status, 202)
        self.assertTrue(body["accepted"])
        intents = self._drain_intents()
        self.assertTrue(any(i[0] == front_door._Intent.STOP_HOSTING
                            for i in intents))

    def test_rearbitrate_action(self):
        status, body = self._post("/control", {"action": "rearbitrate"})
        self.assertEqual(status, 202)
        intents = self._drain_intents()
        self.assertTrue(any(i[0] == front_door._Intent.REARBITRATE
                            for i in intents))

    def test_set_config_writes_file_and_posts_intent(self):
        status, body = self._post("/control", {
            "action": "set_config",
            "config": {"auto_host": True},
        })
        self.assertEqual(status, 202)
        # Response echoes the new full config.
        self.assertTrue(body["accepted"])
        self.assertTrue(body["config"]["auto_host"])
        # File actually updated.
        self.assertTrue(manager_config.read()["auto_host"])
        # Intent posted with the changed-keys set.
        intents = self._drain_intents()
        matching = [i for i in intents
                    if i[0] == front_door._Intent.CONFIG_CHANGED]
        self.assertEqual(len(matching), 1)
        self.assertIn("auto_host", matching[0][1])

    def test_set_config_idempotent_write_does_not_post_intent(self):
        """Writing the same value that's already there should not
        trigger a re-arbitration cycle — the changed-keys set is empty
        so no CONFIG_CHANGED intent is queued."""
        # Write once.
        self._post("/control", {"action": "set_config",
                                "config": {"auto_host": True}})
        self._drain_intents()
        # Write same value again.
        status, body = self._post("/control", {
            "action": "set_config", "config": {"auto_host": True},
        })
        self.assertEqual(status, 202)
        intents = self._drain_intents()
        self.assertFalse(any(i[0] == front_door._Intent.CONFIG_CHANGED
                             for i in intents))

    def test_set_config_rejects_invalid_combo(self):
        # take_host_on_crash=true with auto_host=false is disallowed.
        status, body = self._post("/control", {
            "action": "set_config",
            "config": {"take_host_on_crash": True, "auto_host": False},
        })
        self.assertEqual(status, 400)
        self.assertIn("auto_host", body["error"])

    def test_set_config_no_valid_keys(self):
        status, body = self._post("/control", {
            "action": "set_config", "config": {"wibble": True},
        })
        self.assertEqual(status, 400)

    def test_set_config_wrong_types_rejected(self):
        status, body = self._post("/control", {
            "action": "set_config", "config": {"auto_host": "yes"},
        })
        self.assertEqual(status, 400)

    def test_invalid_json_body(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/control",
            data=b"{not valid json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=2)
            self.fail("expected 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_unknown_action(self):
        status, body = self._post("/control", {"action": "explode"})
        self.assertEqual(status, 400)

    def test_unknown_post_path_returns_404(self):
        status, body = self._post("/wibble", {"action": "start"})
        self.assertEqual(status, 404)


class LoopbackOnlyBindTests(unittest.TestCase):
    """The single most security-critical property of the control endpoint:
    it must NEVER be reachable on any non-loopback interface, even
    accidentally. A silent misbind here would expose start/stop/config-
    write commands to the whole tailnet."""

    def test_endpoint_binds_only_loopback(self):
        port = _free_port()
        server = front_door._LoopbackHTTPServer(
            ("127.0.0.1", port),
            front_door._StatusRequestHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # Loopback works.
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/status", timeout=2
            ) as r:
                self.assertEqual(r.status, 200)

            # Attempt on the non-loopback address should FAIL to connect,
            # since the socket is bound only to 127.0.0.1.
            addr = _local_non_loopback_addr()
            if addr is None:
                self.skipTest("host has no non-loopback IPv4")

            with self.assertRaises((ConnectionRefusedError, TimeoutError,
                                    OSError)):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.settimeout(2)
                    sock.connect((addr, port))
                    # If it did connect (it shouldn't), issue a request
                    # to force the OS-level rejection to materialize.
                    sock.sendall(b"GET /status HTTP/1.1\r\nHost: x\r\n\r\n")
                    # And read to make the test fail loudly rather than
                    # quietly assuming success.
                    data = sock.recv(64)
                    if data:
                        self.fail(
                            f"control endpoint answered on non-loopback "
                            f"address {addr}:{port}! Got: {data!r}"
                        )
                finally:
                    sock.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
