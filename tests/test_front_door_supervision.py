"""
Tests for front_door child supervision + pointer state transitions.

Uses tests/_fake_child.py as a stand-in for the real server.py so
supervision behavior can be observed independent of Flask/yt_dlp import
weight. Injects a fake arbitration decision (host vs client) rather than
touching the real tailscale CLI.

front_door.py holds module-level runtime state (the pointer, the child,
the respawn deque, etc.) — these tests carefully reset that state
between cases via _reset_front_door_state() to keep them independent.
"""

import os
import socket
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import arbitration
import front_door
import host_state
import manager_config


def _reset_front_door_state():
    """Full reset of front_door module state between tests."""
    with front_door._supervisor_lock:
        # Kill any lingering child from a prior test.
        if front_door._child_process is not None:
            try:
                front_door._child_process.terminate()
                front_door._child_process.wait(timeout=3)
            except Exception:
                try:
                    front_door._child_process.kill()
                except Exception:
                    pass
        front_door._child_process = None
    front_door._child_generation = 0
    front_door._respawn_attempts.clear()
    # Drain the queue.
    while not front_door._control_queue.empty():
        try:
            front_door._control_queue.get_nowait()
        except Exception:
            break
    front_door._set_pointer(kind="unavailable", machine=None, child_pid=None,
                            starting=False, last_error=None)
    front_door._liveness_should_run.clear()
    front_door._shutdown.clear()


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_child_argv(mode="ok", port=None, delay=None, stop_delay=None):
    env_prefix = []
    if port is not None:
        env_prefix.extend(["FAKE_PORT", str(port)])
    if delay is not None:
        env_prefix.extend(["FAKE_DELAY", str(delay)])
    if stop_delay is not None:
        env_prefix.extend(["FAKE_STOP_DELAY", str(stop_delay)])

    # We use env vars, not argv — build a wrapper that sets them.
    return [sys.executable, os.path.join(os.path.dirname(__file__),
                                          "_fake_child.py")]


class SupervisionTests(unittest.TestCase):

    def setUp(self):
        _reset_front_door_state()
        # Point the front door at a per-test free port so we don't collide
        # with anything real, and don't need root.
        self._orig_child_port = front_door.LOCAL_CHILD_PORT
        self._child_port = _free_port()
        front_door.LOCAL_CHILD_PORT = self._child_port

        # Bypass the real Popen: patch _spawn_child_command to return an
        # invocation of _fake_child.py with the current test's env.
        self._orig_spawn_cmd = front_door._spawn_child_command
        self._child_mode = "ok"
        self._child_stop_delay = None
        test_self = self

        def _fake_cmd():
            return [sys.executable, os.path.join(
                os.path.dirname(__file__), "_fake_child.py")]
        front_door._spawn_child_command = _fake_cmd

        # Patch _spawn_local_child to inject env vars into Popen.
        # Easier than a shell wrapper.
        self._orig_spawn_local = front_door._spawn_local_child

        def _spawn_local():
            import subprocess as sp
            argv = front_door._spawn_child_command()
            env = os.environ.copy()
            env["FAKE_MODE"] = test_self._child_mode
            env["FAKE_PORT"] = str(test_self._child_port)
            if test_self._child_stop_delay is not None:
                env["FAKE_STOP_DELAY"] = str(test_self._child_stop_delay)
            try:
                child = sp.Popen(argv, env=env)
            except OSError as e:
                print(f"[fake-spawn] Popen failed: {e}")
                return None, None
            if not front_door._wait_for_child_port(
                child, test_self._child_port, timeout=8
            ):
                try:
                    child.terminate()
                    child.wait(timeout=2)
                except Exception:
                    pass
                return None, None
            front_door._child_generation += 1
            gen = front_door._child_generation

            def _waiter():
                try:
                    child.wait()
                except Exception:
                    pass
                front_door._control_queue.put(
                    (front_door._Intent.CHILD_EXITED, gen)
                )
            threading.Thread(target=_waiter, daemon=True).start()
            return child, gen

        front_door._spawn_local_child = _spawn_local

        # Neutralize the port-free check — it probes an OS port that
        # concurrent tests might collide on; we've already picked a free
        # one per-test.
        self._orig_port_free = front_door._port_5001_free
        front_door._port_5001_free = lambda: True

    def tearDown(self):
        front_door._spawn_child_command = self._orig_spawn_cmd
        front_door._spawn_local_child = self._orig_spawn_local
        front_door._port_5001_free = self._orig_port_free
        front_door.LOCAL_CHILD_PORT = self._orig_child_port
        _reset_front_door_state()

    # ------------------------------------------------------------------

    def test_spawn_and_running(self):
        child, gen = front_door._spawn_local_child()
        self.assertIsNotNone(child)
        try:
            # Child is up on the loopback port.
            with socket.create_connection(("127.0.0.1", self._child_port),
                                          timeout=2) as s:
                s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                buf = b""
                s.settimeout(2)
                while True:
                    try:
                        chunk = s.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                self.assertIn(b"fake-child-ok", buf)
        finally:
            front_door._stop_local_child(child, "test teardown")

    def test_child_that_never_binds_reports_failure(self):
        self._child_mode = "nobind"
        child, gen = front_door._spawn_local_child()
        self.assertIsNone(child)
        self.assertIsNone(gen)

    def test_graceful_stop(self):
        child, gen = front_door._spawn_local_child()
        self.assertIsNotNone(child)
        front_door._stop_local_child(child, "test graceful")
        # Give the OS a moment to reap.
        for _ in range(20):
            if child.poll() is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(child.poll())

    def test_graceful_stop_escalates_to_kill(self):
        # slow_stop mode stalls SIGTERM until FAKE_STOP_DELAY seconds pass.
        # Set it far past our graceful timeout so escalation is guaranteed.
        self._child_mode = "slow_stop"
        self._child_stop_delay = front_door.CHILD_STOP_GRACEFUL_SECONDS + 10
        child, _ = front_door._spawn_local_child()
        self.assertIsNotNone(child)
        t0 = time.time()
        front_door._stop_local_child(child, "test kill escalate")
        elapsed = time.time() - t0
        # Should have terminated within the graceful window + a small
        # margin for kill() + reap.
        self.assertLess(elapsed, front_door.CHILD_STOP_GRACEFUL_SECONDS + 5)
        self.assertIsNotNone(child.poll())

    def test_respawn_cap(self):
        """Simulate three crashes within window; fourth call should NOT
        get another spawn."""
        front_door._respawn_attempts.clear()
        # Push three attempts as if they just happened.
        now = time.time()
        for _ in range(front_door.RESPAWN_MAX_ATTEMPTS):
            front_door._respawn_attempts.append(now)
        self.assertFalse(front_door._within_respawn_cap())

        # After window elapses (simulated by clearing), it should be OK again.
        front_door._respawn_attempts.clear()
        self.assertTrue(front_door._within_respawn_cap())

    def test_stale_child_exited_event_ignored(self):
        """A CHILD_EXITED event tagged with an OLD generation must NOT
        trigger respawn or pointer changes when the current generation
        differs."""
        # Simulate a live current child at generation 5.
        front_door._child_generation = 5
        # Fake up a child_process object that has a pid attribute; a
        # real Popen isn't needed for this generation-guard test.

        class _FakeChild:
            pid = 12345
        front_door._child_process = _FakeChild()

        front_door._set_pointer(kind="local", machine="x", child_pid=12345,
                                starting=False, last_error=None)

        # Fire a stale event.
        with front_door._supervisor_lock:
            front_door._handle_child_exited(3)   # stale gen

        # Nothing should have changed.
        self.assertIsNotNone(front_door._child_process)
        self.assertEqual(front_door._read_pointer()["kind"], "local")


