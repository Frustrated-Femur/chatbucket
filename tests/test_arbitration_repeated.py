"""
Regression: arbitration.py must be safely callable REPEATEDLY from a
long-lived process (front_door), not just once from a short-lived one
(the old execv-based main.py).

Injectable-checker paths cover:
  * many consecutive calls with the same fakes -> deterministic result
  * port_free_checker override -> arbitration doesn't reject its own
    machine just because front_door owns :5000
  * host-state healing branch is idempotent under repeated calls
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import arbitration
import host_state


class RepeatedArbitrationTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmp)
        # Tighten jitter so tests don't drag.
        self._orig_jmin = arbitration.JITTER_MIN
        self._orig_jmax = arbitration.JITTER_MAX
        arbitration.JITTER_MIN = 0.0
        arbitration.JITTER_MAX = 0.01

    def tearDown(self):
        arbitration.JITTER_MIN = self._orig_jmin
        arbitration.JITTER_MAX = self._orig_jmax
        os.chdir(self._orig_cwd)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_repeated_call_no_state_claims_then_re_claims(self):
        """Empty state, injected sweep says nobody's alive: we claim
        on every call. No process-lifetime assumptions leak in and stop
        the 2nd/3rd invocation from working."""
        peers_result = {"peers": [], "hidden_count": 0}
        health_fake = lambda name: False
        tailscale_fake = lambda name: False

        # Patch list_tailnet_peers for _find_live_host.
        orig_list = arbitration.list_tailnet_peers
        arbitration.list_tailnet_peers = lambda *a, **kw: peers_result
        try:
            for _ in range(5):
                # Clear the state to simulate the "boot" branch each call.
                if os.path.exists("state/host-state.json"):
                    os.remove("state/host-state.json")
                result = arbitration.should_i_be_host(
                    "alpha",
                    tailscale_checker=tailscale_fake,
                    health_checker=health_fake,
                    port_free_checker=lambda: True,
                )
                self.assertTrue(result)
                # host-state.json should now claim alpha.
                s = host_state.read_state()
                self.assertEqual(s["action"], "start")
                self.assertEqual(s["machine"], "alpha")
        finally:
            arbitration.list_tailnet_peers = orig_list

    def test_port_free_checker_injection_bypasses_5000_default(self):
        """The front door owns :5000 permanently. Without the injected
        checker, arbitration's default _port_is_free(5000) call would
        always fail on the machine legitimately about to claim. Injecting
        a checker that reports True (as the front door does, for :5001)
        must let the claim through."""
        peers_result = {"peers": [], "hidden_count": 0}
        orig_list = arbitration.list_tailnet_peers
        arbitration.list_tailnet_peers = lambda *a, **kw: peers_result
        try:
            result = arbitration.should_i_be_host(
                "alpha",
                tailscale_checker=lambda n: False,
                health_checker=lambda n: False,
                # Simulate front door: checks 5001 (the child's port),
                # not the 5000 the front door owns.
                port_free_checker=lambda: True,
            )
            self.assertTrue(result)
        finally:
            arbitration.list_tailnet_peers = orig_list

    def test_port_free_checker_false_raises(self):
        """The port_free_checker is a caller-supplied predicate; if it
        reports the port is not free, arbitration must refuse to write
        a claim it can't back — this is the invariant the old TOCTOU
        check on :5000 protected, preserved under injection."""
        peers_result = {"peers": [], "hidden_count": 0}
        orig_list = arbitration.list_tailnet_peers
        arbitration.list_tailnet_peers = lambda *a, **kw: peers_result
        try:
            with self.assertRaises(arbitration.ArbitrationError):
                arbitration.should_i_be_host(
                    "alpha",
                    tailscale_checker=lambda n: False,
                    health_checker=lambda n: False,
                    port_free_checker=lambda: False,
                )
        finally:
            arbitration.list_tailnet_peers = orig_list

    def test_state_healing_on_deferred_branch(self):
        """When a live host is found via sweep despite an empty local
        state, arbitration should heal the local state (write 'start' with
        the discovered host) — repeatedly callable, idempotent."""
        peers_result = {
            "peers": [{"name": "beta", "online": True}], "hidden_count": 0,
        }
        orig_list = arbitration.list_tailnet_peers
        arbitration.list_tailnet_peers = lambda *a, **kw: peers_result
        try:
            for _ in range(3):
                if os.path.exists("state/host-state.json"):
                    os.remove("state/host-state.json")
                result = arbitration.should_i_be_host(
                    "alpha",
                    tailscale_checker=lambda n: True,
                    health_checker=lambda n: True,   # beta is alive
                    port_free_checker=lambda: True,
                )
                self.assertFalse(result)   # defer
                s = host_state.read_state()
                self.assertEqual(s["machine"], "beta")
                self.assertEqual(s["action"], "start")
        finally:
            arbitration.list_tailnet_peers = orig_list

    def test_defer_when_claimed_host_is_alive(self):
        """host-state names beta and beta is alive -> defer, do not
        rewrite state, on every call."""
        host_state.write_state("start", "beta")
        for _ in range(3):
            result = arbitration.should_i_be_host(
                "alpha",
                tailscale_checker=lambda n: True,
                health_checker=lambda n: True,
                port_free_checker=lambda: True,
            )
            self.assertFalse(result)
            s = host_state.read_state()
            self.assertEqual(s["machine"], "beta")

    def test_claim_when_claimed_host_is_dead(self):
        """host-state names beta but beta is unreachable -> alpha claims."""
        host_state.write_state("start", "beta")
        peers_result = {
            "peers": [{"name": "beta", "online": False}], "hidden_count": 0,
        }
        orig_list = arbitration.list_tailnet_peers
        arbitration.list_tailnet_peers = lambda *a, **kw: peers_result
        try:
            result = arbitration.should_i_be_host(
                "alpha",
                tailscale_checker=lambda n: False,   # beta offline
                health_checker=lambda n: False,
                port_free_checker=lambda: True,
            )
            self.assertTrue(result)
            s = host_state.read_state()
            self.assertEqual(s["machine"], "alpha")
        finally:
            arbitration.list_tailnet_peers = orig_list

    def test_no_module_level_state_between_calls(self):
        """Successive should_i_be_host calls must not accumulate state in
        the arbitration module — the front door will call this over and
        over, so any lingering per-call state would silently corrupt
        later decisions. Exercised by running the same scenario twice
        and asserting identical externally-visible behavior."""
        host_state.write_state("start", "beta")
        for _ in range(2):
            r = arbitration.should_i_be_host(
                "alpha",
                tailscale_checker=lambda n: True,
                health_checker=lambda n: True,
                port_free_checker=lambda: True,
            )
            self.assertFalse(r)
            self.assertEqual(host_state.read_state()["machine"], "beta")


if __name__ == "__main__":
    unittest.main()
