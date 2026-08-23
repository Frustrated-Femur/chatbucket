"""
Tests for the auto_host toggle transitions — the exact scenarios called
out in the brief:

  - auto_host flipped ON while acting as client:
      * if a live host already exists, DO NOT steal it (stay redirect)
      * if nobody is live, claim (transition to Local)
  - auto_host flipped OFF while hosting:
      * gracefully release hosting
      * front door stays alive (port 5000 owner does NOT exit)
      * pointer transitions to Redirect or Unavailable via re-arbitration

Exercises _react_to_config_change() and _release_hosting() directly under
the supervisor lock, with the child-spawn path faked so tests are fast
and deterministic. Arbitration itself is injected via
should_i_be_host's checker parameters so the tests never touch a real
tailnet.
"""

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import arbitration
import front_door
import host_state
import manager_config


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeChildHandle:
    """Stand-in for a subprocess.Popen returned by _spawn_local_child.
    We don't need a real child running for these tests — we only need
    _release_hosting() to observe a truthy handle to terminate."""
    def __init__(self):
        self.pid = 12345
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        # Simulate a well-behaved child that exits immediately on SIGTERM.
        return 0

    def poll(self):
        return 0 if self.terminated or self.killed else None


class _AutoHostTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmp)

        # Reset front_door globals.
        front_door._shutdown.clear()
        front_door._liveness_should_run.clear()
        while not front_door._control_queue.empty():
            try:
                front_door._control_queue.get_nowait()
            except Exception:
                break
        front_door._my_machine_name = "alpha"
        front_door._child_process = None
        front_door._child_generation = 0
        front_door._respawn_attempts.clear()
        front_door._set_pointer(kind="unavailable", machine=None,
                                child_pid=None, starting=False,
                                last_error=None)

        # Tighten arbitration jitter so re-arbitrate paths finish fast.
        self._orig_jmin = arbitration.JITTER_MIN
        self._orig_jmax = arbitration.JITTER_MAX
        arbitration.JITTER_MIN = 0.0
        arbitration.JITTER_MAX = 0.01

        # Injected fakes for arbitration's non-injectable dependency.
        self._orig_list = arbitration.list_tailnet_peers

        # Patch _spawn_local_child so we don't actually launch anything.
        # Return a fake handle whose .terminate() we can observe.
        self._orig_spawn = front_door._spawn_local_child
        self._spawned_children = []

        def _fake_spawn():
            h = _FakeChildHandle()
            self._spawned_children.append(h)
            front_door._child_generation += 1
            return h, front_door._child_generation
        front_door._spawn_local_child = _fake_spawn

        # Also patch _stop_local_child to be a no-op body — the real one
        # calls terminate/wait which our fake handle honors, but bypassing
        # it entirely makes tests faster and lets us observe the call.
        self._orig_stop = front_door._stop_local_child
        self._stop_calls = []

        def _fake_stop(child, reason):
            self._stop_calls.append((child, reason))
            if child is not None:
                child.terminate()
        front_door._stop_local_child = _fake_stop

        # Force arbitration to accept our port-free predicate.
        self._orig_port_check = front_door._port_5001_free
        front_door._port_5001_free = lambda: True

        # Inject arbitration checkers module-wide via a wrapper on
        # should_i_be_host so _do_arbitrate_and_apply picks them up.
        # (arbitration.should_i_be_host takes injectable params, but
        # _do_arbitrate_and_apply calls the real one with only
        # port_free_checker set.)
        self._orig_should = arbitration.should_i_be_host
        self._tailscale_alive = {}    # peer -> bool
        self._health_alive = {}       # peer -> bool
        test_self = self

        def _wrapped_should(name, tailscale_checker=None,
                            health_checker=None, port_free_checker=None):
            return test_self._orig_should(
                name,
                tailscale_checker=lambda n:
                    test_self._tailscale_alive.get(n, False),
                health_checker=lambda n:
                    test_self._health_alive.get(n, False),
                port_free_checker=port_free_checker,
            )
        arbitration.should_i_be_host = _wrapped_should

    def tearDown(self):
        arbitration.JITTER_MIN = self._orig_jmin
        arbitration.JITTER_MAX = self._orig_jmax
        arbitration.list_tailnet_peers = self._orig_list
        arbitration.should_i_be_host = self._orig_should
        front_door._spawn_local_child = self._orig_spawn
        front_door._stop_local_child = self._orig_stop
        front_door._port_5001_free = self._orig_port_check
        front_door._child_process = None
        front_door._shutdown.set()
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


