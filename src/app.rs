//! app.rs — egui GUI + CLI probe.
//!
//! Renders the same information the Python `web/index.html` shows: Role
//! badge (color-coded per role_state), Process card with Start/Stop, Claimed
//! host reachability, Tailnet peers, Syncthing (sync-state), Version + Check
//! for Updates. Uses ChatBucket's own design tokens (background/surface
//! stack, --success/--warn/--danger colors) so it visually reads as part of
//! the project.
//!
//! Background worker: a dedicated thread runs `ctx.get_status()` on a poll
//! interval and pushes results through a channel. The GUI thread only reads
//! the latest snapshot; it NEVER blocks on tailscale/HTTP work. Same intent
//! as Python's fast-refresh-while-STARTING logic (1.5s while starting, 15s
//! otherwise).

use crate::arbitration::{PeerInfo, PeerList};
use crate::host_state::HostState;
use crate::manager::{
    ActionResult, ClaimedReachability, ManagerContext, RoleDetail, RoleState, StatusSnapshot,
    UpdateCheckResult, UpdateInstallResult,
};
use crate::syncthing::SyncthingState;
use eframe::egui;
use egui::{Color32, RichText};
use std::sync::mpsc::{Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const REFRESH_NORMAL: Duration = Duration::from_secs(15);
const REFRESH_FAST: Duration = Duration::from_millis(1500);
const STARTING_MAX_VISIBLE: Duration = Duration::from_secs(45);

// ── Design tokens (mirror web/index.html :root) ────────────────────────
const C_BG: Color32 = Color32::from_rgb(0, 0, 0);
const C_SURFACE_2: Color32 = Color32::from_rgb(0x12, 0x12, 0x12);
const C_SURFACE_3: Color32 = Color32::from_rgb(0x1a, 0x1a, 0x1a);
const C_SURFACE_4: Color32 = Color32::from_rgb(0x24, 0x24, 0x24);
const C_BORDER_1: Color32 = Color32::from_rgb(0x1f, 0x1f, 0x1f);
const C_BORDER_2: Color32 = Color32::from_rgb(0x2a, 0x2a, 0x2a);
const C_TEXT: Color32 = Color32::from_rgb(0xf5, 0xf5, 0xf5);
const C_TEXT_2: Color32 = Color32::from_rgb(0xb5, 0xb5, 0xb5);
const C_TEXT_3: Color32 = Color32::from_rgb(0x7a, 0x7a, 0x7a);
const C_TEXT_4: Color32 = Color32::from_rgb(0x54, 0x54, 0x54);
const C_SUCCESS: Color32 = Color32::from_rgb(0x4a, 0xde, 0x80);
const C_WARN: Color32 = Color32::from_rgb(0xfb, 0xbf, 0x24);
const C_DANGER: Color32 = Color32::from_rgb(0xff, 0x6b, 0x6b);

// ── Messages the worker thread sends up to the GUI ─────────────────────
enum WorkerMsg {
    Snapshot(StatusSnapshot, Instant),
    ActionDone(ActionKind, ActionResult),
    UpdateCheck(UpdateCheckResult),
    UpdateInstall(UpdateInstallResult),
}

#[derive(Clone, Copy)]
enum ActionKind {
    Start,
    Stop,
}

// ── Commands the GUI sends down to the worker ─────────────────────────
enum WorkerCmd {
    Refresh,
    Start,
    Stop,
    CheckUpdate,
    InstallUpdate,
    Shutdown,
}

pub struct ManagerApp {
    ctx: Arc<ManagerContext>,
    snapshot: Option<StatusSnapshot>,
    last_updated: Option<Instant>,
    banner: Option<Banner>,
    busy: Busy,

    // last update-check info sticky in the UI even after refresh replaces the snapshot
    latest_tag: Option<String>,
    update_detail: Option<String>,
    can_install: bool,

    starting_since: Option<Instant>,

    cmd_tx: Sender<WorkerCmd>,
    msg_rx: Receiver<WorkerMsg>,

    // egui context so the worker thread can wake the GUI when new data arrives
    egui_ctx: Arc<Mutex<Option<egui::Context>>>,
}

#[derive(Default)]
struct Busy {
    start: bool,
    stop: bool,
    check_update: bool,
    install_update: bool,
    refresh: bool,
}

impl Busy {
    fn any(&self) -> bool {
        self.start || self.stop || self.check_update || self.install_update || self.refresh
    }
}

#[derive(Clone)]
struct Banner {
    kind: BannerKind,
    text: String,
}
#[derive(Clone, Copy, PartialEq)]
enum BannerKind {
    Error,
    Warn,
    Info,
}

impl ManagerApp {
    pub fn new(cc: &eframe::CreationContext<'_>, ctx: Arc<ManagerContext>) -> Self {
        apply_dark_style(&cc.egui_ctx);

        let (cmd_tx, cmd_rx) = std::sync::mpsc::channel::<WorkerCmd>();
        let (msg_tx, msg_rx) = std::sync::mpsc::channel::<WorkerMsg>();
        let egui_ctx_slot = Arc::new(Mutex::new(Some(cc.egui_ctx.clone())));
        spawn_worker(ctx.clone(), cmd_rx, msg_tx, egui_ctx_slot.clone());

        // Kick off first refresh immediately.
        let _ = cmd_tx.send(WorkerCmd::Refresh);

        Self {
            ctx,
            snapshot: None,
            last_updated: None,
            banner: None,
            busy: Busy::default(),
            latest_tag: None,
            update_detail: None,
            can_install: false,
            starting_since: None,
            cmd_tx,
            msg_rx,
            egui_ctx: egui_ctx_slot,
        }
    }

    fn drain_messages(&mut self) {
        while let Ok(msg) = self.msg_rx.try_recv() {
            match msg {
                WorkerMsg::Snapshot(snap, at) => {
                    // Track STARTING window for fast-refresh behaviour.
                    match &snap.role.state {
                        RoleState::Starting => {
                            if self.starting_since.is_none() {
                                self.starting_since = Some(Instant::now());
                            } else if let Some(since) = self.starting_since {
                                if since.elapsed() > STARTING_MAX_VISIBLE {
                                    self.set_banner(
                                        BannerKind::Warn,
                                        "STARTING has been shown for a while — the arbitration process may be wedged. Try Stop, then Start again.",
                                    );
                                }
                            }
                        }
                        _ => self.starting_since = None,
                    }
                    self.snapshot = Some(snap);
                    self.last_updated = Some(at);
                    self.busy.refresh = false;
                }
                WorkerMsg::ActionDone(kind, result) => {
                    match kind {
                        ActionKind::Start => self.busy.start = false,
                        ActionKind::Stop => self.busy.stop = false,
                    }
                    match (kind, result.ok, result.action) {
                        (ActionKind::Start, true, "started_unconfirmed") => {
                            self.set_banner(BannerKind::Warn, &result.detail);
                        }
                        (ActionKind::Start, false, _) => {
                            self.set_banner(BannerKind::Warn, &result.detail);
                        }
                        (ActionKind::Stop, _, "forced") => {
                            self.set_banner(BannerKind::Warn, &result.detail);
                        }
                        (ActionKind::Stop, false, _) => {
                            self.set_banner(
                                BannerKind::Warn,
                                if result.detail.is_empty() {
                                    "Stop did not complete cleanly."
                                } else {
                                    &result.detail
                                },
                            );
                        }
                        (ActionKind::Stop, true, "graceful") => {
                            self.set_banner(BannerKind::Info, &result.detail);
                        }
                        _ => {}
                    }
                    // Trigger a refresh right after any action.
                    let _ = self.cmd_tx.send(WorkerCmd::Refresh);
                    self.busy.refresh = true;
                }
                WorkerMsg::UpdateCheck(result) => {
                    self.busy.check_update = false;
                    self.latest_tag = result.latest.clone();
                    self.update_detail = Some(result.detail.clone());
                    self.can_install = result.ok && result.newer_available;
                    if !result.ok {
                        self.set_banner(BannerKind::Warn, &result.detail);
                    }
                }
                WorkerMsg::UpdateInstall(result) => {
                    self.busy.install_update = false;
                    self.update_detail = Some(result.detail.clone());
                    if result.ok && result.step == "done" {
                        self.can_install = false;
                        self.set_banner(BannerKind::Info, &result.detail);
                    } else if result.ok && result.step == "noop" {
                        self.set_banner(BannerKind::Info, &result.detail);
                    } else {
                        self.set_banner(BannerKind::Warn, &result.detail);
                    }
                    let _ = self.cmd_tx.send(WorkerCmd::Refresh);
                    self.busy.refresh = true;
                }
            }
        }
    }

    fn set_banner(&mut self, kind: BannerKind, text: &str) {
        self.banner = Some(Banner {
            kind,
            text: text.to_string(),
        });
    }
    fn clear_banner(&mut self) {
        self.banner = None;
    }
}

impl eframe::App for ManagerApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        // Keep the worker's context handle up-to-date (frame recreation).
        *self.egui_ctx.lock().unwrap() = Some(ctx.clone());

        self.drain_messages();

        // Repaint on our own schedule so poll/animation stays smooth even
        // when the user isn't interacting.
        let repaint_after = match self
            .snapshot
            .as_ref()
            .map(|s| s.role.state.clone())
            .unwrap_or(RoleState::Idle)
        {
            RoleState::Starting => Duration::from_millis(200),
            _ => Duration::from_millis(500),
        };
        ctx.request_repaint_after(repaint_after);

        egui::CentralPanel::default()
            .frame(egui::Frame::default().fill(C_BG).inner_margin(20.0))
            .show(ctx, |ui| {
                ui.style_mut().spacing.item_spacing.y = 10.0;
                self.render_header(ui);
                self.render_banner(ui);

                egui::ScrollArea::vertical()
                    .auto_shrink([false, false])
                    .show(ui, |ui| {
                        self.render_role_card(ui);
                        self.render_process_card(ui);
                        self.render_claimed_card(ui);
                        self.render_peers_card(ui);
                        self.render_syncthing_card(ui);
                        self.render_version_card(ui);
                        ui.add_space(8.0);
                        self.render_refresh_footer(ui);
                    });
            });
    }
}

