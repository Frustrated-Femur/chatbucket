"""
manager_main.py — Step 2 of the Manager build: read-only status probe.

No UI yet (see ChatBucket_Manager_Architecture.md's build order). This
proves the direct-import integration model (Manager doc §4) actually
works against this machine's real state/host-state.json and real
`tailscale status`, before any pywebview/js_api complexity is layered
on top.

Run from repo root, inside the activated venv:
    python3 manager/manager_main.py

Requires arbitration.py to have check_machine_online() and
list_tailnet_peers() (public wrappers around the module-private
_fetch_tailscale_status/_real_tailscale_checker) — see the patch
delivered alongside this file's first revision.

Revision note: this version replaces a hardcoded KNOWN_MACHINES roster
with two dynamic checks — see get_claimed_host_status() and
get_tailnet_peers() below for why a guessed-in-advance name list was
wrong, not just incomplete: it can't find a machine under a name
nobody guessed, and it silently hides that a claimed host has drifted
to a different name instead of surfacing the drift.
"""
import os
import sys

# manager/ sits one level below repo root, where arbitration.py,
# host_state.py, and main.py actually live. Must happen before the
# imports below.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import arbitration
import host_state
# Reused, not reimplemented: main.py's own machine-identity logic.
# Importing main.py does NOT run its main() — that's guarded behind
# `if __name__ == "__main__"`, so this is a safe, side-effect-free
# import of just its helper functions.
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
    caller, never a fixed roster. This is the only name the Manager
    actually needs an online/offline answer for: "is the current claim
    backed by a live machine" is a question about one specific name,
    not about however many machines might theoretically exist.

    Self-handling mirrors arbitration.check_machine_online's own
    reasoning: `tailscale status --json` never lists the local machine
    under "Peer" — it appears under "Self" instead, a different key
    with a different shape. Feeding the local machine's own name into
    a peer-list search isn't a tailscale problem, it's asking the wrong
    question — "is X in MY peer list" can never be true for X == me.
    Rather than reach into "Self"'s JSON shape for a claim that's
    tautologically true anyway — if this code is executing, this
    machine is up — self is reported as its own case.

    Returns one of:
      {"self": True}                    — the claim is this machine's own
      {"online": True/False}            — a real peer, checked via Tailscale
      {"online": None, "error": <str>}  — the Tailscale check itself failed
    """
    if claimed_machine == my_machine_name:
        return {"self": True}
    try:
        return {"online": arbitration.check_machine_online(claimed_machine)}
    except arbitration.ArbitrationError as e:
        return {"online": None, "error": str(e)}


def get_tailnet_peers():
    """
    Dynamic diagnostic: whatever Tailscale currently reports, not a
    fixed, guessed-in-advance list of names. A hardcoded roster doesn't
    just fail to find new machines — it silently hides drift on
    existing ones. If a machine gets reinstalled and Tailscale
    re-registers it under a fresh default hostname before anyone
    renames it back, a hardcoded check for the old name reports "not
    found" — indistinguishable from "doesn't exist yet" — while the
    machine is actually up under a name nobody's looking for. Dynamic
    enumeration surfaces the real current name instead.

    Returns {"ok": True, "peers": [...]} or {"ok": False, "error": <str>}.
    Each peer dict is {"name": <str>, "online": <bool>}, straight from
    arbitration.list_tailnet_peers() — no filtering or reordering here.
    """
    try:
        return {"ok": True, "peers": arbitration.list_tailnet_peers()}
    except arbitration.ArbitrationError as e:
        return {"ok": False, "error": str(e)}


def main():
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
    print("=== tailnet peers (dynamic) ===")
    tp = get_tailnet_peers()
    if not tp["ok"]:
        print(f"  ERROR — {tp['error']}")
    elif not tp["peers"]:
        print("  (no peers found)")
    else:
        for peer in sorted(tp["peers"], key=lambda p: p["name"]):
            status = "online" if peer["online"] else "offline"
            print(f"  {peer['name']}: {status}")


if __name__ == "__main__":
    main()