class AutoHostFlippedOnTests(_AutoHostTestBase):
    """auto_host was OFF, we were acting as client; user flips it ON."""

    def test_do_not_steal_healthy_host(self):
        """A different machine is genuinely alive — flipping auto_host
        on must NOT trigger a claim. Preserves the doc's "don't steal a
        healthy host" invariant."""
        # Seed: beta is host and alive.
        host_state.write_state("start", "beta")
        self._tailscale_alive["beta"] = True
        self._health_alive["beta"] = True
        # Simulate initial state: acting as client, pointer=redirect(beta).
        manager_config.update(auto_host=False, take_host_on_crash=False)
        front_door._set_pointer(kind="redirect", machine="beta",
                                child_pid=None, starting=False,
                                last_error=None)

        # Flip auto_host on.
        manager_config.update(auto_host=True)
        with front_door._supervisor_lock:
            front_door._react_to_config_change({"auto_host"})

        # Should have re-arbitrated but NOT claimed.
        p = front_door._read_pointer()
        self.assertEqual(p["kind"], "redirect")
        self.assertEqual(p["machine"], "beta")
        # host-state.json should still name beta.
        self.assertEqual(host_state.read_state()["machine"], "beta")
        # No child should have been spawned.
        self.assertEqual(self._spawned_children, [])

    def test_claim_when_nobody_is_live(self):
        """Nobody's live — flipping auto_host on triggers a claim."""
        # Seed: state file says stop, no peers alive.
        arbitration.list_tailnet_peers = lambda *a, **kw: {
            "peers": [], "hidden_count": 0,
        }
        manager_config.update(auto_host=False, take_host_on_crash=False)
        front_door._set_pointer(kind="unavailable", machine=None,
                                child_pid=None, starting=False,
                                last_error=None)

        # Flip auto_host on.
        manager_config.update(auto_host=True)
        with front_door._supervisor_lock:
            front_door._react_to_config_change({"auto_host"})

        # Should have claimed and spawned a child.
        p = front_door._read_pointer()
        self.assertEqual(p["kind"], "local")
        self.assertEqual(p["machine"], "alpha")
        self.assertEqual(len(self._spawned_children), 1)
        # host-state.json should now name alpha.
        s = host_state.read_state()
        self.assertEqual(s["action"], "start")
        self.assertEqual(s["machine"], "alpha")


