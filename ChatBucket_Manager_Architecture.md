# ChatBucket Manager — Architecture & Decisions

Status: **Design decided, not yet implemented.**
This document is the reference for all decisions about the ChatBucket Manager app and its companion Windows installer script. Any chat discussing the Manager, its tech stack, or the bootstrap installer should treat this as ground truth unless explicitly revised. Companion document: `ChatBucket_Networking_Architecture.md` (host/doorman/arbitration — unaffected by anything here).

---

## 1. Problem being solved

Two distinct problems, previously conflated — kept separate on purpose:

- **A. Ongoing diagnostics/control** for a running (or should-be-running) ChatBucket instance. Primary targets: Win1/Win2. Also runs on Arch (dev/test machine — no Windows hardware available, see §12).
- **B. First-time environment setup** — Tailscale, Syncthing, Python, Task Scheduler registration — on a machine that has none of it yet.

Two separate deliverables solve these:
- The **Manager app** (this document) — solves A.
- A **PowerShell bootstrap script** — solves B.

---

## 2. Why not a webpage served by ChatBucket itself

Rejected. A status/control page served by `server.py` cannot report anything at the exact moment it's needed most — process crashed, never started, or lost arbitration. This is a category error (monitoring dependent on the thing being monitored), not a limitation to engineer around. The Manager must have a lifecycle fully independent of ChatBucket's own process.

---

## 3. Chosen architecture: pywebview

Native window, Python backend, HTML/CSS/JS front end rendered via the OS's system webview (WebKitGTK on Linux, WebView2 on Windows). Conceptually equivalent to an Android WebView-hosted app: real HTML/CSS/JS content, but not inside a browser tab, and not dependent on any server — local or remote — being reachable.

### Alternatives considered and rejected

- **Flask-served webpage** — rejected, see §2.
- **Tkinter (vanilla or ttkbootstrap)** — rejected on aesthetics. Dated by default; real effort needed to hit "somewhat aesthetically pleasing"; no reuse of ChatBucket's own design tokens.
- **PyQt/PySide6** — rejected. Large dependency (~60–80MB, bundles Qt). QSS is CSS-*like*, not CSS — nothing from `index.css`/`skill.md` transfers. Real native widgets, but at the cost of a second styling paradigm to learn and maintain.
- **Electron** — rejected. Same reasoning `ChatBucket_Networking_Architecture.md` §8 already used to reject Electron for the PWA. Reintroducing it here for the Manager would be an inconsistent double standard, and it's heavier than pywebview for equivalent output.
- **Compiled-language (Go/Rust) rewrite of Manager logic** — rejected. Would require reimplementing arbitration/tailscale-parsing logic in a second language — see §4.

### Dependency cost (stated honestly, not hidden)

- **Linux:** `webkit2gtk` system package (pacman), not merely pip-installable.
- **Windows:** Microsoft Edge WebView2 Runtime — bundled by default on any current Win10/11 install; bootstrap script checks for it and falls back to `winget install Microsoft.EdgeWebView2Runtime`.
- **Python:** `pywebview` package (pip) — Manager-only dependency, see §11.

---

## 4. Integration model — direct import, not subprocess, not reimplementation

The Manager's Python backend **imports `arbitration.py` and `host_state.py` directly** and calls their functions — the same pattern `main.py` itself already uses.

Explicitly rejected:
- **Subprocess + stdout parsing** (`python3 arbitration.py archlinux`, grep `HOST`/`CLIENT`) — fragile, couples the Manager to output formatting that could change.
- **A second, independent implementation** of "am I host" logic in another language — directly violates the module-boundary discipline already established in this codebase (`host_state.py` knows nothing about arbitration; `arbitration.py` knows nothing about file format). A Manager-side reimplementation would be a third, competing source of truth.

Consequence: **the Manager must be a Python process.** Treated as a constraint that simplifies the language decision, not a limitation worked around.

---

## 5. Status fields shown by the Manager