impl ManagerApp {
    fn render_header(&self, ui: &mut egui::Ui) {
        ui.label(
            RichText::new("ChatBucket Manager")
                .color(C_TEXT)
                .size(18.0)
                .strong(),
        );
        let me = self
            .snapshot
            .as_ref()
            .map(|s| s.my_name.clone())
            .unwrap_or_else(|| self.ctx.my_name.clone());
        ui.label(
            RichText::new(format!("this machine: {}", me))
                .color(C_TEXT_3)
                .size(12.0),
        );
        ui.add_space(6.0);
    }

    fn render_banner(&mut self, ui: &mut egui::Ui) {
        let Some(banner) = self.banner.clone() else {
            return;
        };
        let (bg, fg, border) = match banner.kind {
            BannerKind::Error => (
                Color32::from_rgb(0x1a, 0x0e, 0x0e),
                C_DANGER,
                Color32::from_rgb(0x3a, 0x16, 0x16),
            ),
            BannerKind::Warn => (
                Color32::from_rgb(0x24, 0x1c, 0x06),
                C_WARN,
                Color32::from_rgb(0x4a, 0x3a, 0x0f),
            ),
            BannerKind::Info => (
                Color32::from_rgb(0x0e, 0x1a, 0x14),
                C_SUCCESS,
                Color32::from_rgb(0x16, 0x3a, 0x24),
            ),
        };
        let frame = egui::Frame::none()
            .fill(bg)
            .stroke(egui::Stroke::new(1.0, border))
            .rounding(egui::Rounding::same(10.0))
            .inner_margin(egui::Margin::symmetric(12.0, 8.0));
        frame.show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new(&banner.text).color(fg).size(12.0));
                if ui
                    .add(egui::Button::new(RichText::new("×").color(fg).size(12.0)).frame(false))
                    .clicked()
                {
                    self.clear_banner();
                }
            });
        });
    }

    fn render_role_card(&self, ui: &mut egui::Ui) {
        card(ui, "Role", |ui| {
            let (label, detail, color) = match self.snapshot.as_ref().map(|s| s.role.clone()) {
                Some(r) => (r.state.label().to_string(), r.detail, role_color(&r.state)),
                None => ("—".to_string(), "loading…".into(), C_TEXT_3),
            };
            ui.label(RichText::new(label).color(color).size(22.0).strong());
            ui.label(RichText::new(detail).color(C_TEXT_2).size(12.0));
        });
    }

    fn render_process_card(&mut self, ui: &mut egui::Ui) {
        card(ui, "Process", |ui| {
            let running = self
                .snapshot
                .as_ref()
                .and_then(|s| s.process.clone())
                .map(|p| format!("pid {} — {}", p.pid, p.role.as_str()));
            match running {
                Some(text) => status_row(ui, DotColor::Online, &text, "running"),
                None => status_row(ui, DotColor::Offline, "—", "not running"),
            }

            ui.horizontal(|ui| {
                let can_start = self
                    .snapshot
                    .as_ref()
                    .map(|s| s.process.is_none())
                    .unwrap_or(false)
                    && !self.busy.any();
                let can_stop = self
                    .snapshot
                    .as_ref()
                    .map(|s| s.process.is_some())
                    .unwrap_or(false)
                    && !self.busy.any();

                let start_label = if self.busy.start {
                    "Starting…"
                } else {
                    "Start"
                };
                let stop_label = if self.busy.stop {
                    "Stopping…"
                } else {
                    "Stop"
                };

                if action_button(ui, start_label, can_start, false).clicked() {
                    self.clear_banner();
                    self.busy.start = true;
                    let _ = self.cmd_tx.send(WorkerCmd::Start);
                }
                if action_button(ui, stop_label, can_stop, true).clicked() {
                    self.clear_banner();
                    self.busy.stop = true;
                    let _ = self.cmd_tx.send(WorkerCmd::Stop);
                }
            });
        });
    }

    fn render_claimed_card(&self, ui: &mut egui::Ui) {
        card(ui, "Claimed host reachability", |ui| {
            let Some(snap) = &self.snapshot else {
                status_row(ui, DotColor::Offline, "—", "loading…");
                return;
            };
            match (&snap.claimed_machine, &snap.claimed_reachability) {
                (None, _) => status_row(ui, DotColor::Offline, "—", "no claim to check"),
                (Some(name), Some(ClaimedReachability::IsSelf)) => {
                    status_row(ui, DotColor::Self_, name, "this machine")
                }
                (Some(name), Some(ClaimedReachability::Online)) => {
                    status_row(ui, DotColor::Online, name, "online")
                }
                (Some(name), Some(ClaimedReachability::Offline)) => {
                    status_row(ui, DotColor::Offline, name, "offline")
                }
                (Some(name), Some(ClaimedReachability::Error(e))) => {
                    status_row(ui, DotColor::Error, name, e)
                }
                (Some(name), None) => status_row(ui, DotColor::Offline, name, "…"),
            }
        });
    }

    fn render_peers_card(&self, ui: &mut egui::Ui) {
        card(ui, "Tailnet peers", |ui| {
            let Some(snap) = &self.snapshot else {
                status_row(ui, DotColor::Offline, "—", "loading…");
                return;
            };
            match &snap.tailnet_peers {
                Err(e) => status_row(ui, DotColor::Error, "—", &format!("error: {e}")),
                Ok(PeerList {
                    peers,
                    hidden_count,
                }) => {
                    if peers.is_empty() {
                        status_row(ui, DotColor::Offline, "—", "no peers found");
                    } else {
                        let mut sorted: Vec<&PeerInfo> = peers.iter().collect();
                        sorted.sort_by(|a, b| a.name.cmp(&b.name));
                        for p in sorted {
                            let dot = if p.online {
                                DotColor::Online
                            } else {
                                DotColor::Offline
                            };
                            status_row(ui, dot, &p.name, if p.online { "online" } else { "offline" });
                        }
                    }
                    if *hidden_count > 0 {
                        ui.add_space(6.0);
                        ui.separator();
                        ui.label(
                            RichText::new(format!(
                                "+{} infrastructure peer(s) hidden (no DNSName, likely Tailscale Funnel)",
                                hidden_count
                            ))
                            .color(C_TEXT_4)
                            .size(11.0),
                        );
                    }
                }
            }
        });
    }

    fn render_syncthing_card(&self, ui: &mut egui::Ui) {
        card(ui, "Syncthing (sync-state)", |ui| {
            let Some(snap) = &self.snapshot else {
                status_row(ui, DotColor::Offline, "sync-state", "loading…");
                return;
            };
            let (dot, label) = match &snap.syncthing {
                SyncthingState::NotConfigured => (DotColor::Offline, "not configured".to_string()),
                SyncthingState::InSync => (DotColor::Online, "in sync".to_string()),
                SyncthingState::Syncing => (DotColor::Warn, "syncing…".to_string()),
                SyncthingState::Error(d) => (DotColor::Error, d.clone()),
            };
            status_row(ui, dot, "sync-state", &label);
        });
    }

    fn render_version_card(&mut self, ui: &mut egui::Ui) {
        card(ui, "Version", |ui| {
            let installed = self
                .snapshot
                .as_ref()
                .and_then(|s| s.version.clone())
                .unwrap_or_else(|| "(missing)".to_string());
            let latest = self
                .latest_tag
                .clone()
                .unwrap_or_else(|| "not checked".to_string());
            version_row(ui, "installed", &installed);
            version_row(ui, "latest", &latest);
            if let Some(detail) = self.update_detail.clone() {
                ui.add_space(4.0);
                ui.separator();
                ui.label(RichText::new(detail).color(C_TEXT_2).size(12.0));
            }
            ui.horizontal(|ui| {
                let can_check = !self.busy.any();
                let can_install = self.can_install && !self.busy.any();
                let check_label = if self.busy.check_update {
                    "Checking…"
                } else {
                    "Check for Updates"
                };
                let install_label = if self.busy.install_update {
                    "Installing…"
                } else {
                    "Install Update"
                };
                if action_button(ui, check_label, can_check, false).clicked() {
                    self.clear_banner();
                    self.busy.check_update = true;
                    let _ = self.cmd_tx.send(WorkerCmd::CheckUpdate);
                }
                if self.can_install {
                    if action_button(ui, install_label, can_install, false).clicked() {
                        self.clear_banner();
                        self.busy.install_update = true;
                        let _ = self.cmd_tx.send(WorkerCmd::InstallUpdate);
                    }
                }
            });
        });
    }

    fn render_refresh_footer(&mut self, ui: &mut egui::Ui) {
        let refresh_label = if self.busy.refresh {
            "Refreshing…"
        } else {
            "Refresh"
        };
        if action_button(ui, refresh_label, !self.busy.any(), false).clicked() {
            self.busy.refresh = true;
            let _ = self.cmd_tx.send(WorkerCmd::Refresh);
        }
        if let Some(t) = self.last_updated {
            let secs = t.elapsed().as_secs();
            ui.vertical_centered(|ui| {
                ui.label(
                    RichText::new(format!("updated {}s ago", secs))
                        .color(C_TEXT_4)
                        .size(10.5),
                );
            });
        }
    }
}

