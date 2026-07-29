# ChatBucket — Networking & Host-Discovery Architecture

Status: **Design decided, not yet implemented.**
This document is the reference for all networking/hosting/relay decisions made for ChatBucket. Any chat in this project discussing networking, hosting, Tailscale, or the doorman/relay system should treat this as ground truth unless explicitly revised.

Participants: 3 machines total.
- **Arch** (archlinux.tail888cf2.ts.net) — manual start only, no autostart, no doorman.
- **Win1** (win1.tail888cf2.ts.net) — Windows 10/11, full autostart + arbitration + doorman.
- **Win2** (win2.tail888cf2.ts.net) — Windows 10/11, full autostart + arbitration + doorman.

All machines are on the same Tailscale tailnet (`tail888cf2.ts.net`). TLS is handled via Tailscale's built-in cert provisioning (`tailscale cert`), so HTTPS is available on every node's MagicDNS name without extra setup.

---

## 1. Problem being solved

ChatBucket has no fixed server. Any participant's machine can act as host, and hosting is transient — whoever is up "wins" host for that session. This creates two distinct problems that were initially conflated and had to be separated:

1. **Leader election** — deciding *who* is host right now, avoiding split-brain (two hosts at once).
2. **Discovery/addressing** — giving each person a single, permanent, bookmarkable URL that keeps working no matter who is currently host.

These are solved independently, by two separate mechanisms, described below.

---

## 2. Syncthing's actual role (correction — important)

Syncthing is used **only** to sync files/directories between participants (chat history persistence, the `host-state.json` pointer file described below, stickers/media assets, etc.).

**Syncthing is NOT the live chat transport.** ChatBucket is a live chat application with its own real-time connection mechanism (WebSocket or equivalent), separate and independent from Syncthing. Do not conflate the two in any design discussion — this was an earlier mistake that got corrected and should not resurface.

---

## 3. Leader election (who becomes host)

Mechanism: a single shared file, synced via Syncthing to all participants, e.g. `host-state.json`:

```json
{
  "action": "start",
  "machine": "win1",
  "timestamp": "2026-07-01T18:32:04Z"
}
```

`action` is `"start"` or `"stop"`. Whoever is hosting keeps this file current; on clean shutdown they write `"stop"`.

### Startup sequence (runs on Win1 and Win2 automatically; runs manually on Arch when the user types the start command)

```
1. Read host-state.json (local Syncthing-synced copy — no network call, it's already local).

2. If action == "stop" or file missing/empty:
     → No one is hosting → claim host (go to step 4).

3. If action == "start" and machine != me:
     a. Check `tailscale status --json` for that machine's Online state.
        - Offline → claim host (go to step 4).
        - Online  → attempt a real health-check HTTP connect to that
          machine's app port (NOT just Tailscale reachability — the
          machine can be up while the ChatBucket process itself is dead).
            - Health check succeeds → I am CLIENT. Do not touch the
              chat server. (Still start the doorman — see §4.)
            - Health check fails    → claim host (go to step 4).

4. Claim host:
     a. Random jitter delay (0.5–2s) to reduce boot-time race collisions
        (relevant mainly for Win1/Win2 booting near-simultaneously).
     b. Re-read host-state.json once more after the jitter.
     c. If it now shows someone else has claimed "start" in that window
        → back off, become CLIENT instead (go to step 3's client branch).
     d. Otherwise → write {"action":"start","machine":"<me>","timestamp":now}
                  → start the ChatBucket server.

5. Regardless of outcome (host or client) → start the doorman/relay
   listener. This step is UNCONDITIONAL — see §4 for why this matters.
```

This is deliberately not a "real" consensus algorithm (no Raft, no quorum). For 3 people, jitter + re-check + a liveness/health-check tiebreaker is proportionate. Full consensus protocols would be overengineering per ChatBucket's own stated philosophy.

### Arch's variant

Arch does not autostart or run background arbitration. Arch has a manual `chatbucket-start` script that performs the same check (steps 1–4) synchronously at the moment the user runs it, then execs the server. Arch does not run a standing doorman process — see §4 and §6 for the consequence of this.

