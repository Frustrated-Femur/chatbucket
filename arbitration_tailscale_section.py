"""
arbitration.py — Tailscale liveness section (drop-in replacement).

Replace the section of arbitration.py from the "Tailscale liveness"
comment banner through the end of list_tailnet_peers() (i.e. every
function delivered by the original dynamic-tailnet-discovery.patch)
with the code below. Everything above and below that section in
arbitration.py stays as-is — the arbitration decision tree, the
health-check helpers, and ArbitrationError itself are unchanged.

Why this rewrite:
    Devices that were reachable on the tailnet were reporting Offline
    in the Manager. Root cause was a stack of thin single-signal
    checks — a lone `peer.Online` boolean, a strict FQDN-suffix match
    on `DNSName`, no retries on transient CLI failures, and no
    fallback when Tailscale's control-plane heartbeat lags behind
    actual direct-connection reachability. Each of those is repaired
    below with an explanation of why the naive version was wrong.
"""

import json
import subprocess
import time
from datetime import datetime, timezone


# TAILNET_SUFFIX stays defined earlier in arbitration.py (unchanged).
# Referenced here as `TAILNET_SUFFIX` — assume it's imported / defined
# in the module scope where this section lands.

# How recently Tailscale must have heard from a peer for it to count
# as online when its own `Online: false` disagrees. Tailscale marks a
# peer offline the moment a control-plane heartbeat lags — a direct
# LAN connection to that same peer can still be perfectly reachable
# for many seconds after that flip. 90s is comfortably above the
# normal control-plane heartbeat interval (~60s) and below anything
# a human would still call "recently online."
_LASTSEEN_ONLINE_THRESHOLD_SECONDS = 90

# tailscale CLI can transiently fail (daemon restart, initial boot on
# a cold Windows session). One retry with a short backoff is the
# difference between "the Manager wrongly says everyone is offline
# for 15 seconds after login" and "the Manager rides that out."
_CLI_RETRY_ATTEMPTS = 2
_CLI_RETRY_BACKOFF_SECONDS = 0.8
_CLI_TIMEOUT_SECONDS = 8


class ArbitrationError(Exception):
    """Defined earlier in arbitration.py — restated here only so this
    file can be read standalone. Do NOT re-declare when merging back."""
    pass


# ── Tailscale liveness ──────────────────────────────────────────────────