- **Host/client role** — read via the same `arbitration.py` logic `main.py` uses, never re-derived independently.
- **Tailscale peer status** — reuses `arbitration.py`'s existing `_real_tailscale_checker` (or an equivalent factored-out call), not a second `tailscale status --json` parser.
- **Syncthing folder sync state — minimal only.** One read-only REST call (`/rest/db/status?folder=sync-state` or equivalent) answering exactly one question: is the folder holding `host-state.json` actually in sync right now. No transfer queues, no per-file progress, no conflict-resolution UI, no device list with percentages — Syncthing already has a correct GUI at `127.0.0.1:8384`. This mirrors the rejection already on record in `ChatBucket_Networking_Architecture.md` §7 for the in-chat presence indicator; same reasoning applies here.
- **Process status** — running/stopped, PID, uptime.
- **Syncthing API key handling** — requires a one-time manual copy of the API key from Syncthing's `config.xml` (`~/.config/syncthing` on Linux, `%LOCALAPPDATA%\Syncthing` on Windows) into the Manager's own config. Real, non-zero setup friction — budgeted honestly, not treated as "just an API call."

**Known gap this depends on (not fixed here):** `ChatBucket_Networking_Architecture.md` §9 lists the Syncthing-synced folders as `sync-state`/`sync-mess`/`sync-gifs`/`sync-sticker`/`sync-uploads` — five folders. `sfx/` (local music) and `presence/` are not on that list. If `sfx/` isn't actually synced, a file uploaded while one machine is host silently doesn't exist when another machine later becomes host — and the Manager's Syncthing-status field would report "in sync" while missing that this folder was never configured to sync at all. This is a gap in the Networking doc, surfaced here because it affects what this doc's filesystem/status sections can promise; fix belongs in that document, not this one.

---

## 6. Process control

- **Start:** `subprocess.Popen` launching `main.py` — same as `chatbucket-start.sh`/Task Scheduler already do.
- **Stop:** must send a real, catchable termination signal, not a hard kill.

### Windows shutdown gotcha (must not be skipped)

`subprocess.Popen.terminate()` on Windows calls `TerminateProcess()` — a hard kill. The child gets zero chance to run its shutdown handler (the one that writes `{"action":"stop"}` to `host-state.json`, per `ChatBucket_Networking_Architecture.md` §9). A Stop button that hard-kills on Windows reintroduces the "stale claim, doorman redirects to a dead host" gap — as a *routine* occurrence on every Stop press, on the two machines the architecture depends on most for autostart reliability.

**Correct approach on Windows:** spawn with `CREATE_NEW_PROCESS_GROUP`, then send `CTRL_BREAK_EVENT` via `GenerateConsoleCtrlEvent` to request graceful shutdown.
**On POSIX (Arch):** plain SIGTERM via `.terminate()` is correct and already handled by `server.py`.

---

## 7. Open-browser button