impl Drop for ManagerApp {
    fn drop(&mut self) {
        let _ = self.cmd_tx.send(WorkerCmd::Shutdown);
    }
}

// ── Worker thread ─────────────────────────────────────────────────────

fn spawn_worker(
    ctx: Arc<ManagerContext>,
    cmd_rx: Receiver<WorkerCmd>,
    msg_tx: Sender<WorkerMsg>,
    egui_ctx: Arc<Mutex<Option<egui::Context>>>,
) {
    std::thread::Builder::new()
        .name("cb-mgr-worker".into())
        .spawn(move || {
            let mut last_refresh: Option<Instant> = None;
            loop {
                // Non-blocking command drain first so user actions are prompt.
                let mut got_cmd = false;
                while let Ok(cmd) = cmd_rx.try_recv() {
                    got_cmd = true;
                    match cmd {
                        WorkerCmd::Shutdown => return,
                        WorkerCmd::Refresh => {
                            let snap = ctx.get_status();
                            let _ = msg_tx.send(WorkerMsg::Snapshot(snap, Instant::now()));
                            last_refresh = Some(Instant::now());
                        }
                        WorkerCmd::Start => {
                            let r = ctx.start();
                            let _ = msg_tx.send(WorkerMsg::ActionDone(ActionKind::Start, r));
                        }
                        WorkerCmd::Stop => {
                            let r = ctx.stop();
                            let _ = msg_tx.send(WorkerMsg::ActionDone(ActionKind::Stop, r));
                        }
                        WorkerCmd::CheckUpdate => {
                            let r = ctx.check_for_updates();
                            let _ = msg_tx.send(WorkerMsg::UpdateCheck(r));
                        }
                        WorkerCmd::InstallUpdate => {
                            let r = ctx.install_update();
                            let _ = msg_tx.send(WorkerMsg::UpdateInstall(r));
                        }
                    }
                    // Wake GUI so it sees new messages promptly.
                    if let Some(c) = egui_ctx.lock().unwrap().as_ref() {
                        c.request_repaint();
                    }
                }

                // Periodic auto-refresh. Fast while STARTING (mimics the JS
                // fast-refresh); normal otherwise.
                let interval = match last_snapshot_role(&msg_tx) {
                    _ => REFRESH_NORMAL, // fast-refresh gating is now inside app; keep worker simple
                };
                let due = last_refresh.map(|t| t.elapsed() >= interval).unwrap_or(true);
                if due {
                    let snap = ctx.get_status();
                    let fast = matches!(snap.role.state, RoleState::Starting);
                    let _ = msg_tx.send(WorkerMsg::Snapshot(snap, Instant::now()));
                    last_refresh = Some(Instant::now());
                    if let Some(c) = egui_ctx.lock().unwrap().as_ref() {
                        c.request_repaint();
                    }
                    if fast {
                        std::thread::sleep(REFRESH_FAST);
                        continue;
                    }
                }

                if !got_cmd {
                    std::thread::sleep(Duration::from_millis(200));
                }
            }
        })
        .expect("spawn worker thread");
}

