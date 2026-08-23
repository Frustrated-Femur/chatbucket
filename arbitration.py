"""
arbitration.py — Leader election decision logic (§3 of ChatBucket_Networking_Architecture.md).

Consumes host_state.py's read_state()/write_state(). Contains no file-format
knowledge of its own — that boundary is deliberate, see host_state.py's
module docstring for why.

IMPORTANT — what this module does NOT do:
This module decides host-or-client and writes that decision to host-state.json.
It does NOT start the doorman/relay listener, and it does NOT start the actual
ChatBucket server. The caller must ensure port 5000 is bound after arbitration:
by the full server when this function returns True, or by the redirect-only
doorman when it returns False. This module cannot enforce that startup
contract; it only answers "host or client?"

Configuration note — machine identity:
MACHINE_NAME must be supplied explicitly per deployment, never derived from
socket.gethostname(). On Arch this happens to match (hostname is literally
"archlinux"), but Windows machines almost never have a hostname matching the
tidy names used in host-state.json / Tailscale DNS ("win1", "win2") unless
someone deliberately renamed the PC. Auto-deriving identity here would
silently write the wrong machine name into host-state.json on Windows.

Protocol note — HTTP vs HTTPS:
Defaults to http://, matching the CURRENT server.py (plain Werkzeug dev
server, no ssl_context — see server.py's app.run() call). The architecture
doc's assumption of HTTPS via `tailscale cert` is not yet implemented in the
actual server. If/when that migration happens, change SCHEME below — but
until then, defaulting to https here would make every health check fail
silently and cause every machine to self-elect host on every boot.
"""

import json
import random
import socket
import subprocess
import time
import urllib.error
import urllib.request

import host_state

TAILNET_SUFFIX = "tail888cf2.ts.net"
APP_PORT = 5000
SCHEME = "http"  # see module docstring — change to "https" only once server.py actually terminates TLS

HEALTH_CHECK_TIMEOUT = 3       # seconds — per §3, this is the tiebreaker timeout
TAILSCALE_CLI_TIMEOUT = 5      # seconds — guards against a hung `tailscale` subprocess
JITTER_MIN = 0.5
JITTER_MAX = 2.0
MAX_CLAIM_RETRIES = 3          # guards against pathological back-and-forth re-claiming


class ArbitrationError(Exception):
    """Raised when arbitration cannot proceed safely (e.g. tailscale CLI unusable)."""
    pass


# ── Tailscale liveness ──────────────────────────────────────────────────