Opens the Base URL (own machine's tailnet MagicDNS bookmark, per `ChatBucket_Networking_Architecture.md` §5) in the system default browser via `webbrowser.open()`. No special handling required.

---

## 8. Auto-restart-on-crash — deferred, not silently included

`ChatBucket_Networking_Architecture.md` §6 documents Arch as deliberately manual-start-only — autostart machinery there is an explicit non-goal, since Win1/Win2 already cover "always comes back up." An auto-restart-on-crash toggle is autostart-on-crash by another name.

**Decision:** not included in the first build. If wanted later, it must be an explicit, off-by-default opt-in, decided consciously rather than defaulted on because a checkbox was easy to add.

---

## 9. Platform scope

All three machines (Arch, Win1, Win2) may run the Manager. Arch is the primary dev/test machine for this project (no Windows hardware available for direct testing — mitigation in §12). This does not change Arch's role in the core hosting architecture — Arch remains manual-start-only per `ChatBucket_Networking_Architecture.md` §6; the Manager is an optional convenience layer on top, not a revision of that decision.

---

## 10. Filesystem layout & install location (Windows)

```
C:\Users\<name>\ChatBucket\
├── .venv\                   ← single shared virtualenv, see §11
├── VERSION                  ← plain-text version string, read by §14's update check
├── requirements.txt         ← ChatBucket server core deps
├── requirements-manager.txt ← Manager-only deps
├── requirements-prod-posix.txt  ← optional, POSIX-only (gunicorn/gevent), not installed on Windows
├── main.py
├── arbitration.py
├── host_state.py
├── doorman.py
├── server.py
├── presence_state.py
├── manager\
│   ├── manager_main.py
│   └── web\                 ← pywebview HTML/CSS/JS, shares tokens with static/index.css
├── messages\
├── uploads\
│   └── yt\
├── gifs\, stickers\, sfx\
├── state\                   ← Syncthing folder "sync-state"
├── presence\                ← Syncthing sync status currently undecided, see §5's gap note
├── static\
└── scripts\
    └── chatbucket-win-task.xml
```

**Install location: `%USERPROFILE%\ChatBucket`.** Compared against the alternatives:

| Option | Admin rights needed? | Findable by a non-technical friend | Syncthing-friendly | Verdict |
|---|---|---|---|---|
| `Program Files` | Yes — UAC on every write | Yes | No — live read/write app data fighting ACLs constantly | Rejected |
| `%LOCALAPPDATA%\ChatBucket` | No | Hidden by default | Works, but user has to type a hidden path into Syncthing's folder picker | More idiomatic, more friction |
| `%USERPROFILE%\ChatBucket` | No | Yes — sits next to Desktop/Documents | Yes — trivial to point Syncthing at | **Chosen** — least friction |
| `C:\ChatBucket` | Ambiguous, varies by account/UAC config | Yes | Yes | Rejected for unpredictability |

**Known gotcha:** usernames containing spaces (`C:\Users\John Smith\ChatBucket`) break unquoted command invocations. Every path reference — the Task Scheduler XML's `<Command>`/`<WorkingDirectory>`, the Manager's `subprocess.Popen` calls, Syncthing's folder config — must be quoted consistently. Treated as a known, bounded constraint to enforce everywhere paths are built, not a design flaw.

---

## 11. Python dependency management

**Decision: a single shared virtualenv at `ChatBucket\.venv`, populated by the bootstrap script via `pip install -r requirements.txt -r requirements-manager.txt`.**

Rejected: global `pip install` on the system interpreter. If a friend already has Python installed for something unrelated, a global install risks version conflicts with whatever that other thing needs, and PATH resolution could silently pick up the wrong `python.exe` depending on install order. A scoped venv avoids both failure modes and needs no elevation regardless of account type.

Rejected: separate venvs for ChatBucket server vs. Manager. Both run on the same machine from the same repo tree; one interpreter path is one less thing to keep consistent across the Task Scheduler XML and the Manager's own launch code. Dependencies are still split into separate requirement files (below) so it's clear what's actually needed by what, even though they land in the same venv.

**`requirements.txt`** (ChatBucket server core — installed everywhere):
```
flask
flask-sock
werkzeug
yt-dlp
```

**`requirements-manager.txt`** (Manager only):
```
pywebview
```
Note: the Manager imports `arbitration.py`/`host_state.py` directly (§4) but neither of those modules imports Flask or anything server-side — the Manager genuinely does not need `requirements.txt`'s contents to function, only its own.

**`requirements-prod-posix.txt`** (optional, POSIX only — not installed by the Windows bootstrap script at all):
```
gunicorn
gevent
```
This matches `main.py`'s existing `_exec_host_server()` logic exactly: Gunicorn is gated behind `os.name != "nt"` and a `shutil.which("gunicorn")` check, with graceful fallback to the Werkzeug dev server if either is false. Windows always takes the Werkzeug path, so installing Gunicorn/gevent there would be dead weight — correctly excluded, not merely allowed to be skipped.

**System (non-pip) dependency:** `ffmpeg`, used for sticker audio-stripping in `server.py`. Already gracefully degraded in code (`shutil.which("ffmpeg")` falls back to saving without stripping if absent) — so the bootstrap script treats it as a `winget install ffmpeg` nice-to-have, not a hard requirement that blocks install on failure.

**Required fix to `chatbucket-win-task.xml`:** the file's own inline comment currently instructs the installer to find Python via `` `where python` ``. That's now wrong under the venv decision — the `<Command>` element must point at `ChatBucket\.venv\Scripts\python.exe` specifically, not whatever `where python` happens to resolve to systemwide.

---

## 12. Installer — PowerShell bootstrap script, not a compiled installer

**Decision:** plain `.ps1` script using `winget`, not a compiled installer (NSIS/Inno Setup).

| Route | Verdict |
|---|---|
| Compiled installer (NSIS/Inno Setup) | Rejected — disproportionate engineering for 3 people; unsigned `.exe` silently registering scheduled tasks is a Defender/SmartScreen trigger; unverifiable without real Windows hardware or Wine (imperfect proxy — Task Scheduler semantics differ under Wine) |
| PowerShell bootstrap (`.ps1`, `winget`) | **Chosen** — reuses existing `chatbucket-win-task.xml` as-is; a friend can read the script in Notepad before running it; testable via GitHub Actions `windows-latest` runners without owning Windows hardware |
| Manual README, no script | Rejected as primary path — highest human-error risk, exactly the failure mode this effort exists to prevent; may still ship as a fallback reference |

### What the script does

- Checks for `winget` itself (`Get-Command winget`) before attempting anything; on absence, fails with a clear message and manual-download link rather than erroring silently. Not all Windows installs ship `winget` by default (older/minimal installs lack "App Installer" from the Store) — handled explicitly, not assumed away.
- Checks for and installs (via `winget`) Python, Tailscale, Syncthing, and the WebView2 Runtime if missing. `ffmpeg` installed best-effort (see §11).
- Downloads the latest stable release per §13, extracts into `%USERPROFILE%\ChatBucket` (see §10).
- Creates `.venv` and installs `requirements.txt` + `requirements-manager.txt` into it (see §11).
- Runs `schtasks /Create /XML` using `chatbucket-win-task.xml`, with the venv-python fix from §11 already applied in the shipped XML.

### Testing strategy (no Windows hardware available)

GitHub Actions `windows-latest` runners exercise the script's logic in CI — checkout repo, run the bootstrap script, assert on results (`schtasks /Query`, installed paths present, venv created, pip installs succeed). This validates logic correctness (bad flags, sequencing bugs, deprecated commands) but **not** visual/human-factor concerns (does a confused user know to right-click → Run with PowerShell, does the execution-policy prompt make sense) — those stay unverified until run on real hardware. Wine is explicitly rejected as a substitute test environment: Task Scheduler behavior under Wine does not reliably match real Windows.

---

## 13. ChatBucket source distribution

**Source of ChatBucket:** the GitHub repository — `https://github.com/Frustrated-Femur/chatbucket`. Treated as the single source of truth for all ChatBucket code.

**Initial installation:** the PowerShell bootstrap script downloads the latest stable release from GitHub and extracts it into place. Git is **not** required on user machines — the installer is responsible for obtaining ChatBucket; users never manually download source after the first install.

**Versioning:** each GitHub Release is an installable version. Development happens on `main`. Stable releases are published through GitHub Releases. Each release must include a `VERSION` file at repo root (plain text, e.g. `1.2.0`) — required by §14's update-comparison step; without it, "compare installed version with latest" has nothing concrete to compare against.

---

## 14. Updating ChatBucket

Performed by the Manager app via a **"Check for Updates" button — manual, user-triggered only.** Never a background poll: §8 already rejected silent auto-restart-on-crash on policy grounds (Arch's manual-start-only design), and an unattended background auto-update would be the same category of mistake in a different spot. Stated explicitly here so it isn't left as an implicit default someone quietly flips on later.

Workflow:

1. Query the latest GitHub Release.
2. Compare the repo's `VERSION` file against the release's `VERSION`.
3. If newer:
   - Stop ChatBucket gracefully (§6's signal discipline applies here too — no hard kill mid-update).
   - Back up the current installation.
   - Download the latest release ZIP.
   - **Replace only code, never data.** Extraction is scoped to an explicit allowlist — `*.py`, `manager/`, `static/`, `scripts/`, `requirements*.txt`, `VERSION` — and must never touch `messages/`, `uploads/`, `gifs/`, `stickers/`, `sfx/`, `state/`, or `presence/`. A naive "extract zip over the directory" approach risks clobbering chat history or `host-state.json` mid-arbitration on someone else's machine via Syncthing propagation. This is the highest-severity failure mode in this whole document — everything else here is inconvenience, this one is data loss.
   - **Restart by re-invoking `main.py`, never by directly relaunching whatever process was running before the update.** If the Manager instead resumes the prior role directly (e.g. jumps straight back into `server.py` because that's what was running pre-update), it bypasses arbitration entirely — a machine that was CLIENT before the update could force itself into HOST on restart, since it skipped the re-election step that would've told it another machine already holds the claim. Going back through `main.py` is what guarantees this doesn't happen.
4. If startup succeeds: remove the backup.
5. If startup fails: restore the previous version automatically from the backup.

Updating is never performed by the PowerShell bootstrap script — that script exists only for first-time installation (§12).

---

## 15. Design language

Manager UI (HTML/CSS/JS inside the pywebview window) reuses ChatBucket's own design tokens from `index.css` (`--surface-*`, `--border-*`, `--text-*`, etc.) and follows `skill.md`'s design-thinking process — a deliberate aesthetic direction, not generic default AI-app styling. Goal: the Manager should visually read as part of the ChatBucket project, not a bolted-on generic utility.

---

## 16. Rejected alternatives (do not resurface without new justification)

- **Flask-served admin webpage** — category error, dependent on the process it monitors (§2).
- **Tkinter (vanilla or ttkbootstrap)** — aesthetic ceiling too low for the stated requirement; no design-token reuse.
- **PyQt/PySide6** — large dependency, second styling paradigm (QSS), no reuse of existing web assets.
- **Electron** — already rejected once for the PWA decision (Networking Architecture §8); same lightweight-over-convenience reasoning applies here.
- **Manager reimplementing arbitration/tailscale-parsing logic independently** — violates existing module-boundary discipline; risks a second source of truth drifting from the real one.
- **Full Syncthing dashboard inside the Manager** — already rejected once in a different context (Networking Architecture §7); same "don't duplicate Syncthing's own correct GUI" reasoning applies here.
- **Compiled Windows installer (NSIS/Inno Setup)** — disproportionate engineering, unverifiable without real Windows hardware, higher trust/security friction than a readable script.
- **Auto-restart-on-crash as a default-on feature** — contradicts Arch's deliberate manual-start-only design decision; deferred, not rejected outright, but must be a conscious opt-in if ever built.
- **Global (system-wide) pip install instead of a venv** — risks version conflicts and PATH ambiguity on a friend's machine that already has Python for something else; rejected in favor of an isolated `.venv` (§11).
- **Background/automatic update polling** — same policy problem as auto-restart-on-crash; updates stay manual, button-triggered only (§14).

---

## 16.5 Bugs found and fixed post-build (2026-07-27)

**File layout changed: `manager_main.py` and `web/` now live at repo root**, not in a `manager/` subfolder as originally drafted in §10 — this is where the file actually got run from in practice, and it's now made a non-issue regardless (see next item), so the doc follows reality rather than fighting it.

**Bug 1 — repo-root path resolution assumed a fixed directory depth, and it broke.** The original code computed repo root as "two directories up from this file," assuming `manager/manager_main.py`. Once the file moved to repo root, that computation resolved to the repo's *parent* directory instead. Every path built from it was wrong: venv Python not found, `main.py` not found, and — the more serious cascade — `subprocess.Popen`'s `cwd` for launching `main.py` was also wrong, which propagated through `main.py`'s `os.execv()` into gunicorn, which then failed with `ModuleNotFoundError: No module named 'server'` because gunicorn's own cwd-based import resolution was pointed at the wrong directory.

Fixed by replacing the fixed-depth assumption with content-based discovery: walk upward from this file until a directory containing `arbitration.py`, `host_state.py`, and `main.py` together is found. Correct regardless of where this file sits, what the repo folder is named, or whether it moves again — including on Windows, where it also removes any need to hardcode the installer's target path (`%USERPROFILE%\ChatBucket` per §10): the search finds it by content, the same way, on both platforms.

**Bug 2 — the Role badge trusted `host-state.json`'s claim blindly, with no cross-check against reality.** Directly observed: arbitration wrote `{"action":"start","machine":"archlinux"}` to `host-state.json`, then `_exec_host_server()`'s gunicorn worker crashed immediately after (Bug 1's cwd issue). The Manager's Process card correctly showed "not running" — but the Role badge still said **HOST**, because it read the claim directly without checking whether a live process actually backed it. A claim can go stale the instant the process that wrote it dies.

Fixed with `_derive_role_state()` — a single Python-side function, not duplicated in JS, that cross-references the claim against `find_chatbucket_process()`'s live scan every time:
- `HOST` — claim is mine, **and** a real host-role process is verified running.
- `STALE CLAIM` — claim is mine, but nothing live backs it (exactly the bug above).
- `CLIENT` — claim is someone else's.
- `CONFLICT` — claim is someone else's, but *this* machine is also running as host locally (should never happen under correct arbitration; surfaced loudly if it ever does).
- `STARTING` — no claim yet, but a process is running (mid-arbitration window).
- `IDLE` — no claim, nothing running.
- `UNKNOWN` — `host-state.json` corrupted.

The frontend renders whatever this function returns; it does not re-derive role logic independently — same "one source of truth, not two implementations that can drift" discipline already applied earlier to the tailscale-peer logic.

## 16.6 Loading/progress indicator + a race-condition gap found while building it (2026-07-27)

Requested: visible confirmation that Start/Stop are doing something, not just log output. Building it correctly surfaced a real bug, not just a UI gap.

**The bug:** `find_chatbucket_process()` only recognized ChatBucket *after* `main.py`'s `os.execv()` replaced it with `server.py`/`gunicorn`/`doorman.py`. During arbitration itself (jitter + health-check, several real seconds) the process is still plain `python main.py <name>` — invisible to the detector. Two consequences: that's exactly the window a loading indicator needs to cover, and — more seriously — `start()`'s duplicate-launch guard couldn't see it either, so a second Start click (or a genuine race against Task Scheduler autostarting the same machine) during that window could launch a second `main.py`, reproducing the exact concurrent-arbitration race `chatbucket-start.sh`'s pidfile guard exists to prevent on Arch.

**Fix:** `find_chatbucket_process()` now recognizes three cmdline shapes across the *same* logical instance (PID is preserved by `os.execv()`, never changes) — `main.py` (role `"arbitrating"`), then `server.py`/gunicorn (role `"host"`), or `doorman.py` (role `"client"`). `_derive_role_state()` treats `"arbitrating"` as taking priority over whatever `host-state.json` currently claims, since that claim might be stale and about to be overwritten. `start()` now blocks until the launch resolves to a real role or `START_GRACE_SECONDS` (8s) elapses — mirroring `stop()`'s existing act-then-verify discipline instead of returning the instant `Popen()` succeeds, which reported "started" before there was anything real to show for it.

UI: buttons swap to a spinner + "Starting…"/"Stopping…" while their call is in flight (spinner style matches ChatBucket's own `.preview-spinner`, reused rather than reinvented). The Role badge pulses while in the `STARTING` state — this matters even when the Manager didn't initiate the start itself (e.g. Task Scheduler autostart, observed moments later on refresh): a static badge there would look identical to "stuck," which is the exact confusion this was built to remove.

## 17. Implementation status

**Built and verified on Arch (no Windows hardware exercised yet — see §12's testing gap, and the open question below):**

- `requirements.txt` / `requirements-manager.txt` / `requirements-prod-posix.txt` — created, installed into a `--system-site-packages` venv (see amendment below §11).
- `manager_smoke_test.py` — 7-point environment check (Python version, venv, repo-root imports, tailscale CLI, pywebview+GTK backend, pystray+Pillow, psutil). All passing.
- `arbitration.py` — extended with three public functions beyond the original arbitration decision tree: `check_machine_online()`, `list_tailnet_peers()`, and shared fetch helper `_fetch_tailscale_status()`. See amendment below §5.
- `manager/manager_main.py` — implements `get_host_state()`, `get_claimed_host_status()`, `get_tailnet_peers()`, plus a `ManagerApi` class exposing `get_status()` via pywebview's `js_api`. Dual entrypoint: `--cli` (stdout probe, no window) and default (opens the real window). The `--cli` path is kept permanently, not just during bring-up — every bug found in this module so far was caught faster reading plain stdout than it would have been through the rendered UI.
- `manager/web/index.html` — working frontend: role badge (HOST/CLIENT/IDLE), claimed-host reachability, dynamic tailnet peer list, manual refresh + 15s auto-refresh. Styled with ChatBucket's own `index.css` token values (dark surface stack, `--success`/offline status-dot convention) per §15.
- `window_check.py` — minimal literal pywebview window-open/close test, used to verify the GTK backend renders (not just imports) before the real UI was built on top of it.

**Not yet built:** Start/Stop process control, Windows graceful-shutdown handling (`CREATE_NEW_PROCESS_GROUP`/`CTRL_BREAK_EVENT`) — written nowhere yet, `psutil`-based already-running detection, tray icon (`pystray` installed, unused so far), Syncthing status integration, PowerShell bootstrap script, GitHub Actions test workflow, `VERSION` file.

**Open question surfaced during the build, not yet resolved:** a real Windows machine (not Win1/Win2, a different personal device) was discovered reachable on the tailnet mid-build. Whether it's available as an actual Windows test target — rather than relying solely on GitHub Actions `windows-latest` runners per §12 — is undecided and changes how much confidence the eventual Windows-specific code (shutdown handling above all) can have before Win1/Win2 themselves exist.

### Amendment to §5 — dynamic discovery, not a hardcoded roster

The original §5 draft assumed the Manager would check a fixed, known-in-advance list of participant machine names. Built and then corrected during implementation: this was wrong, not just incomplete. A hardcoded name list can't find a machine under a name nobody guessed, and — worse — it silently hides drift on an *existing* machine (e.g. a reinstalled Windows box re-registering under a fresh Tailscale hostname before being renamed back) by reporting "not found," indistinguishable from "never existed." The corrected design:

- **Claimed-host reachability** is checked against whatever name `host-state.json` currently contains, read fresh every time — never a pre-guessed list.
- **The tailnet peer list** is fully dynamic (`arbitration.list_tailnet_peers()`), with one structural filter: peers lacking a `DNSName` are excluded by default and counted, not silently dropped. Empirically confirmed against this project's real tailnet: a peer with no `DNSName` is not a device anyone added (MagicDNS names every real member device) — it's Tailscale-operated infrastructure (Funnel ingress nodes, confirmed via `HostName: "funnel-ingress-node"`). The filter is structural (absence of `DNSName`), not a hardcoded name to exclude, so it stays correct if Tailscale changes Funnel's naming or adds a different infra peer type.
- **Deferred, not rejected:** filtering the *real, named* peers down to "just ChatBucket participants" (e.g. hiding a user's personal phone/other PC that happen to share the tailnet) — a lightweight, user-editable config, not hardcoded in `.py`. Not built yet; judged not worth it with only one real participant (Arch) currently provisioned. Revisit once Win1/Win2 actually exist and the peer list has real participants to distinguish from noise.

### Amendment to §11 — venv isolation caveat (Arch-specific)

On Arch, the venv is created with `--system-site-packages` (not a plain isolated venv) because `python-gobject`/`webkit2gtk` are pacman-installed C-extension bindings that aren't reasonably pip-buildable in isolation. Practical consequence, confirmed empirically: this venv provides **no real dependency isolation on Arch** — every install resolved to `Requirement already satisfied` against pre-existing system/user site-packages, nothing was actually installed *into* `.venv` itself. This is accepted, not a bug: the original isolation rationale (avoiding conflicts with unrelated global Python tools) doesn't meaningfully apply to a single-purpose dev machine, and the venv is kept anyway for interpreter-path consistency with the fully-isolated Windows venv (`.venv\Scripts\python.exe`), not for isolation. This asymmetry is Linux-only — Windows's WebView2 backend never touches GTK/PyGObject, so its venv has no equivalent caveat.