fn last_snapshot_role(_: &Sender<WorkerMsg>) -> RoleState {
    RoleState::Idle
}

// ── Small painters ────────────────────────────────────────────────────

fn card(ui: &mut egui::Ui, label: &str, body: impl FnOnce(&mut egui::Ui)) {
    let frame = egui::Frame::none()
        .fill(C_SURFACE_2)
        .stroke(egui::Stroke::new(1.0, C_BORDER_1))
        .rounding(egui::Rounding::same(14.0))
        .inner_margin(egui::Margin::symmetric(16.0, 14.0));
    frame.show(ui, |ui| {
        ui.label(
            RichText::new(label.to_uppercase())
                .color(C_TEXT_3)
                .size(10.5)
                .strong(),
        );
        ui.add_space(4.0);
        body(ui);
    });
}

#[derive(Clone, Copy)]
enum DotColor {
    Online,
    Offline,
    Error,
    Self_,
    Warn,
}
impl DotColor {
    fn color(self) -> Color32 {
        match self {
            DotColor::Online => C_SUCCESS,
            DotColor::Offline => Color32::from_rgb(0x55, 0x55, 0x55),
            DotColor::Error => C_DANGER,
            DotColor::Self_ => C_TEXT_3,
            DotColor::Warn => C_WARN,
        }
    }
}