---

## 4. The doorman / relay mechanism (discovery/addressing)

**Problem it solves:** a bookmark is a static URL. It can't run logic to find "whoever is currently host." Something listening *at* the bookmarked address needs to be smart enough to redirect the browser elsewhere if it isn't host.

**Mechanism:** every machine that participates in autostart (Win1, Win2) runs a tiny always-on listener — the "doorman" — on a fixed, known port. It is not a proxy and does not relay chat traffic; it issues a single HTTP 302 redirect based on the same `host-state.json` file.

```javascript
app.get('/', (req, res) => {
  const state = JSON.parse(fs.readFileSync('host-state.json'));
  if (state.machine === MY_OWN_NAME) {
    serveChatBucketApp(req, res);   // I'm host — serve directly, no redirect
  } else {
    res.redirect(302, `https://${state.machine}.tail888cf2.ts.net:${PORT}`);
  }
});
```

### Critical implementation constraint (do not regress on this)

**The doorman must start unconditionally, before/independent of the host-vs-client decision.** It must NOT be nested inside an `if (iAmHost)` branch, nor inside the `else`. It is not conditioned on the arbitration result at all.

Wrong (bug — breaks the whole scheme silently):
```javascript
if (iAmHost) {
    startChatBucketServer();
    startDoorman();   // WRONG — doorman becomes unreachable whenever this machine loses arbitration
}
```

Correct:
```javascript
startDoorman();        // always runs, first, no conditional around it

