//! chatbucket-manager (Rust) — native GUI manager for ChatBucket.
//!
//! Migrated from the pywebview-based Python `manager_main.py`. Key improvements:
//!
//!   * No WebView dependency (no webkit2gtk on Linux, no WebView2 on Windows).
//!   * Native egui GUI — single self-contained binary.
//!   * Same design tokens as `web/index.html` (surface stack, --success/--warn/
//!     --danger colors) so it visually reads as part of ChatBucket.
//!   * Same "one source of truth" discipline that §16.5 of the architecture doc
//!     established for Role state: `derive_role_state()` is the ONLY function
//!     that reduces (claim, process, arbitration) into a UI label. The tray
//!     path and window path both call it — never re-derive independently.
//!   * Background worker thread does status polling + start/stop/update work,
//!     so the GUI thread never blocks on `tailscale status` (up to 5s) or on a
//!     STOP_GRACE_SECONDS-long wait.
//!
//! Module wiring is unchanged from v0.2; the initial window size grew to fit
//! the left navigation rail, and a Sync tab was added for the Syncthing
//! control plane (manager-syncthing-setup_4.md).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod app;
mod arbitration;
mod host_state;
mod manager;
mod process_scan;
mod repo;
mod resources;
mod syncthing;
mod tray;
mod update;

use std::sync::Arc;

fn main() -> Result<(), eframe::Error> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    if std::env::args().any(|a| a == "--cli") {
        return match app::cli_probe() {
            Ok(()) => Ok(()),
            Err(e) => {
                eprintln!("cli probe failed: {e:#}");
                std::process::exit(2);
            }
        };
    }

    let repo_root = match repo::find_repo_root_from_exe() {
        Ok(p) => p,
        Err(e) => {
            eprintln!(
                "Could not locate the ChatBucket repo root: {e}\n\
                 Place chatbucket-manager next to arbitration.py / host_state.py / main.py,\n\
                 or run it from anywhere inside a ChatBucket checkout."
            );
            std::process::exit(2);
        }
    };

    let ctx = Arc::new(manager::ManagerContext::new(repo_root));

    let native_options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([880.0, 720.0]) // wider — rail needs 220 px
            .with_min_inner_size([760.0, 620.0])
            .with_title("ChatBucket Manager"),
        ..Default::default()
    };

    let _tray_keepalive = tray::spawn_tray(ctx.clone());

    let ctx_for_app = ctx.clone();
    eframe::run_native(
        "ChatBucket Manager",
        native_options,
        Box::new(move |cc| Box::new(app::ManagerApp::new(cc, ctx_for_app))),
    )
}