fn status_row(ui: &mut egui::Ui, dot: DotColor, name: &str, state: &str) {
    ui.horizontal(|ui| {
        let (rect, _) = ui.allocate_exact_size(egui::vec2(10.0, 10.0), egui::Sense::hover());
        ui.painter()
            .circle_filled(rect.center(), 4.0, dot.color());
        ui.label(RichText::new(name).color(C_TEXT).size(13.0));
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.label(RichText::new(state).color(C_TEXT_3).size(11.5));
        });
    });
}

fn version_row(ui: &mut egui::Ui, label: &str, value: &str) {
    ui.horizontal(|ui| {
        ui.label(
            RichText::new(label.to_uppercase())
                .color(C_TEXT_3)
                .size(11.0)
                .strong(),
        );
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.label(
                RichText::new(value)
                    .color(C_TEXT)
                    .size(12.5)
                    .monospace(),
            );
        });
    });
}

fn action_button(ui: &mut egui::Ui, label: &str, enabled: bool, danger: bool) -> egui::Response {
    let stroke = if danger {
        egui::Stroke::new(1.0, Color32::from_rgb(0x4a, 0x20, 0x20))
    } else {
        egui::Stroke::new(1.0, C_BORDER_2)
    };
    let btn = egui::Button::new(RichText::new(label).color(C_TEXT).size(13.0).strong())
        .fill(C_SURFACE_4)
        .stroke(stroke)
        .rounding(egui::Rounding::same(10.0))
        .min_size(egui::vec2(0.0, 32.0));
    ui.add_enabled(enabled, btn)
}

