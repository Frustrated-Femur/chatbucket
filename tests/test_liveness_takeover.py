"""
Liveness-loop tests — the client-side takeover trigger.

Design under test (front_door._liveness_loop):
  * Runs ONLY while pointer==Redirect AND take_host_on_crash==True
    AND auto_host==True (takeover-implies-permission-to-claim).
  * Uses a consecutive-failure counter for debounce; single misses do
    NOT trigger takeover.
  * On LIVENESS_FAILURES_TO_TAKEOVER consecutive failures, posts one
    TAKEOVER_ATTEMPT intent to the control queue. The control thread
    then calls the real arbitration function — no duplicate leader
    election.
  * Re-reads manager_config.read() every iteration so a toggle flip
    takes effect without a restart.

These tests exercise the loop by patching the health check + reading
the intent queue, so we can prove the debounce and gating behavior
without needing an actual remote host to kill.
"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import front_door
import manager_config


def _drain_intents():
    seen = []
    while not front_door._control_queue.empty():
        try:
            seen.append(front_door._control_queue.get_nowait())
        except Exception:
            break
    return seen


class _LivenessTestBase(unittest.TestCase):

    def setUp(self):
        # Isolate manager_config.json to a tmpdir so we don't clobber
        # a real one and tests don't affect each other.
        self._tmp = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmp)

        # Reset all front_door globals we touch.
        front_door._shutdown.clear()
        front_door._liveness_should_run.clear()
        while not front_door._control_queue.empty():
            try:
                front_door._control_queue.get_nowait()
            except Exception:
                break
        front_door._my_machine_name = "alpha"

        # Speed the loop up dramatically so tests don't take 20s per tick.
        self._orig_interval = front_door.LIVENESS_INTERVAL_SECONDS
        front_door.LIVENESS_INTERVAL_SECONDS = 1

        # Patch the health check so we control pass/fail per test.
        self._orig_health = front_door._health_check
        self._health_results = []   # list of bools, consumed in order;
                                    # last value is repeated when exhausted
        self._health_calls = []     # names checked, for assertions

        def _fake_health(name):
            self._health_calls.append(name)
            if not self._health_results:
                return True
            if len(self._health_results) == 1:
                return self._health_results[0]
            return self._health_results.pop(0)

        front_door._health_check = _fake_health

    def tearDown(self):
        front_door._shutdown.set()
        front_door._liveness_should_run.clear()
        front_door._health_check = self._orig_health
        front_door.LIVENESS_INTERVAL_SECONDS = self._orig_interval
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        # Give the liveness thread a moment to notice shutdown.
        time.sleep(0.2)

    def _start_liveness(self):
        # Kick the loop into life. _ensure_liveness_thread is idempotent
        # per its own lock so it's safe to call again if a prior test
        # already started it in this process.
        front_door._ensure_liveness_thread()

    def _wait_for_intent(self, intent, timeout):
        """Block up to `timeout` seconds waiting for a matching intent
        in the control queue. Returns True if seen."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for kind, _payload in list(front_door._control_queue.queue):
                if kind == intent:
                    return True
            time.sleep(0.05)
        return False


class LivenessGatingTests(_LivenessTestBase):
    """Confirms the loop does NOT run in states where it shouldn't."""

    def test_does_not_run_while_hosting(self):
        manager_config.update(auto_host=True, take_host_on_crash=True)
        front_door._set_pointer(kind="local", machine="alpha", child_pid=123,
                                starting=False, last_error=None)
        front_door._update_liveness_gate()
        self._start_liveness()

        # Even after several intervals, no TAKEOVER_ATTEMPT should fire.
        time.sleep(front_door.LIVENESS_INTERVAL_SECONDS * 3)
        intents = _drain_intents()
        self.assertFalse(any(k == front_door._Intent.TAKEOVER_ATTEMPT
                             for k, _ in intents))
        # And no health checks should have happened (loop was gated off).
        self.assertEqual(self._health_calls, [])

    def test_does_not_run_when_auto_host_off(self):
        manager_config.update(auto_host=False, take_host_on_crash=True)
        front_door._set_pointer(kind="redirect", machine="beta",
                                child_pid=None, starting=False,
                                last_error=None)
        front_door._update_liveness_gate()
        self._start_liveness()

        time.sleep(front_door.LIVENESS_INTERVAL_SECONDS * 3)
        intents = _drain_intents()
        self.assertFalse(any(k == front_door._Intent.TAKEOVER_ATTEMPT
                             for k, _ in intents))
        self.assertEqual(self._health_calls, [])

    def test_does_not_run_when_takeover_off(self):
        manager_config.update(auto_host=True, take_host_on_crash=False)
        front_door._set_pointer(kind="redirect", machine="beta",
                                child_pid=None, starting=False,
                                last_error=None)
        front_door._update_liveness_gate()
        self._start_liveness()

        time.sleep(front_door.LIVENESS_INTERVAL_SECONDS * 3)
        intents = _drain_intents()
        self.assertFalse(any(k == front_door._Intent.TAKEOVER_ATTEMPT
                             for k, _ in intents))
        self.assertEqual(self._health_calls, [])