if (iAmHost) {
    startChatBucketServer();
}
```

If this is implemented wrong, the bug will not show up every session — it will only manifest on the specific day a given machine loses arbitration to another, at which point its bookmark silently dead-ends instead of redirecting. This is the single most important correctness constraint in the whole design and should be checked explicitly during implementation/review.

### Redirect loop protection

During Syncthing propagation windows, `host-state.json` copies can briefly disagree across machines, risking a redirect loop (A points to B, B's stale copy points to A). Mitigation: a `?hop=N` query param incremented on each redirect; if `hop > 2`, stop redirecting and show a "sync in progress, try again" message instead of looping the browser indefinitely.

### Accepted gap — not fixed, decided as acceptable

If `host-state.json` is stale (claims a host that has since crashed without writing `"stop"`), the doorman will redirect to a dead machine and the browser will simply fail to connect there — no further self-correcting bounce. Decision: **accepted as-is** for a 3-person group. The failure mode is "click the bookmark again in a few seconds once Syncthing/arbitration catches up," not catastrophic. Adding pre-redirect liveness verification at the doorman level was considered and explicitly rejected as unnecessary complexity for this group size.

---

## 5. Bookmarking scheme (final decision)

**Everyone bookmarks their own machine's URL, permanently. Never anyone else's.**

- Arch bookmarks `archlinux.tail888cf2.ts.net:PORT`
- Win1 bookmarks `win1.tail888cf2.ts.net:PORT`
- Win2 bookmarks `win2.tail888cf2.ts.net:PORT`

This works *because* of the doorman: a bookmark never needs to point at the actual current host — it only needs to point at something that knows how to find the host. Each person's own machine, running its own doorman, satisfies that as long as it's powered on and the doorman is running (see §4's unconditional-start constraint — this is exactly the property that would break if that bug were introduced).

Consequence: zero bookmark coordination needed between the three people, ever, regardless of who hosts on a given day. Nobody re-bookmarks anything when host changes.

---

## 6. Known asymmetry: Arch has no standing doorman

Because Arch is manual-start-only (per explicit user preference — user is comfortable with terminal, doesn't want autostart machinery), Arch does **not** run a persistent doorman process when it isn't actively running ChatBucket.

Consequence: if Arch is offline/idle and someone clicks Arch's bookmark, the connection simply dead-ends — no redirect, because nothing is listening on that machine to redirect from. This is an accepted tradeoff, not an oversight. Win1 and Win2's bookmarks are strictly more "always useful" than Arch's as a result. This is fine in practice since Arch's participation is already opt-in/manual by design.

---

## 7. Rejected alternatives (do not resurface without new justification)

- **Anchor/always-on reverse-proxy node**: rejected — no machine in this group is online 24/7, so a fixed always-up redirector doesn't exist to host it on.
- **Client-side "try multiple bookmarked URLs with fetch timeout" fallback list**: superseded by the doorman/relay approach — doorman is strictly simpler once autostart exists, since it needs no candidate-list logic client-side.
- **Ping-based leader election** (each machine pinging every other machine's URL at boot to decide who hosts): rejected — this is a naive leader-election race prone to split-brain (near-simultaneous boots both seeing the other as "down" and both self-electing). Replaced by the file + `tailscale status` + health-check approach in §3, which needs no direct inter-machine pinging at all.
- **Full Syncthing-status dashboard rebuild inside ChatBucket** (sync progress bars, per-message delivery ticks, device stats, transfer queues, conflict-resolution UI, auto-cleanup-on-full-sync): rejected as scope creep and a reliability regression — Syncthing already has a correct GUI (`127.0.0.1:8384`) for all of this; duplicating it risks two sources of truth disagreeing, and several items (especially auto-delete-on-full-sync) are genuinely dangerous distributed-consensus problems disproportionate to a 3-person group. If a lightweight in-chat presence indicator is wanted later, the minimal version is a single online/offline dot per user derived from `tailscale status`, nothing more granular.
- **Raft/quorum-based consensus for host election**: never seriously proposed, but explicitly noted as overkill should it come up — 3 participants don't need a real consensus protocol; jitter + re-check + liveness check is sufficient and proportionate.

---

## 8. Frontend / client platform decisions

- **PWA** (manifest.json + minimal service worker + HTTPS via Tailscale certs) is the agreed approach for making the chat UI feel like an installed app on PC, without Electron (rejected as bloat — ChatBucket's own philosophy prioritizes lightweight over convenience-via-bundled-runtime).
- PWA is a frontend/presentation concern only. It does **not** replace or absorb the doorman/arbitration backend logic — those remain separate always-running processes independent of whether the UI is opened as an installed PWA or a plain browser tab.
- **Android**: full hosting/doorman/arbitration role is explicitly out of scope — Android aggressively kills background processes not tied to a foreground service, and fighting that is disproportionate effort/fragility per ChatBucket's philosophy. Android's role is **client-only**: install the PWA, read the same Syncthing-synced `host-state.json` (via the official Syncthing Android app, which already handles Android background constraints correctly), connect directly to whichever machine is currently host. Android is never a redirect target and never expected to run a doorman.
- **Web Push notifications are a separate mechanism from the background-process point above** and are not ruled out by it. §8's Android exclusion is specifically about running arbitration/doorman/hosting logic as a background process, which Android kills. Browser Web Push (via FCM on Android Chrome) does not require the tab or app process to be running, foreground or background — it's delivered at the OS level once notification permission has been granted once. If phone-side engagement is ever revisited, this is the one lever that doesn't fight the Android constraint already documented here; it's gated purely on the person granting the browser notification permission, not on any of the hosting/arbitration limitations above.

---

## 9. Implementation status

**Built and tested** (host_state.py, arbitration.py, doorman.py, main.py, server.py's `/health` and shutdown handler): all proven against real subprocess/signal tests or Flask test clients on Arch, not just designed. See per-module docstrings for design rationale.

- `host_state.py` — atomic read/write via temp file + `os.replace` + `os.fsync`. Timestamps use microsecond precision, not seconds — a second-precision timestamp let two genuinely distinct writes of the same machine name within one wall-clock second collide into byte-identical dicts, which broke arbitration's jitter re-check (see next item). Fixed.
- `arbitration.py` — implements §3's full decision tree, including a fixed bug: the jitter re-check originally compared "is the current claim mine," which infinite-looped whenever a pre-existing stale claim from someone else just sat there unchanged. Fixed to compare against the exact state snapshot taken before jitter.
- `doorman.py` — implements §4's redirect logic, including the hop-count loop guard.
- `main.py` — wires arbitration to server.py/doorman.py selection. Machine name is passed as argv, never derived from OS hostname (Windows hostnames won't match `win1`/`win2` unless deliberately renamed).
- **Clean shutdown — corrected (2026-07-29):** this section previously claimed server.py registers its own SIGINT/SIGTERM handler that writes `{"action":"stop"}`. Checked directly against the actual file: no such handler exists anywhere in server.py, and never did — this was a stale claim in the doc, not a regression in the code. The practical effect matched what the claim would have missed anyway: every stop, graceful or forced, left `host-state.json` claiming this machine as host with nothing live behind it, which is why the Manager's STALE CLAIM state kept appearing after routine stops, not just crashes.

  Fixed at the **Manager** layer instead, in `manager_main.py`'s `stop()` (`_mark_stopped_if_mine()`), not inside server.py. Deliberate, not a shortcut: gunicorn's gevent worker already owns SIGTERM for its own graceful in-flight-request draining, and a second handler for the same signal in the same process risks shadowing or racing gunicorn's own handler rather than cooperating with it. The Manager is a separate process that verifies the target has actually exited before writing "stop" — safe regardless of graceful or forced shutdown, and the *only* mechanism that can correctly record a stop after a force-kill, since SIGKILL can't be caught by anything, anywhere, full stop. Same defensive check as originally described (only overwrites the claim if it still names this machine) now lives in that function instead.
- **Arch's guard script** (previously listed below as "sketched, not finalized") is superseded — `python3 main.py archlinux`, run manually, already performs the full arbitration-and-guard sequence. No separate bash script is needed.
- **`/health` endpoint** — implemented in server.py, dependency-free, sub-10ms.
- **Doorman port** — resolved: port 5000, same as the chat server. server.py and doorman.py never run simultaneously on one machine (mutually exclusive by arbitration outcome), so there's no collision to avoid.

**Resolved since last revision (2026-07-24):**

- **WAN exposure via `server.py`'s `0.0.0.0` bind** — Verified: router port-forwarding table checked, port 5000 is not forwarded from WAN. The `0.0.0.0` bind is therefore reachable only via LAN/tailnet under the current router configuration, consistent with this design's tailnet-only threat model. Caveat worth keeping in mind: this is a router-configuration fact, not a code-level guarantee — it needs re-checking if the router config ever changes (new device added, UPnP enabled, a port-forwarding rule edited). Binding `server.py` explicitly to `tailscale ip -4` instead of `0.0.0.0` would remove the dependency on router config entirely and remains a strictly stronger fix worth doing eventually as defense-in-depth, but it is no longer a blocking correctness gap.

- **`/upload` filename sanitization** — This doc's previous entry described a naive regex sanitizer that doesn't match the actual shipped `_safe_upload_name()`: the live code already normalizes backslashes to forward slashes and runs `os.path.basename()` *before* calling Werkzeug's `secure_filename()` on both the base and extension. Tested directly against real `secure_filename` with traversal payloads (`../../etc/passwd`, `..\..\windows\system32\evil.exe`) — both fully neutralized already, independent of the accidental timestamp-prefix protection this doc previously credited. That part of the original entry was inaccurate, not merely outdated.
  The genuine, empirically-confirmed gap was narrower: Windows-reserved device names (`CON`, `NUL`, `COM1`, etc.) passed through unchanged — `con.png` sanitized to `('con', '.png')`, which is a live problem specifically because Win1/Win2 are real hosts for this project. Fixed by adding an explicit case-insensitive reserved-name check (exact match on the sanitized base, not substring, so names like `confidential.png` are unaffected) plus a 100-character cap on the sanitized base to guard against pathological-length filenames causing filesystem errors. See `_safe_upload_name()` in `server.py`.

**Still genuinely outstanding, blocked on physical setup:**

- Windows autostart mechanism (Task Scheduler at logon vs. NSSM service) — leaning Task Scheduler for debuggability, not finalized. Cannot be built or tested until Win1 has Tailscale joined, Syncthing paired (`sync-state`/`sync-mess`/`sync-gifs`/`sync-sticker`/`sync-uploads` shared to it), and Python installed.
- Actual Windows startup script (`.bat`/PowerShell + Task Scheduler XML) — not yet written, blocked on the above.
