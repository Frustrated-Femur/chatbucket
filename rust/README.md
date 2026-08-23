# chatbucket-manager (Rust)

Native Rust GUI Manager for ChatBucket. Migrated from the pywebview-based
`manager_main.py`.

## Improvements over the Python version

| Concern | Python (pywebview) | Rust (this) |
| --- | --- | --- |
| Linux runtime dep | `webkit2gtk` system pkg + PyGObject | none (native egui) |
| Windows runtime dep | Microsoft Edge WebView2 Runtime | none |
| Distribution | Python + venv + pip install | single ~8 MB binary |
| Startup cost | Python interpreter + WebKit process | native process |
| GUI blocking | tailscale CLI on GUI thread risked hangs | background worker thread; GUI never blocks |
| Tray icon | pystray (optional import) | `tray-icon` crate behind `tray` feature |
| Same-language logic | arbitration/host_state re-imported | ported natively, no Python needed at runtime |

## Build

```
cd rust
cargo build --release                        # with tray (needs GTK/gdk-pixbuf on Linux)
cargo build --release --no-default-features  # window-only, no system deps
```

Windows / macOS: `cargo build --release` — tray feature works out of the box.

## Run

Place `chatbucket-manager` (or `chatbucket-manager.exe`) inside a ChatBucket
checkout — anywhere at or below the directory that holds `main.py`,
`arbitration.py`, `host_state.py`.

```
./chatbucket-manager           # GUI
./chatbucket-manager --cli     # stdout probe, no window
```

## Layout

```
src/
├── main.rs          entrypoint + repo-root discovery
├── app.rs           egui GUI, worker channel, CLI probe
├── manager.rs       ManagerContext: start/stop/update, derive_role_state
├── arbitration.rs   tailscale status + health check (read-only)
├── host_state.rs    atomic read/write of state/host-state.json
├── process_scan.rs  find ChatBucket processes, master/worker resolution
├── syncthing.rs     throttled /rest/db/status probe
├── update.rs        GitHub release check + allowlist/zip-slip extract
├── tray.rs          optional tray icon (feature = "tray")
└── repo.rs          content-based repo-root walker
```

## Design invariants preserved from the Python port

* `derive_role_state()` is the SINGLE reducer of (host_state, process, name)
  into a Role badge. Tray and window both call it — never re-derive.
* `host_state::write_state()` writes to a temp file in the same directory,
  fsyncs, then `std::fs::rename` — same atomicity contract as Python.
* Update install: allowlist of code dirs (`manager`, `web`, `static`,
  `scripts`) + allowlist of file suffixes (`.py`, `.txt`) + denylist of data
  dirs (`messages/`, `uploads/`, `state/`, …) + zip-slip guard on every
  extracted path.
* Stop on POSIX: SIGTERM to pgid when target IS its own group leader, else
  SIGTERM to the pid; wait `STOP_GRACE_SECONDS`; force-kill only after
  timeout, with a visible warning.
* Stop on Windows: `CTRL_BREAK_EVENT` to the process group (main.py launched
  with `CREATE_NEW_PROCESS_GROUP`).
* Stale-arbitrating processes (`main.py` older than 45 s stuck at role
  "arbitrating") are cleaned up on every Start/Stop so a wedged prior launch
  doesn't re-poison every fresh attempt.