fn role_color(state: &RoleState) -> Color32 {
    match state {
        RoleState::Host => C_SUCCESS,
        RoleState::Client => C_TEXT,
        RoleState::Idle => C_TEXT_3,
        RoleState::Starting => C_TEXT_2,
        RoleState::Stale => C_WARN,
        RoleState::Conflict | RoleState::Unknown => C_DANGER,
    }
}

fn apply_dark_style(ctx: &egui::Context) {
    let mut style = (*ctx.style()).clone();
    style.visuals.window_fill = C_BG;
    style.visuals.panel_fill = C_BG;
    style.visuals.extreme_bg_color = C_SURFACE_3;
    style.visuals.override_text_color = Some(C_TEXT);
    style.visuals.widgets.noninteractive.bg_fill = C_SURFACE_2;
    style.visuals.widgets.inactive.bg_fill = C_SURFACE_4;
    style.visuals.widgets.hovered.bg_fill = Color32::from_rgb(0x2e, 0x2e, 0x2e);
    style.visuals.widgets.active.bg_fill = Color32::from_rgb(0x35, 0x35, 0x35);
    style.spacing.item_spacing = egui::vec2(8.0, 10.0);
    ctx.set_style(style);
}

// ── CLI probe ─────────────────────────────────────────────────────────