def _fetch_tailscale_status():
    """
    Run `tailscale status --json` and return the parsed dict.

    This is the fetch/parse step every tailscale-status caller needs —
    factored out so there is exactly one place that knows how to invoke
    the CLI and parse its output. _real_tailscale_checker() filters this
    down to one machine's Online bool; list_tailnet_peers() returns it
    unfiltered (minus Tailscale's own infrastructure peers, see that
    function's docstring). Both read the exact same data through the
    exact same code path; neither re-implements the subprocess/JSON
    handling.

    Raises ArbitrationError if the tailscale CLI itself is unusable (not
    installed, daemon not running, timeout, or output isn't valid JSON).
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True,
            timeout=TAILSCALE_CLI_TIMEOUT, check=True,
        )
    except FileNotFoundError:
        raise ArbitrationError("`tailscale` binary not found in PATH")
    except subprocess.TimeoutExpired:
        raise ArbitrationError("`tailscale status --json` timed out")
    except subprocess.CalledProcessError as e:
        raise ArbitrationError(f"`tailscale status --json` failed: {e.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ArbitrationError(f"tailscale status returned invalid JSON: {e}")


def _real_tailscale_checker(machine_name):
    """
    Return whether `machine_name` is Online, per a fresh tailscale status.

    Raises ArbitrationError if the tailscale CLI itself is unusable (not
    installed, daemon not running, timeout) — this is deliberately NOT
    swallowed into "assume offline", because a broken tailscale CLI on
    THIS machine tells you nothing about whether the claimed host is
    actually alive. Silently treating "I can't tell" as "they're dead"
    would cause this machine to wrongly self-elect host while a real,
    healthy host is running elsewhere — the worst kind of split-brain,
    caused by a local tooling problem rather than an actual peer failure.
    """
    status = _fetch_tailscale_status()

    target = machine_name.lower()
    for peer in status.get("Peer", {}).values():
        dns_name = peer.get("DNSName", "").rstrip(".").lower()
        if dns_name.startswith(target + "."):
            return bool(peer.get("Online", False))

    raise ArbitrationError(
        f"Machine '{machine_name}' not found in `tailscale status` peer list "
        f"— check spelling, or that device is actually in this tailnet"
    )


def check_machine_online(machine_name, tailscale_checker=None):
    """
    Public entry point for callers OUTSIDE the arbitration decision tree
    (e.g. the Manager's status display) that just want a raw online/
    offline answer for a machine, without invoking the full claim/defer/
    jitter arbitration logic. Internal arbitration code (_arbitrate)
    keeps calling _real_tailscale_checker directly via its own
    tailscale_checker parameter — this wrapper exists so external
    callers have a stable name to depend on instead of reaching into a
    module-private function.
    """
    checker = tailscale_checker or _real_tailscale_checker
    return checker(machine_name)


def list_tailnet_peers(status_fetcher=None, include_unnamed=False):
    """
    Public entry point for callers that want everything Tailscale
    currently reports as an actual tailnet MEMBER device — not one name
    checked against a guess, and not Tailscale's own operated
    infrastructure mixed in with real devices.

    Peers with no DNSName are excluded by default. Empirically confirmed
    (2026-07-26, against this project's real tailnet): a peer lacking
    DNSName is not a device anyone added to this tailnet — MagicDNS
    assigns every real member device a name — it's Tailscale-operated
    infrastructure surfacing as a peer for routing reasons (Tailscale
    Funnel's own ingress relay nodes report
    HostName="funnel-ingress-node", DNSName=""). Filtering on the
    STRUCTURAL absence of DNSName, rather than on that specific hostname
    string, means this stays correct if Tailscale ever changes Funnel's
    naming or adds a different kind of infra peer — there's no hardcoded
    name here to go stale.

    Hidden peers are never silently dropped without a trace: the
    returned "hidden_count" says how many were excluded, so a caller can
    report "+N infrastructure peers hidden" instead of the count just
    vanishing — same "surface it, don't swallow it" discipline this
    module already applies to tailscale-CLI failures elsewhere.

    Pass include_unnamed=True to see the raw, unfiltered list instead
    (falls back to HostName, whitespace-stripped, for a peer with no
    DNSName — "(unnamed)" if even that's missing). Useful for exactly
    the kind of diagnosis that led to this filter existing in the first
    place.

    Returns {"peers": [{"name": str, "online": bool}, ...],
             "hidden_count": int}.

    status_fetcher: injectable for testing (no-arg callable returning
    the parsed status dict) — separate from the tailscale_checker/
    health_checker injection used elsewhere in this module because the
    shape differs (no args in, a dict out, rather than a name in, a
    bool out). Defaults to the real _fetch_tailscale_status.

    Raises ArbitrationError under the same conditions as
    check_machine_online (CLI missing, daemon down, timeout, bad JSON)
    — this reads the same underlying data, so it fails the same way for
    the same reasons.
    """
    fetch = status_fetcher or _fetch_tailscale_status
    status = fetch()

    suffix = "." + TAILNET_SUFFIX
    peers = []
    hidden_count = 0
    for peer in status.get("Peer", {}).values():
        dns_name = peer.get("DNSName", "").rstrip(".")
        if not dns_name:
            if not include_unnamed:
                hidden_count += 1
                continue
            name = peer.get("HostName", "").strip() or "(unnamed)"
        else:
            if dns_name.lower().endswith(suffix.lower()):
                dns_name = dns_name[: -len(suffix)]
            name = dns_name
        peers.append({
            "name": name,
            "online": bool(peer.get("Online", False)),
        })
    return {"peers": peers, "hidden_count": hidden_count}


# ── Live tailnet sweep (bypasses host-state.json entirely) ──────────────

def _find_live_host(my_machine_name, health_checker, peers_fetcher=None):
    """
    Live sweep across every known tailnet peer, ignoring host-state.json
    entirely, to answer one question: is ANYONE actually serving
    ChatBucket right now, regardless of what the (possibly stale/
    unsynced) state file claims?

    This exists specifically for the two _arbitrate branches that used
    to skip verification altogether — "state is empty/stopped" and "I'm
    already the named host, resume." Both trust local host-state.json at
    face value, which is exactly what breaks during a Syncthing
    propagation lag: a machine reboots (crash or otherwise) faster than
    Syncthing delivers someone else's newer claim, sees its own stale
    local copy (empty, or still naming itself), and — without this sweep
    — would self-elect or resume host on top of a genuinely alive host
    elsewhere. This closes both branches at once, since both need the
    same answer: "is somebody already alive out there, whatever the file
    says."

    Returns the first responding peer's name, or None if nobody
    answered. Checks /health directly and skips a separate Tailscale-
    online step — list_tailnet_peers() already reports each peer's
    online state from the same `tailscale status` call, so re-checking
    it here would just be a second subprocess call for the same answer.

    Deliberately sequential, not parallel — for 2-3 participants this is
    simple and fast enough (worst case N * HEALTH_CHECK_TIMEOUT if
    everyone's genuinely down, paid once at claim time, not per doorman
    click).

    Raises ArbitrationError if the peer list itself is unavailable
    (surfaced by list_tailnet_peers/_fetch_tailscale_status) — same
    "don't guess" discipline as the rest of this module.
    """
    fetch_peers = peers_fetcher or list_tailnet_peers
    result = fetch_peers()

    for peer in result["peers"]:
        if peer["name"].lower() == my_machine_name.lower():
            continue
        if not peer["online"]:
            continue
        if health_checker(peer["name"]):
            return peer["name"]

    return None


# ── App-level health check ──────────────────────────────────────────────

def _real_health_checker(machine_name):
    """
    Attempt an actual HTTP GET to machine_name's /health endpoint.

    This is the tiebreaker Tailscale liveness alone can't provide: a
    machine can be fully reachable on the tailnet while the ChatBucket
    process itself has crashed. Returns False on ANY failure (connection
    refused, timeout, non-200, DNS failure) — deliberately not
    distinguishing failure modes here, because from the arbitration
    logic's point of view they all mean the same thing: "don't trust
    this claim, it's not backed by a live server."
    """
    url = f"{SCHEME}://{machine_name}.{TAILNET_SUFFIX}:{APP_PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_CHECK_TIMEOUT) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


# ── Core arbitration ─────────────────────────────────────────────────────

def should_i_be_host(my_machine_name, tailscale_checker=None, health_checker=None,
                     port_free_checker=None):
    """
    The arbitration decision from §3 of the architecture doc.

    Returns True  → this machine should (or does now) hold the host claim.
                     host-state.json has already been written by this call.
    Returns False → an existing host was verified alive; defer, do not
                     start the chat server. host-state.json is untouched.

    tailscale_checker / health_checker: injectable for testing. Each takes
    a machine name (str) and returns bool. Defaults to the real
    implementations above. Tests should inject fakes rather than mocking
    subprocess/urllib directly — keeps tests readable and decoupled from
    implementation details of the real checkers.

    port_free_checker: injectable no-arg callable returning bool. Front-door
    integration NEEDS this: under the front-door design, the caller (front
    door) already owns port 5000 for its entire lifetime, so a default
    _port_is_free(5000) check would ALWAYS fail on the machine legitimately
    about to claim — it's checking the wrong port. The front door passes a
    checker for :5001 (the child's port) instead. Legacy callers (the
    module's __main__ block, or the old execv-based main.py, both of which
    ARE about to bind :5000 themselves) get the historical behavior by
    leaving this None.

    Callers must bind the application port after this call — see module
    docstring.
    """
    tailscale_checker = tailscale_checker or _real_tailscale_checker
    health_checker = health_checker or _real_health_checker
    port_free_checker = port_free_checker or (lambda: _port_is_free(APP_PORT))

    return _arbitrate(my_machine_name, tailscale_checker, health_checker,
                      port_free_checker, MAX_CLAIM_RETRIES)


def _arbitrate(my_machine_name, tailscale_checker, health_checker, port_free_checker, retries_left):
    state = host_state.read_state()

    # §3 step 2: nobody hosting, or previous host cleanly stopped — per
    # the local file. Don't trust that at face value: it can be stale if
    # Syncthing hasn't yet delivered someone else's newer claim. Sweep
    # the tailnet for a live host before concluding "nobody's hosting."
    if state is None or state["action"] == "stop":
        live_host = _find_live_host(my_machine_name, health_checker)
        if live_host is not None:
            # Somebody's genuinely running — the local file just hasn't
            # caught up yet. Defer, and heal the local copy to match
            # reality so doorman (which trusts the file blindly, with no
            # network calls of its own) stops pointing at nothing.
            host_state.write_state("start", live_host)
            return False
        return _claim_host(my_machine_name, tailscale_checker, health_checker,
                           port_free_checker, retries_left, state)

    claimed_machine = state["machine"]

    # I was host last (e.g. I just rebooted) — before resuming, confirm
    # nobody else took over the claim while I was down. Same rationale
    # as above: a crash + fast reboot can race ahead of Syncthing, so
    # "the file still names me" isn't proof nobody else has since taken
    # the claim for real.
    if claimed_machine == my_machine_name:
        live_host = _find_live_host(my_machine_name, health_checker)
        if live_host is not None:
            host_state.write_state("start", live_host)
            return False
        return _claim_host(my_machine_name, tailscale_checker, health_checker,
                           port_free_checker, retries_left, state)

    # Someone else claims host — verify before deferring. §3 step 3.
    try:
        online = tailscale_checker(claimed_machine)
    except ArbitrationError:
        # Can't determine reachability at all. Conservative choice: do NOT
        # blindly trust the file, but also don't blindly claim host either —
        # surface this loudly, since it means something is wrong with THIS
        # machine's own tailscale setup, not necessarily that the peer is dead.
        raise

    if not online:
        return _claim_host(my_machine_name, tailscale_checker, health_checker,
                           port_free_checker, retries_left, state)

    if health_checker(claimed_machine):
        return False  # genuinely alive — defer

    # On the tailnet but app isn't answering — stale claim, crashed process.
    return _claim_host(my_machine_name, tailscale_checker, health_checker,
                       port_free_checker, retries_left, state)


def _port_is_free(port, host="0.0.0.0"):
    """
    Best-effort check: can we bind `port` on this machine right now?

    Checked immediately before writing a host claim (see _claim_host).
    Guards against the exact failure seen in testing: arbitration wrote
    {"action":"start","machine":"archlinux"} to host-state.json, then
    gunicorn tried to bind port 5000 and failed because a leftover
    doorman.py from an earlier manual run was still holding it. The
    claim was already written — and synced to other machines — before
    the bind failure ever surfaced, so every doorman on the tailnet
    (including this machine's own leftover one) started redirecting
    people to a machine that was never actually running a server,
    producing a self-redirect loop.

    This can't fully close the window — the port could still be taken
    by something else in the brief gap between this check and the real
    bind a moment later (TOCTOU race) — but it catches the common case:
    a leftover process already sitting on the port from an earlier
    manual run, exactly like that scenario. That's nearly all of the
    fixable window.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _claim_host(my_machine_name, tailscale_checker, health_checker,
                port_free_checker, retries_left, state_before_jitter):
    """
    §3 step 4: jitter, re-check, commit — or back off if the ground truth
    changed during our jitter window.

    Critical detail: the re-check compares against the EXACT state snapshot
    we already evaluated (state_before_jitter), not against "is the current
    claim mine". An earlier version of this function compared "is the
    current claim mine" and infinite-looped whenever the pre-existing stale
    claim from someone else was simply still sitting there, unchanged, after
    our sleep — that unchanged-but-not-mine state kept getting misread as
    "someone else claimed during my jitter", triggering a pointless
    re-evaluation of the exact same stale claim forever until retries ran
    out. Comparing full state equality fixes this: an unchanged file
    (whoever it names) means proceed with the claim; a CHANGED file means
    something genuinely happened during our sleep and deserves fresh
    evaluation.
    """
    if retries_left <= 0:
        raise ArbitrationError(
            f"Exceeded {MAX_CLAIM_RETRIES} claim/defer retries — two machines "
            f"appear to be repeatedly re-claiming against each other. This "
            f"should not happen for 2-3 participants under normal conditions; "
            f"treat as a bug, not a transient race, if it occurs."
        )

    time.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

    state_after_jitter = host_state.read_state()
    if state_after_jitter != state_before_jitter:
        # Ground truth changed while we slept — re-evaluate from scratch
        # against whatever it is now, rather than trusting a decision made
        # against stale information.
        return _arbitrate(my_machine_name, tailscale_checker, health_checker,
                          port_free_checker, retries_left - 1)

    if not port_free_checker():
        # Under the front door, this checker probes :5001 (the child's
        # port), NOT :5000 (permanently held by the front door). Same
        # spirit as before — refuse to write a claim we can't back —
        # just against the port that actually matters now.
        raise ArbitrationError(
            "The port needed to back a host claim is already in use on "
            "this machine — refusing to write a claim I can't actually "
            "back. Usually a leftover ChatBucket child process; kill it "
            "and retry."
        )

    host_state.write_state("start", my_machine_name)
    return True


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python3 arbitration.py <my-machine-name>")
        print("Example: python3 arbitration.py archlinux")
        sys.exit(1)

    my_name = sys.argv[1]
    try:
        result = should_i_be_host(my_name)
        print(f"HOST" if result else "CLIENT")
        sys.exit(0)
    except ArbitrationError as e:
        print(f"ARBITRATION ERROR: {e}")
        sys.exit(2)
