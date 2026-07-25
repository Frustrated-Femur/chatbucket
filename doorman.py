"""
doorman.py — Minimal redirect-only listener for non-host machines.

Runs ONLY when main.py has already decided this machine is CLIENT, never
concurrently with server.py on the same machine (they'd conflict on the
same port, and don't need to run together — whichever one is active
already fulfills this machine's "answer this bookmark correctly" duty).
See ChatBucket_Networking_Architecture.md §4.

This process knows NOTHING about messages, gifs, stickers, or uploads.
Its entire job: answer any request with a 302 redirect to whoever
host-state.json currently names as host. Deliberately dependency-light
and route-light — a client machine sitting idle as non-host has no
business running the full Flask app (message-store scanning, upload
handling, etc.) just to keep a bookmark alive.

Design choice worth being explicit about: the architecture doc's §4
pseudocode showed ONE unified route doing "if I'm host, serve; else,
redirect" — implying a single process could play both roles. This
implementation instead splits into two separate scripts (server.py vs.
doorman.py), selected by main.py at startup. Reasoning: server.py should
never need to carry redirect-branching logic mixed into its real routes,
and a client machine genuinely doesn't need server.py's full route table
resident in memory. If you'd rather have one unified script instead,
that's a legitimate alternative — this is the lighter-weight option.
"""

import os

from flask import Flask, request, redirect

import host_state

TAILNET_SUFFIX = "tail888cf2.ts.net"
SCHEME = "http"  # matches server.py's current lack of TLS — see arbitration.py's SCHEME note
PORT = 5000
MAX_HOPS = 2  # §4 redirect loop protection

app = Flask(__name__)


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def catch_all(path):
    """
    Redirect any request to whoever host-state.json currently claims as
    host. Path and query string beyond the hop counter are NOT preserved —
    this is bookmark recovery, not a general reverse proxy. Bookmarks point
    at "/", and that's the only case this realistically needs to handle.
    """
    try:
        hop = int(request.args.get("hop", "0"))
    except ValueError:
        hop = 0

    if hop > MAX_HOPS:
        # Redirect loop protection (§4): during a Syncthing propagation
        # window, host-state.json copies can briefly disagree across
        # machines (A's stale copy points to B, B's stale copy points back
        # to A). Rather than looping the browser indefinitely, stop and
        # say so plainly once the hop budget is exhausted.
        return (
            "ChatBucket: sync still catching up between machines. "
            "Try again in a few seconds.",
            503,
        )

    try:
        state = host_state.read_state()
    except host_state.HostStateError as e:
        # Corrupted host-state.json — do not guess a redirect target.
        # An honest error page beats silently sending someone to a
        # possibly-wrong machine based on garbage data.
        return (f"ChatBucket: host-state.json is corrupted — {e}", 500)

    if state is None or state.get("action") != "start":
        return ("ChatBucket: no host currently active.", 503)

    target_machine = state["machine"]
    target_url = f"{SCHEME}://{target_machine}.{TAILNET_SUFFIX}:{PORT}/?hop={hop + 1}"
    return redirect(target_url, code=302)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