pub fn cli_probe() -> anyhow::Result<()> {
    let repo_root = crate::repo::find_repo_root_from_exe()?;
    let ctx = ManagerContext::new(repo_root.clone());
    println!("Detected machine name: {}", ctx.my_name);
    println!("Repo root: {}", repo_root.display());
    println!();

    let snap = ctx.get_status();

    println!("=== host-state.json ===");
    match &snap.host_state {
        Ok(None) => println!("  No claim on record (nobody has ever hosted)."),
        Ok(Some(s)) => print_host_state(s),
        Err(e) => println!("  CORRUPTED: {e}"),
    }
    println!();

    println!("=== process ===");
    match &snap.process {
        None => println!("  Not running."),
        Some(p) => println!(
            "  Running: pid {}, role: {}, subshape: {:?}",
            p.pid,
            p.role.as_str(),
            p.subshape
        ),
    }
    println!();

    println!("=== reconciled role ===");
    let r: &RoleDetail = &snap.role;
    println!("  {} — {}", r.state.label(), r.detail);
    println!();

    println!("=== version ===");
    println!(
        "  local VERSION: {}",
        snap.version.clone().unwrap_or_else(|| "(missing)".into())
    );
    println!();

    println!("=== claimed host status ===");
    match (&snap.claimed_machine, &snap.claimed_reachability) {
        (None, _) => println!("  (no claim on record — nothing to check)"),
        (Some(name), Some(ClaimedReachability::IsSelf)) => {
            println!("  {}: (this machine)", name)
        }
        (Some(name), Some(ClaimedReachability::Online)) => println!("  {}: online", name),
        (Some(name), Some(ClaimedReachability::Offline)) => println!("  {}: offline", name),
        (Some(name), Some(ClaimedReachability::Error(e))) => println!("  {}: ERROR — {}", name, e),
        (Some(name), None) => println!("  {}: (not checked)", name),
    }
    println!();

    println!("=== tailnet peers (dynamic, infra hidden) ===");
    match &snap.tailnet_peers {
        Err(e) => println!("  ERROR — {e}"),
        Ok(list) => {
            if list.peers.is_empty() {
                println!("  (no peers found)");
            }
            let mut peers: Vec<&PeerInfo> = list.peers.iter().collect();
            peers.sort_by(|a, b| a.name.cmp(&b.name));
            for p in peers {
                println!("  {}: {}", p.name, if p.online { "online" } else { "offline" });
            }
            if list.hidden_count > 0 {
                println!(
                    "  (+{} infrastructure peer(s) hidden — no DNSName, likely Tailscale Funnel)",
                    list.hidden_count
                );
            }
        }
    }
    println!();

    println!("=== syncthing (sync-state folder) ===");
    match &snap.syncthing {
        SyncthingState::NotConfigured => {
            println!("  not configured (no manager_config.json / syncthing_api_key)")
        }
        SyncthingState::InSync => println!("  in_sync"),
        SyncthingState::Syncing => println!("  syncing"),
        SyncthingState::Error(d) => println!("  ERROR — {d}"),
    }
    Ok(())
}

fn print_host_state(s: &HostState) {
    println!("  action:    {}", s.action);
    println!("  machine:   {}", s.machine);
    println!("  timestamp: {}", s.timestamp);
}