class LivenessDebounceTests(_LivenessTestBase):
    """Confirms the consecutive-failure debounce actually debounces."""

    def test_single_failure_does_not_trigger_takeover(self):
        manager_config.update(auto_host=True, take_host_on_crash=True)
        front_door._set_pointer(kind="redirect", machine="beta",
                                child_pid=None, starting=False,
                                last_error=None)
        # One failure, then healthy forever.
        self._health_results = [False, True]
        front_door._update_liveness_gate()
        self._start_liveness()

        # Wait long enough for several health check cycles to run.
        time.sleep(front_door.LIVENESS_INTERVAL_SECONDS *
                   (front_door.LIVENESS_FAILURES_TO_TAKEOVER + 2))
        intents = _drain_intents()
        self.assertFalse(
            any(k == front_door._Intent.TAKEOVER_ATTEMPT
                for k, _ in intents),
            "single failure must not trigger takeover",
        )
        # We should have made SOME health calls though — this asserts
        # the loop did run, so the negative result above is meaningful.
        self.assertGreater(len(self._health_calls), 0)

    def test_three_consecutive_failures_trigger_takeover(self):
        manager_config.update(auto_host=True, take_host_on_crash=True)
        front_door._set_pointer(kind="redirect", machine="beta",
                                child_pid=None, starting=False,
                                last_error=None)
        # Always fail.
        self._health_results = [False]
        front_door._update_liveness_gate()
        self._start_liveness()

        # Cap: (N failures) * interval + safety margin.
        triggered = self._wait_for_intent(
            front_door._Intent.TAKEOVER_ATTEMPT,
            timeout=(front_door.LIVENESS_FAILURES_TO_TAKEOVER + 3) *
                     front_door.LIVENESS_INTERVAL_SECONDS,
        )
        self.assertTrue(
            triggered,
            f"expected TAKEOVER_ATTEMPT after "
            f"{front_door.LIVENESS_FAILURES_TO_TAKEOVER} failures",
        )
        # And it should have queried the RIGHT machine.
        self.assertTrue(all(name == "beta" for name in self._health_calls))
        self.assertGreaterEqual(
            len(self._health_calls),
            front_door.LIVENESS_FAILURES_TO_TAKEOVER,
        )


class LivenessConfigFlipTests(_LivenessTestBase):
    """Toggling take_host_on_crash at runtime takes effect within one
    interval without a restart."""

    def test_disabling_takeover_mid_loop_stops_health_checks(self):
        manager_config.update(auto_host=True, take_host_on_crash=True)
        front_door._set_pointer(kind="redirect", machine="beta",
                                child_pid=None, starting=False,
                                last_error=None)
        self._health_results = [True]
        front_door._update_liveness_gate()
        self._start_liveness()

        # Let it run a few ticks with takeover enabled.
        time.sleep(front_door.LIVENESS_INTERVAL_SECONDS * 2)
        calls_before = len(self._health_calls)
        self.assertGreater(calls_before, 0)

        # Flip the toggle off. This is the belt-and-braces path: the
        # per-iteration re-read of manager_config.read() must catch this
        # even without a CONFIG_CHANGED intent.
        manager_config.update(take_host_on_crash=False)

        # Wait a couple intervals; call count should stop growing.
        time.sleep(front_door.LIVENESS_INTERVAL_SECONDS * 3)
        calls_after = len(self._health_calls)

        # Allow one straggler check from the in-flight iteration at the
        # moment of the flip, but no more than that.
        self.assertLessEqual(
            calls_after - calls_before, 1,
            f"expected loop to stop after config flip; "
            f"before={calls_before}, after={calls_after}",
        )

    def test_enabling_takeover_mid_loop_starts_health_checks(self):
        manager_config.update(auto_host=True, take_host_on_crash=False)
        front_door._set_pointer(kind="redirect", machine="beta",
                                child_pid=None, starting=False,
                                last_error=None)
        self._health_results = [True]
        front_door._update_liveness_gate()
        self._start_liveness()

        # With takeover off, no health calls should happen.
        time.sleep(front_door.LIVENESS_INTERVAL_SECONDS * 2)
        self.assertEqual(len(self._health_calls), 0)

        # Flip it on. Under the front door's normal flow, this happens
        # via _react_to_config_change() -> _update_liveness_gate() which
        # sets the run event; simulate that path here.
        manager_config.update(take_host_on_crash=True)
        front_door._update_liveness_gate()

        # Now health checks should start firing.
        deadline = time.time() + front_door.LIVENESS_INTERVAL_SECONDS * 3
        while time.time() < deadline and len(self._health_calls) == 0:
            time.sleep(0.05)
        self.assertGreater(len(self._health_calls), 0)


if __name__ == "__main__":
    unittest.main()
