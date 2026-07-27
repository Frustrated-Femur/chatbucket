"""
manager_main.py — Manager entrypoint.

Two modes:
    python3 manager/manager_main.py          -> opens the real pywebview window
    python3 manager/manager_main.py --cli    -> old stdout-only probe (fast,
                                                 no window, useful for quick
                                                 debugging of the underlying
                                                 status logic in isolation
                                                 from the pywebview bridge)

The --cli path is kept deliberately, not deleted: every bug found so far
(self/peer confusion, Funnel-node noise) was caught FASTER by reading
plain stdout than it would have been staring at a rendered UI. Keeping
it means the underlying logic stays checkable independent of whether
the js_api bridge itself is working.
"""
import argparse
import os
import sys

# manager/ sits one level below repo root, where arbitration.py,
# host_state.py, and main.py actually live. Must happen before the
# imports below.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MANAGER_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_INDEX = os.path.join(_MANAGER_DIR, "web", "index.html")

import arbitration
import host_state
# Reused, not reimplemented: main.py's own machine-identity logic.
# Importing main.py does NOT run its main() — guarded behind
# `if __name__ == "__main__"`, so this is a safe, side-effect-free import.
import main as cb_main


def get_host_state():
    """
    Returns {"ok": True, "state": <dict-or-None>} normally, or
    {"ok": False, "error": <str>} if host-state.json exists but is
    corrupted. Deliberately never lets HostStateError propagate up to
    the caller here — a corrupted file is real information the Manager
    should SURFACE, not crash on.
    """
    try:
        return {"ok": True, "state": host_state.read_state()}
    except host_state.HostStateError as e:
        return {"ok": False, "error": str(e)}


def get_claimed_host_status(claimed_machine, my_machine_name):
    """
    Checks online/offline for whichever machine host-state.json
    currently claims as host — a name read fresh from the file by the
    caller, never a fixed roster.

    Self-handling: `tailscale status --json` never lists the local
    machine under "Peer" — it appears under "Self" instead. Rather than
    reach into "Self"'s JSON shape for a claim that's tautologically
    true anyway (if this code is executing, this machine is up), self
    is reported as its own case.

    Returns one of:
      {"self": True}
      {"online": True/False}
      {"online": None, "error": <str>}
    """
    if claimed_machine == my_machine_name:
        return {"self": True}
    try:
        return {"online": arbitration.check_machine_online(claimed_machine)}
    except arbitration.ArbitrationError as e:
        return {"online": None, "error": str(e)}


def get_tailnet_peers():
    """
    Dynamic diagnostic: whatever Tailscale currently reports as real
    tailnet member devices. Tailscale's own infrastructure (Funnel
    ingress nodes etc.) is excluded by arbitration.list_tailnet_peers()
    itself via the structural DNSName filter.

    Returns {"ok": True, "peers": [...], "hidden_count": int} or
    {"ok": False, "error": <str>}.
    """
    try:
        result = arbitration.list_tailnet_peers()
        return {
            "ok": True,
            "peers": result["peers"],
            "hidden_count": result["hidden_count"],
        }
    except arbitration.ArbitrationError as e:
        return {"ok": False, "error": str(e)}


def _print_cli_report():
    """The original stdout probe — unchanged behavior, kept behind --cli."""
    my_name = cb_main.get_machine_name()
    print(f"Detected machine name: {my_name}")
    print()

    print("=== host-state.json ===")
    hs = get_host_state()
    claimed_machine = None
    if hs["ok"]:
        state = hs["state"]
        if state is None:
            print("  No claim on record (nobody has ever hosted).")
        else:
            print(f"  action:    {state['action']}")
            print(f"  machine:   {state['machine']}")
            print(f"  timestamp: {state['timestamp']}")
            claimed_machine = state["machine"]
    else:
        print(f"  CORRUPTED: {hs['error']}")

    print()
    print("=== claimed host status ===")
    if not hs["ok"]:
        print("  (host-state.json is corrupted — see above; nothing to check)")
    elif claimed_machine is None:
        print("  (no claim on record — nothing to check)")
    else:
        info = get_claimed_host_status(claimed_machine, my_name)
        if info.get("self"):
            print(f"  {claimed_machine}: (this machine)")
        elif info.get("error"):
            print(f"  {claimed_machine}: ERROR — {info['error']}")
        else:
            status = "online" if info["online"] else "offline"
            print(f"  {claimed_machine}: {status}")

    print()
    print("=== tailnet peers (dynamic, infra hidden) ===")
    tp = get_tailnet_peers()
    if not tp["ok"]:
        print(f"  ERROR — {tp['error']}")
    else:
        if not tp["peers"]:
            print("  (no peers found)")
        for peer in sorted(tp["peers"], key=lambda p: p["name"]):
            status = "online" if peer["online"] else "offline"
            print(f"  {peer['name']}: {status}")
        if tp["hidden_count"]:
            print(
                f"  (+{tp['hidden_count']} infrastructure peer(s) hidden "
                f"— no DNSName, likely Tailscale Funnel)"
            )


class ManagerApi:
    """
    Exposed to the frontend via pywebview's js_api. Every public method
    here is callable from JS as `pywebview.api.<method_name>(...)`,
    returning a Promise that resolves to whatever Python returns (must
    be JSON-serializable — plain dicts/lists/strings/bools/numbers).

    Deliberately thin: every method is a pass-through to the functions
    above, which are already proven correct via --cli. No new logic
    lives in this class — if the UI ever shows something wrong, the
    bug is either in one of the functions above (checkable via --cli,
    independent of pywebview) or in the HTML/JS rendering it, never
    hidden inside this bridge layer.
    """

    def __init__(self):
        self.my_name = cb_main.get_machine_name()

    def get_status(self):
        """
        Single aggregated call for the frontend's initial render and
        every refresh — one js_api round trip instead of three, since
        the UI always wants all three pieces together.
        """
        hs = get_host_state()
        claimed_machine = None
        if hs["ok"] and hs["state"] is not None:
            claimed_machine = hs["state"]["machine"]

        claimed_status = None
        if claimed_machine is not None:
            claimed_status = get_claimed_host_status(claimed_machine, self.my_name)

        tp = get_tailnet_peers()

        return {
            "my_name": self.my_name,
            "host_state": hs,
            "claimed_machine": claimed_machine,
            "claimed_status": claimed_status,
            "tailnet_peers": tp,
        }


def _run_gui():
    import webview
    api = ManagerApi()
    webview.create_window(
        "ChatBucket Manager",
        _WEB_INDEX,
        js_api=api,
        width=480,
        height=640,
        min_size=(380, 480),
    )
    webview.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cli", action="store_true",
        help="Print status to stdout instead of opening the window.",
    )
    args = parser.parse_args()

    if args.cli:
        _print_cli_report()
    else:
        _run_gui()