class PointerTransitionTests(unittest.TestCase):
    """Exercises _decide_client_pointer directly against a manipulated
    host-state.json in a tmp cwd, so the routing decisions can be verified
    in isolation from arbitration."""

    def setUp(self):
        _reset_front_door_state()
        front_door._my_machine_name = "alpha"
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        _reset_front_door_state()

    def test_no_state_file_yields_unavailable(self):
        with front_door._supervisor_lock:
            front_door._decide_client_pointer()
        self.assertEqual(front_door._read_pointer()["kind"], "unavailable")

    def test_stop_state_yields_unavailable(self):
        host_state.write_state("stop", "beta")
        with front_door._supervisor_lock:
            front_door._decide_client_pointer()
        self.assertEqual(front_door._read_pointer()["kind"], "unavailable")

    def test_start_state_naming_other_machine_yields_redirect(self):
        host_state.write_state("start", "beta")
        with front_door._supervisor_lock:
            front_door._decide_client_pointer()
        p = front_door._read_pointer()
        self.assertEqual(p["kind"], "redirect")
        self.assertEqual(p["machine"], "beta")

    def test_start_state_naming_me_but_not_hosting_yields_unavailable(self):
        """host-state names me but we're on the not-hosting branch: pointer
        should NOT loop back to my own machine — degrade to unavailable
        rather than a redirect-to-self."""
        host_state.write_state("start", "alpha")
        with front_door._supervisor_lock:
            front_door._decide_client_pointer()
        self.assertEqual(front_door._read_pointer()["kind"], "unavailable")


class RedirectResponseTests(unittest.TestCase):
    """Verifies the redirect-serving byte output — this replaces
    doorman.py's route, so we mirror its assertions: 302 with correct
    Location, hop counter incremented, 503 past MAX_HOPS, 503 on
    unavailable."""

    def test_redirect_body_shape(self):
        resp = front_door._redirect_response("beta", hop=0)
        self.assertIn(b"HTTP/1.1 302 Found", resp)
        self.assertIn(b"Location:", resp)
        # Hop is incremented.
        self.assertIn(b"?hop=1", resp)
        # Uses the same tailnet suffix scheme as doorman.py did.
        self.assertIn(b"beta.tail888cf2.ts.net:5000", resp)

    def test_hop_limit_response(self):
        resp = front_door._hop_limit_response()
        self.assertIn(b"HTTP/1.1 503", resp)
        self.assertIn(b"sync still catching up", resp)

    def test_unavailable_response(self):
        resp = front_door._unavailable_response()
        self.assertIn(b"HTTP/1.1 503", resp)
        self.assertIn(b"no host currently active", resp)


if __name__ == "__main__":
    unittest.main()
