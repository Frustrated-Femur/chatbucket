# ChatBucket Manager — Front-Door Integration (Rust v0.3)

## Verified Python contract (front_door.py / manager_config.py)
- Status: GET http://127.0.0.1:5050/status → {routing, machine, child_running,
  child_pid, starting, last_error, auto_host, take_host_on_crash}
- routing is a STRING: "local" | "unavailable" | "redirect:<machine>"
- Control: POST http://127.0.0.1:5050/control {"action": "start"|"stop"|
  "rearbitrate"|"set_config", "config": {...}} → 202 {"accepted": true}
- manager_config.json keys: auto_host, take_host_on_crash (bools). Python
  preserves unknown keys on write; Rust preserves them via
  ManagerConfig::extras (#[serde(flatten)]).

## What changed
- src/front_door_client.rs  (NEW) — loopback client, verified contract, tests
- src/manager.rs            — derive_role_state() now takes FrontDoorStatus;
                              start/stop POST to /control; SyncSnapshot is the
                              only Syncthing state; SyncSnapshot::aggregate_health()
                              for the rail row; ProcessInfo carries a source tag
- src/syncthing.rs          — device self-filter in all_chatbucket_device_associations();
                              legacy sync-state shim deleted entirely;
                              ManagerConfig got extras flatten (preserves Python keys)
- src/arbitration.rs        — TailscaleIPs parsed; extract_ipv4() content-based;
                              PeerInfo.ipv4
- src/app.rs                — Network tab = peers only; Syncthing Config on Sync tab;
                              unconditional OPEN CHATBUCKET (http://127.0.0.1:5000/);
                              rail rows read the SAME snapshot; HOST OF RECORD card
                              renamed; legacy sync-state rendering gone; peers show IPv4
- src/process_scan.rs       — explicit fallback-only; recognizes front_door.py shape
- src/tray.rs               — same derive_role_state() via get_role_only()
- src/main.rs               — registers front_door_client module

## Verification
cargo check --no-default-features : clean
cargo test  --no-default-features : 80 passed, 0 failed
(tray feature needs system gdk-3.0; unchanged from v0.2)