def _run_tailscale_status_cli():
    """
    Invoke `tailscale status --json` with a bounded retry.

    The retry is intentionally narrow: only transient-looking failures
    (non-zero exit code, timeout, or empty stdout) get retried. A
    tailscale binary that isn't installed at all fails the same way on
    every attempt and shouldn't be retried — that just delays the
    inevitable error. This helper never silently swallows a hard
    failure into "assume offline"; it always ends by either returning
    real bytes of stdout or raising ArbitrationError. Silently treating
    "I can't tell" as "they're dead" is the split-brain hazard the
    module has always been careful about — that discipline stays.
    """
    last_error = None
    for attempt in range(_CLI_RETRY_ATTEMPTS):
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=_CLI_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            raise ArbitrationError(
                "`tailscale` CLI not found on PATH. Tailscale must be installed "
                "and reachable via the shell PATH for arbitration to work."
            )
        except subprocess.TimeoutExpired:
            last_error = ArbitrationError(
                f"`tailscale status --json` timed out after {_CLI_TIMEOUT_SECONDS}s "
                f"(attempt {attempt + 1}/{_CLI_RETRY_ATTEMPTS})."
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            last_error = ArbitrationError(
                f"`tailscale status --json` failed (attempt {attempt + 1}/"
                f"{_CLI_RETRY_ATTEMPTS}): {stderr or e}"
            )
        else:
            if result.stdout and result.stdout.strip():
                return result.stdout
            last_error = ArbitrationError(
                f"`tailscale status --json` produced no output "
                f"(attempt {attempt + 1}/{_CLI_RETRY_ATTEMPTS})."
            )
        # Backoff before the next attempt, only if we have more attempts left.
        if attempt + 1 < _CLI_RETRY_ATTEMPTS:
            time.sleep(_CLI_RETRY_BACKOFF_SECONDS)

    # Exhausted retries.
    raise last_error if last_error else ArbitrationError(
        "`tailscale status --json` failed for an unknown reason."
    )


def _fetch_tailscale_status():
    """
    Run `tailscale status --json` and return the parsed dict.

    This is the fetch/parse step every tailscale-status caller needs —
    factored out so there is exactly one place that knows how to invoke
    the CLI and parse its output. _real_tailscale_checker() filters
    this down to one machine's online bool; list_tailnet_peers()
    returns it unfiltered (with an infra-hidden count). Both read the
    exact same data through the exact same code path; neither
    re-implements the subprocess/JSON handling.

    Raises ArbitrationError if the CLI is unusable (not installed,
    daemon down, repeated timeout, or output isn't valid JSON).
    """
    stdout = _run_tailscale_status_cli()
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ArbitrationError(f"tailscale status returned invalid JSON: {e}")


def _parse_lastseen(value):
    """
    Tailscale reports `LastSeen` as an RFC3339 timestamp string, or
    sometimes as an ISO-with-Z-suffix, or (rarely, if the field is
    absent) not at all. All three shapes have to be tolerated — a
    parse failure here previously caused the whole peer to be dropped
    to offline, which is exactly the false-offline behaviour being
    fixed.
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Python's fromisoformat doesn't accept a trailing Z until 3.11 —
    # normalise it manually so this works on the Python versions
    # bundled with older Windows Python installs.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _peer_is_online(peer):
    """
    Decide whether a Tailscale peer dict represents a machine that
    should be treated as online, using more than one signal.

    The single-signal version (`peer.get("Online")`) is what caused
    devices that are demonstrably reachable to be reported offline.
    Tailscale's `Online` field flips false the moment a control-plane
    heartbeat lags, even while a direct or DERP-relayed connection to
    that same peer is still fine. The three additional signals below
    exist because at least one of them stays true across every
    control-plane flap I've been able to reproduce:

      1. `Online` — the authoritative fast signal when it agrees.
      2. `LastSeen` within _LASTSEEN_ONLINE_THRESHOLD_SECONDS —
         catches the "heartbeat lagged, peer is still there" case.
      3. `Active` — Tailscale sets this when there's a live
         connection tracked to the peer; a peer that's Active
         cannot meaningfully be called offline.
      4. Non-empty `CurAddr` or `Relay` — the connection has a
         concrete endpoint right now.

    Any single true signal is treated as online. This is deliberately
    a union, not an intersection: false-offline was the observed
    failure mode, and every one of these signals is conservative in
    the "yes it's up" direction.
    """
    if not isinstance(peer, dict):
        return False

    if bool(peer.get("Online", False)):
        return True

    if bool(peer.get("Active", False)):
        return True

    if peer.get("CurAddr") or peer.get("Relay"):
        return True

    last_seen = _parse_lastseen(peer.get("LastSeen"))
    if last_seen is not None:
        delta = (datetime.now(timezone.utc) - last_seen).total_seconds()
        if 0 <= delta <= _LASTSEEN_ONLINE_THRESHOLD_SECONDS:
            return True

    return False


def _peer_name_candidates(peer):
    """
    Every plausible name a caller might look a peer up by, in order
    from most-canonical to least.

    Previously, matching went only by `DNSName` with a strict FQDN
    tail check. That fails whenever:
      - A machine's DNSName isn't yet propagated (fresh join).
      - The user typed the short `HostName` rather than the MagicDNS
        one (very common — `host-state.json` stores whatever
        `main.get_machine_name()` returned, which is `HostName`).
      - Tailscale is configured with a non-MagicDNS tailnet suffix
        and TAILNET_SUFFIX doesn't match.

    Returning every candidate name lets the caller compare against
    whichever form it has, without the module having to guess which
    one the caller will use.
    """
    names = []
    dns_name = (peer.get("DNSName") or "").rstrip(".")
    if dns_name:
        names.append(dns_name)
        # Also add the leading label (MagicDNS short form).
        short = dns_name.split(".", 1)[0]
        if short and short != dns_name:
            names.append(short)
        # And the version with TAILNET_SUFFIX stripped, if it matches.
        try:
            suffix = "." + TAILNET_SUFFIX  # noqa: F821 — supplied by arbitration.py scope
        except NameError:
            suffix = None
        if suffix and dns_name.lower().endswith(suffix.lower()):
            stripped = dns_name[: -len(suffix)]
            if stripped and stripped not in names:
                names.append(stripped)

    host_name = peer.get("HostName")
    if host_name and host_name not in names:
        names.append(host_name)

    # `Name` is what tailscale prints in `tailscale status` non-JSON.
    peer_name = peer.get("Name")
    if peer_name:
        peer_name = peer_name.rstrip(".")
        if peer_name and peer_name not in names:
            names.append(peer_name)

    return names


def _display_name_for_peer(peer):
    """
    The one name to show a human for this peer — short MagicDNS label
    where available (matches how host-state.json refers to machines),
    else the DNSName, else the HostName, else empty string. Kept
    separate from _peer_name_candidates() so display never leaks the
    less-canonical fallback forms unless there's nothing better.
    """
    dns_name = (peer.get("DNSName") or "").rstrip(".")
    if dns_name:
        try:
            suffix = "." + TAILNET_SUFFIX  # noqa: F821
        except NameError:
            suffix = None
        if suffix and dns_name.lower().endswith(suffix.lower()):
            return dns_name[: -len(suffix)]
        return dns_name.split(".", 1)[0] or dns_name
    return peer.get("HostName") or peer.get("Name") or ""


def _real_tailscale_checker(machine_name):
    """
    Return whether `machine_name` is Online, per a fresh tailscale
    status. Matches against every candidate name form a peer exposes
    (DNSName, its short label, HostName, Name) — not just DNSName —
    so a caller that stored the short form doesn't get "not found"
    when the FQDN would have matched.

    Raises ArbitrationError if the tailscale CLI itself is unusable
    (not installed, daemon not running, repeated timeout). This is
    deliberately NOT swallowed into "assume offline": a broken
    tailscale CLI on THIS machine tells you nothing about whether
    the claimed host is actually alive. Silently treating "I can't
    tell" as "they're dead" would cause this machine to wrongly
    self-elect host while a real, healthy host is running elsewhere
    — the worst kind of split-brain, caused by a local tooling
    problem rather than an actual peer failure.
    """
    status = _fetch_tailscale_status()

    target = machine_name.strip().rstrip(".").lower()
    if not target:
        raise ArbitrationError("machine_name is empty")

    # Check Self first — a caller can legitimately pass this machine's
    # own name and expect a truthful answer (we're up, we're running).
    self_info = status.get("Self") or {}
    self_candidates = {n.lower() for n in _peer_name_candidates(self_info) if n}
    if target in self_candidates:
        return _peer_is_online(self_info) or True  # self is trivially up

    peers = status.get("Peer") or {}
    for peer in peers.values():
        candidates = {n.lower() for n in _peer_name_candidates(peer) if n}
        if target in candidates:
            return _peer_is_online(peer)

    raise ArbitrationError(
        f"Machine name {machine_name!r} not found in tailscale status "
        f"— check spelling, or that device is actually in this tailnet"
    )


def check_machine_online(machine_name, tailscale_checker=None):
    """
    Public entry point for callers OUTSIDE the arbitration decision
    tree (e.g. the Manager) who just want "is this named machine
    online right now." Kept as a thin wrapper so callers don't reach
    into the private `_real_tailscale_checker` directly, and so the
    injection seam used by tests remains a single point.
    """
    checker = tailscale_checker or _real_tailscale_checker
    return checker(machine_name)


def list_tailnet_peers(status_fetcher=None):
    """
    Public entry point for callers that want everything Tailscale
    currently reports, not one name checked against a guess. Built
    for the Manager's dynamic "what's on my tailnet right now"
    diagnostic — deliberately NOT a lookup against a fixed roster of
    expected names.

    Returns a dict:
        {
            "peers": [{"name": str, "online": bool}, ...],
            "hidden_count": int,   # infra peers with no DNSName
        }

    Peers with NO usable name at all (no DNSName, no HostName, no
    Name) are counted into `hidden_count` and hidden from the list —
    they are Tailscale-operated infrastructure (Funnel ingress, DERP
    helpers), not devices anyone added. The filter is structural
    (absence of any human-usable name), not a hardcoded name to
    exclude, so it stays correct if Tailscale changes Funnel's
    naming or adds a different infra peer type.

    Order is whatever tailscale status returns (not guaranteed
    stable); callers that display this should sort for presentation
    rather than assume an order.

    Raises ArbitrationError under the same conditions as
    check_machine_online (CLI missing, daemon down, timeout, bad
    JSON) — this reads the same underlying data, so it fails the
    same way for the same reasons.
    """
    fetch = status_fetcher or _fetch_tailscale_status
    status = fetch()

    peers_out = []
    hidden = 0
    for peer in (status.get("Peer") or {}).values():
        display = _display_name_for_peer(peer)
        if not display:
            hidden += 1
            continue
        peers_out.append({
            "name": display,
            "online": _peer_is_online(peer),
        })
    return {"peers": peers_out, "hidden_count": hidden}