class AutoHostFlippedOffTests(_AutoHostTestBase):
    """auto_host was ON and we were hosting; user flips it OFF."""

    def test_hosting_released_and_front_door_stays_alive(self):
        """Flipping auto_host off while hosting must:
          - write 'stop' to host-state.json
          - terminate the child
          - transition pointer to unavailable/redirect (via re-arbitrate)
          - NOT signal shutdown (front door must stay alive)"""
        # Seed: we're hosting.
        manager_config.update(auto_host=True, take_host_on_crash=False)
        host_state.write_state("start", "alpha")
        fake_child = _FakeChildHandle()
        front_door._child_process = fake_child
        front_door._set_pointer(kind="local", machine="alpha",
                                child_pid=fake_child.pid, starting=False,
                                last_error=None)

        # Nobody else is alive — re-arbitration after release should NOT
        # immediately re-claim (we passed force_claim_attempt=False by
        # going through _decide_client_pointer, not _do_arbitrate_and_apply).
        arbitration.list_tailnet_peers = lambda *a, **kw: {
            "peers": [], "hidden_count": 0,
        }

        # Flip auto_host off.
        manager_config.update(auto_host=False)
        with front_door._supervisor_lock:
            front_door._react_to_config_change({"auto_host"})

        # 1. host-state.json should now say "stop"
        s = host_state.read_state()
        self.assertEqual(s["action"], "stop")
        self.assertEqual(s["machine"], "alpha")

        # 2. child was terminated
        self.assertEqual(len(self._stop_calls), 1)
        self.assertIs(self._stop_calls[0][0], fake_child)
        self.assertTrue(fake_child.terminated)

        # 3. pointer is no longer local
        p = front_door._read_pointer()
        self.assertNotEqual(p["kind"], "local")
        # With no other live host, we go to unavailable — that's the
        # documented "release, don't immediately re-claim" behavior.
        self.assertEqual(p["kind"], "unavailable")

        # 4. front door is NOT shutting down
        self.assertFalse(front_door._shutdown.is_set())

    def test_release_transitions_to_redirect_if_someone_else_alive(self):
        """If another machine takes over during the release window (or
        was already alive but we were racing them), the post-release
        re-arbitration should point at them, not sit at unavailable."""
        manager_config.update(auto_host=True, take_host_on_crash=False)
        host_state.write_state("start", "alpha")
        fake_child = _FakeChildHandle()
        front_door._child_process = fake_child
        front_door._set_pointer(kind="local", machine="alpha",
                                child_pid=fake_child.pid, starting=False,
                                last_error=None)

        # Beta is up and alive — after we release, arbitration's healing
        # branch (via _decide_client_pointer reading a freshly-written
        # state file) needs to see them. _decide_client_pointer only
        # reads host-state.json; it doesn't run arbitration. So we
        # simulate a Syncthing-propagated write by rewriting the state
        # after our release path writes "stop".
        original_release = front_door._release_hosting

        def _wrapped_release():
            original_release()
            # Simulate beta claiming during our release window.
            host_state.write_state("start", "beta")
            front_door._decide_client_pointer()

        # Actually simpler: after the release path finishes writing "stop"
        # and calling _decide_client_pointer(), we can manually invoke
        # another _decide_client_pointer() after seeding beta's claim.
        manager_config.update(auto_host=False)
        with front_door._supervisor_lock:
            front_door._react_to_config_change({"auto_host"})
            # Beta comes up "moments later."
            host_state.write_state("start", "beta")
            front_door._decide_client_pointer()

        p = front_door._read_pointer()
        self.assertEqual(p["kind"], "redirect")
        self.assertEqual(p["machine"], "beta")
        self.assertFalse(front_door._shutdown.is_set())


class AutoHostToggleWithoutEffectTests(_AutoHostTestBase):
    """Flipping a toggle that shouldn't change lifecycle: only the
    liveness gate should refresh, nothing else."""

    def test_takeover_toggle_only_refreshes_gate(self):
        manager_config.update(auto_host=True, take_host_on_crash=False)
        front_door._set_pointer(kind="redirect", machine="beta",
                                child_pid=None, starting=False,
                                last_error=None)
        # Flip only take_host_on_crash.
        manager_config.update(take_host_on_crash=True)
        with front_door._supervisor_lock:
            front_door._react_to_config_change({"take_host_on_crash"})

        # No child spawn, no release, pointer unchanged.
        self.assertEqual(self._spawned_children, [])
        self.assertEqual(self._stop_calls, [])
        p = front_door._read_pointer()
        self.assertEqual(p["kind"], "redirect")
        self.assertEqual(p["machine"], "beta")
        # But the liveness event should now be set (auto_host + take on).
        self.assertTrue(front_door._liveness_should_run.is_set())


if __name__ == "__main__":
    unittest.main()
