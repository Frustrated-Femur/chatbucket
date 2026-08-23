//! app.rs — egui GUI + worker thread + CLI probe.
//!
//! REDESIGN NOTES (functionality preserved from v0.2):
//!   * Vertical LEFT navigation rail with icon + label + amber active seam.
//!   * Custom fonts embedded via include_bytes!:
//!       - Michroma      → display (wordmark, role badge, eyebrows)
//!       - IBM Plex Sans → body / buttons
//!       - IBM Plex Mono → timestamps, PIDs, versions, logs
//!   * Motion via egui animations: dots pulse when "live", tab active seam
//!     tweens between rows, buttons ease their fill on hover/press.
//!   * Same design tokens as before but pushed toward "instrument panel":
//!     obsidian bg (#050505), hairline borders (#1c1c1c), amber accent (#ffb547).
//!   * Threading model, message channel, worker loop, and CLI probe are
//!     unchanged from v0.2 — this file only reworks presentation.

use crate::arbitration::{PeerInfo, PeerList};
use crate::host_state::HostState;
use crate::manager::SyncSnapshot;
use crate::manager::{
    ActionResult, ClaimedReachability, ConfigSaveResult, ManagerContext, RoleDetail, RoleState,
    StatusSnapshot, UpdateCheckResult, UpdateInstallResult,
};
use crate::resources::{self, FolderHealth};
use crate::syncthing::LegacyState as SyncthingState;
use crate::syncthing::{ConnectionState, InstallState, ManagerConfig};
use crate::syncthing::{DeviceAddOutcome, ManagedTransition};

fn res_msg(r: Result<impl std::fmt::Debug, String>, ok: &str) -> String {
    match r {
        Ok(_) => ok.to_string(),
        Err(e) => format!("failed: {e}"),
    }
}
use eframe::egui;
use egui::{Color32, FontData, FontDefinitions, FontFamily, RichText, Stroke};
use std::collections::VecDeque;
use std::sync::mpsc::{Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const REFRESH_NORMAL: Duration = Duration::from_secs(15);
const REFRESH_FAST: Duration = Duration::from_millis(1500);
const STARTING_MAX_VISIBLE: Duration = Duration::from_secs(45);
const LOG_CAP: usize = 200;

// ── Design tokens ─────────────────────────────────────────────────────
const C_BG: Color32 = Color32::from_rgb(0x05, 0x05, 0x05); // obsidian
const C_RAIL: Color32 = Color32::from_rgb(0x08, 0x08, 0x08);
const C_SURFACE_2: Color32 = Color32::from_rgb(0x0d, 0x0d, 0x0e);
const C_SURFACE_3: Color32 = Color32::from_rgb(0x15, 0x15, 0x17);
const C_SURFACE_4: Color32 = Color32::from_rgb(0x1e, 0x1e, 0x21);
const C_SURFACE_HI: Color32 = Color32::from_rgb(0x28, 0x28, 0x2c);
const C_BORDER_1: Color32 = Color32::from_rgb(0x1c, 0x1c, 0x1e);
const C_BORDER_2: Color32 = Color32::from_rgb(0x2a, 0x2a, 0x2e);
const C_TEXT: Color32 = Color32::from_rgb(0xf2, 0xf1, 0xed);
const C_TEXT_2: Color32 = Color32::from_rgb(0xbb, 0xb8, 0xb0);
const C_TEXT_3: Color32 = Color32::from_rgb(0x82, 0x7f, 0x77);
const C_TEXT_4: Color32 = Color32::from_rgb(0x55, 0x53, 0x4d);
const C_SUCCESS: Color32 = Color32::from_rgb(0x6e, 0xe7, 0x8a); // phosphor
const C_WARN: Color32 = Color32::from_rgb(0xff, 0xb5, 0x47); // amber
const C_DANGER: Color32 = Color32::from_rgb(0xff, 0x6b, 0x6b);
const C_ACCENT: Color32 = Color32::from_rgb(0xff, 0xb5, 0x47); // the signature amber

// Font family aliases we register at boot.
const F_DISPLAY: &str = "cb_display"; // Michroma
const F_UI: &str = "cb_ui"; // IBM Plex Sans
const F_UI_B: &str = "cb_ui_b"; // IBM Plex Sans SemiBold
const F_MONO: &str = "cb_mono"; // IBM Plex Mono

// ── Worker protocol (unchanged) ───────────────────────────────────────
#[allow(clippy::large_enum_variant)] // Snapshot is the hot path; boxing every
                                     // message to shave the small variants off would cost an allocation per refresh.
enum WorkerMsg {
    Snapshot(StatusSnapshot, Instant),
    ActionDone(ActionKind, ActionResult),
    UpdateCheck(UpdateCheckResult),
    UpdateInstall(UpdateInstallResult),
    ConfigSaved(ConfigSaveResult),
    ConfigTested(SyncthingState),
    /// Result of a Syncthing control-plane action (reconcile/scan/add/etc).
    SyncDone(String),
    /// A pending ChatBucket request appeared (event-driven, §17).
    PendingChanged,
    /// Reserved for worker-emitted log lines (the worker currently logs via
    /// the snapshot/action results; kept for parity with the protocol).
    #[allow(dead_code)]
    Log(String),
}
#[derive(Clone, Copy)]
enum ActionKind {
    Start,
    Stop,
}
enum WorkerCmd {
    Refresh,
    Start,
    Stop,
    CheckUpdate,
    InstallUpdate,
    SaveConfig(ManagerConfig),
    TestSyncthing,
    Shutdown,
    // Syncthing control plane
    SyncAutoConnect,
    SyncLaunch,
    SyncReconcile,
    SyncScan(String),
    SyncScanAll,
    SyncPause(String),
    SyncResume(String),
    SyncRestart,
    SyncClearErrors,
    SyncSetManaged(String, bool),
    SyncAddDevice(String),
    SyncRemoveDevice(String),
    SyncAcceptPending(String, Vec<String>),
    SyncRejectPending(String),
}

// ── Tabs ──────────────────────────────────────────────────────────────
#[derive(Clone, Copy, PartialEq, Eq)]
enum Tab {
    Status,
    Sync,
    Network,
    Updates,
    Logs,
}

impl Tab {
    const ALL: [Tab; 5] = [
        Tab::Status,
        Tab::Sync,
        Tab::Network,
        Tab::Updates,
        Tab::Logs,
    ];
    fn label(self) -> &'static str {
        match self {
            Tab::Status => "Status",
            Tab::Sync => "Sync",
            Tab::Network => "Network",
            Tab::Updates => "Updates",
            Tab::Logs => "Event log",
        }
    }
    fn glyph(self) -> &'static str {
        // Unicode geometric marks — render on any glyph set, no icon font needed.
        match self {
            Tab::Status => "◉",
            Tab::Sync => "⇄",
            Tab::Network => "◈",
            Tab::Updates => "▲",
            Tab::Logs => "≡",
        }
    }
    fn subtitle(self) -> &'static str {
        match self {
            Tab::Status => "role • process • claim",
            Tab::Sync => "syncthing • folders • devices",
            Tab::Network => "tailnet • syncthing",
            Tab::Updates => "release channel",
            Tab::Logs => "live event stream",
        }
    }
}

pub struct ManagerApp {
    ctx: Arc<ManagerContext>,
    snapshot: Option<StatusSnapshot>,
    last_updated: Option<Instant>,
    banner: Option<Banner>,
    busy: Busy,
    tab: Tab,

    latest_tag: Option<String>,
    latest_url: Option<String>,
    update_detail: Option<String>,
    can_install: bool,

    starting_since: Option<Instant>,

    cfg_api_key: String,
    cfg_url: String,
    cfg_show_key: bool,
    cfg_dirty: bool,
    cfg_status_line: Option<String>,
    cfg_parse_err: Option<String>,
    cfg_disk_snapshot: (String, String),

    logs: VecDeque<String>,

    // Sync tab UI state
    sync_new_device_id: String,
    sync_status_line: Option<String>,

    cmd_tx: Sender<WorkerCmd>,
    msg_rx: Receiver<WorkerMsg>,
    egui_ctx: Arc<Mutex<Option<egui::Context>>>,

    // Boot animation — used for the initial fade-in on the main surface.
    boot_at: Instant,
}

#[derive(Default)]
struct Busy {
    start: bool,
    stop: bool,
    check_update: bool,
    install_update: bool,
    refresh: bool,
    save_config: bool,
    test_syncthing: bool,
    sync: bool,
}
impl Busy {
    fn any(&self) -> bool {
        self.start
            || self.stop
            || self.check_update
            || self.install_update
            || self.refresh
            || self.save_config
            || self.test_syncthing
            || self.sync
    }
    fn any_lifecycle(&self) -> bool {
        self.start || self.stop || self.install_update
    }
}

#[derive(Clone)]
struct Banner {
    kind: BannerKind,
    text: String,
}
#[derive(Clone, Copy, PartialEq)]
enum BannerKind {
    /// Reserved for hard-error banners; current flows surface everything
    /// user-actionable as Warn. Kept so the match arms stay exhaustive.
    #[allow(dead_code)]
    Error,
    Warn,
    Info,
}

impl ManagerApp {
    pub fn new(cc: &eframe::CreationContext<'_>, ctx: Arc<ManagerContext>) -> Self {
        install_fonts(&cc.egui_ctx);
        apply_dark_style(&cc.egui_ctx);

        let (cmd_tx, cmd_rx) = std::sync::mpsc::channel::<WorkerCmd>();
        let (msg_tx, msg_rx) = std::sync::mpsc::channel::<WorkerMsg>();
        let egui_ctx_slot = Arc::new(Mutex::new(Some(cc.egui_ctx.clone())));
        spawn_worker(ctx.clone(), cmd_rx, msg_tx.clone(), egui_ctx_slot.clone());
        spawn_pending_watcher(ctx.clone(), msg_tx, egui_ctx_slot.clone());
        let _ = cmd_tx.send(WorkerCmd::Refresh);

        let (cfg, parse_err) = ctx.read_config();
        let cfg_api_key = cfg.syncthing_api_key.clone().unwrap_or_default();
        let cfg_url = cfg.syncthing_url.clone().unwrap_or_default();

        let mut app = Self {
            ctx,
            snapshot: None,
            last_updated: None,
            banner: None,
            busy: Busy::default(),
            tab: Tab::Status,
            latest_tag: None,
            latest_url: None,
            update_detail: None,
            can_install: false,
            starting_since: None,
            cfg_api_key: cfg_api_key.clone(),
            cfg_url: cfg_url.clone(),
            cfg_show_key: false,
            cfg_dirty: false,
            cfg_status_line: None,
            cfg_parse_err: parse_err,
            cfg_disk_snapshot: (cfg_api_key, cfg_url),
            logs: VecDeque::with_capacity(LOG_CAP),
            sync_new_device_id: String::new(),
            sync_status_line: None,
            cmd_tx,
            msg_rx,
            egui_ctx: egui_ctx_slot,
            boot_at: Instant::now(),
        };
        app.push_log("manager started");
        app
    }

    fn push_log(&mut self, line: &str) {
        let ts = chrono::Local::now().format("%H:%M:%S").to_string();
        self.logs.push_back(format!("[{ts}] {line}"));
        while self.logs.len() > LOG_CAP {
            self.logs.pop_front();
        }
    }

    fn drain_messages(&mut self) {
        while let Ok(msg) = self.msg_rx.try_recv() {
            match msg {
                WorkerMsg::Log(line) => self.push_log(&line),
                WorkerMsg::Snapshot(snap, at) => {
                    match &snap.role.state {
                        RoleState::Starting => {
                            if self.starting_since.is_none() {
                                self.starting_since = Some(Instant::now());
                            } else if let Some(since) = self.starting_since {
                                if since.elapsed() > STARTING_MAX_VISIBLE {
                                    self.set_banner(
                                        BannerKind::Warn,
                                        "STARTING has been visible for a while — arbitration may be wedged. Try Stop, then Start.",
                                    );
                                }
                            }
                        }
                        _ => self.starting_since = None,
                    }
                    let new_role = snap.role.state.label().to_string();
                    let prev = self
                        .snapshot
                        .as_ref()
                        .map(|s| s.role.state.label().to_string());
                    if prev.as_deref() != Some(new_role.as_str()) {
                        self.push_log(&format!("role → {}", new_role));
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
                    self.push_log(&format!(
                        "{}: {} — {}",
                        match kind {
                            ActionKind::Start => "start",
                            ActionKind::Stop => "stop",
                        },
                        result.action,
                        result.detail
                    ));
                    match (kind, result.ok, result.action) {
                        (ActionKind::Start, true, "started_unconfirmed") => {
                            self.set_banner(BannerKind::Warn, &result.detail)
                        }
                        (ActionKind::Start, false, _) => {
                            self.set_banner(BannerKind::Warn, &result.detail)
                        }
                        (ActionKind::Stop, _, "forced") => {
                            self.set_banner(BannerKind::Warn, &result.detail)
                        }
                        (ActionKind::Stop, false, _) => self.set_banner(
                            BannerKind::Warn,
                            if result.detail.is_empty() {
                                "Stop did not complete cleanly."
                            } else {
                                &result.detail
                            },
                        ),
                        (ActionKind::Stop, true, "graceful") => {
                            self.set_banner(BannerKind::Info, &result.detail)
                        }
                        _ => {}
                    }
                    let _ = self.cmd_tx.send(WorkerCmd::Refresh);
                    self.busy.refresh = true;
                }
                WorkerMsg::UpdateCheck(result) => {
                    self.busy.check_update = false;
                    self.push_log(&format!("update check: {}", result.detail));
                    self.latest_tag = result.latest.clone();
                    self.latest_url = result.release_url.clone();
                    self.update_detail = Some(result.detail.clone());
                    self.can_install = result.ok && result.newer_available;
                    if !result.ok {
                        self.set_banner(BannerKind::Warn, &result.detail);
                    }
                }
                WorkerMsg::UpdateInstall(result) => {
                    self.busy.install_update = false;
                    self.push_log(&format!(
                        "update install: {} — {}",
                        result.step, result.detail
                    ));
                    self.update_detail = Some(result.detail.clone());
                    if result.ok && (result.step == "done" || result.step == "noop") {
                        self.can_install = false;
                        self.set_banner(BannerKind::Info, &result.detail);
                    } else {
                        self.set_banner(BannerKind::Warn, &result.detail);
                    }
                    let _ = self.cmd_tx.send(WorkerCmd::Refresh);
                    self.busy.refresh = true;
                }
                WorkerMsg::ConfigSaved(result) => {
                    self.busy.save_config = false;
                    self.push_log(&format!("config save: {}", result.detail));
                    self.cfg_status_line = Some(result.detail.clone());
                    if let Some(saved) = result.saved.as_ref() {
                        let k = saved.syncthing_api_key.clone().unwrap_or_default();
                        let u = saved.syncthing_url.clone().unwrap_or_default();
                        self.cfg_api_key = k.clone();
                        self.cfg_url = u.clone();
                        self.cfg_disk_snapshot = (k, u);
                        self.cfg_dirty = false;
                        self.cfg_parse_err = None;
                    }
                    if !result.ok {
                        self.set_banner(BannerKind::Warn, &result.detail);
                    }
                    let _ = self.cmd_tx.send(WorkerCmd::Refresh);
                    self.busy.refresh = true;
                }
                WorkerMsg::ConfigTested(state) => {
                    self.busy.test_syncthing = false;
                    self.push_log(&format!("syncthing test → {}", state.short_label()));
                    self.cfg_status_line = Some(format!(
                        "Test result: {} — {}",
                        state.short_label(),
                        state.detail()
                    ));
                    let _ = self.cmd_tx.send(WorkerCmd::Refresh);
                    self.busy.refresh = true;
                }
                WorkerMsg::SyncDone(line) => {
                    self.busy.sync = false;
                    self.push_log(&format!("sync: {line}"));
                    self.sync_status_line = Some(line.clone());
                    if line.to_lowercase().contains("fail") || line.contains("CONFLICT") {
                        self.set_banner(BannerKind::Warn, &line);
                    }
                    let _ = self.cmd_tx.send(WorkerCmd::Refresh);
                    self.busy.refresh = true;
                }
                WorkerMsg::PendingChanged => {
                    self.push_log("pending ChatBucket request detected (event)");
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

    fn detect_dirty(&self) -> bool {
        let (dk, du) = &self.cfg_disk_snapshot;
        self.cfg_api_key.trim() != dk.trim() || self.cfg_url.trim() != du.trim()
    }
}

impl eframe::App for ManagerApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        *self.egui_ctx.lock().unwrap() = Some(ctx.clone());
        self.drain_messages();

        let repaint_after = match self
            .snapshot
            .as_ref()
            .map(|s| s.role.state.clone())
            .unwrap_or(RoleState::Idle)
        {
            RoleState::Starting => Duration::from_millis(120),
            _ => Duration::from_millis(400),
        };
        ctx.request_repaint_after(repaint_after);

        // ── ROOT: SidePanel (left rail) + CentralPanel (main) ─────────
        egui::SidePanel::left("cb_rail")
            .exact_width(228.0)
            .resizable(false)
            .frame(
                egui::Frame::none()
                    .fill(C_RAIL)
                    .stroke(Stroke::new(1.0_f32, C_BORDER_1))
                    .inner_margin(egui::Margin::symmetric(18.0, 22.0)),
            )
            .show(ctx, |ui| self.render_rail(ui));

        egui::CentralPanel::default()
            .frame(
                egui::Frame::default()
                    .fill(C_BG)
                    .inner_margin(egui::Margin::same(0.0)),
            )
            .show(ctx, |ui| {
                // Boot fade-in on the main surface (staggered reveal).
                let elapsed = self.boot_at.elapsed().as_secs_f32();
                let boot_alpha = (elapsed * 2.2).clamp(0.0, 1.0);
                ctx.request_repaint_after(Duration::from_millis(16));

                ui.vertical(|ui| {
                    self.render_top_strip(ui);
                    self.render_banner(ui);
                    ui.add_space(6.0);

                    // Alpha-fade the tab body
                    let fade = egui::Frame::none()
                        .fill(Color32::from_rgba_unmultiplied(
                            0,
                            0,
                            0,
                            ((1.0 - boot_alpha) * 255.0) as u8,
                        ))
                        .inner_margin(egui::Margin::same(0.0));

                    egui::ScrollArea::vertical()
                        .auto_shrink([false, false])
                        .show(ui, |ui| {
                            egui::Frame::none()
                                .inner_margin(egui::Margin::symmetric(26.0, 6.0))
                                .show(ui, |ui| match self.tab {
                                    Tab::Status => self.render_status_tab(ui),
                                    Tab::Sync => self.render_sync_tab(ui),
                                    Tab::Network => self.render_network_tab(ui),
                                    Tab::Updates => self.render_updates_tab(ui),
                                    Tab::Logs => self.render_logs_tab(ui),
                                });
                            // Overlay the boot veil.
                            let rect = ui.max_rect();
                            fade.show(ui, |ui| {
                                ui.allocate_rect(rect, egui::Sense::hover());
                            });
                        });
                });
            });
    }
}

impl ManagerApp {
    // ── LEFT RAIL ─────────────────────────────────────────────────────
    fn render_rail(&mut self, ui: &mut egui::Ui) {
        // Wordmark
        ui.horizontal(|ui| {
            // Amber square dot as a logomark
            let (rect, _) = ui.allocate_exact_size(egui::vec2(10.0, 10.0), egui::Sense::hover());
            ui.painter()
                .rect_filled(rect, egui::Rounding::same(1.5), C_ACCENT);
            ui.add_space(4.0);
            ui.label(
                RichText::new("CHATBUCKET")
                    .family(FontFamily::Name(F_DISPLAY.into()))
                    .color(C_TEXT)
                    .size(11.5),
            );
        });
        ui.add_space(2.0);
        ui.label(
            RichText::new("manager · v0.2")
                .family(FontFamily::Name(F_MONO.into()))
                .color(C_TEXT_4)
                .size(10.0),
        );

        ui.add_space(22.0);

        // Section eyebrow
        ui.label(
            RichText::new("NAVIGATE")
                .family(FontFamily::Name(F_DISPLAY.into()))
                .color(C_TEXT_3)
                .size(9.0),
        );
        ui.add_space(10.0);

        // Nav items
        for tab in Tab::ALL {
            self.render_rail_item(ui, tab);
            ui.add_space(4.0);
        }

        ui.add_space(24.0);

        // ── Live status compact block ─────────────────────────────────
        ui.label(
            RichText::new("LIVE")
                .family(FontFamily::Name(F_DISPLAY.into()))
                .color(C_TEXT_3)
                .size(9.0),
        );
        ui.add_space(8.0);
        rail_live_row(
            ui,
            "role",
            self.snapshot
                .as_ref()
                .map(|s| s.role.state.label())
                .unwrap_or("—"),
            self.snapshot
                .as_ref()
                .map(|s| role_color(&s.role.state))
                .unwrap_or(C_TEXT_3),
            self.snapshot
                .as_ref()
                .map(|s| {
                    matches!(
                        s.role.state,
                        RoleState::Host | RoleState::Client | RoleState::Starting
                    )
                })
                .unwrap_or(false),
        );
        ui.add_space(6.0);
        rail_live_row(
            ui,
            "process",
            self.snapshot
                .as_ref()
                .map(|s| {
                    if s.process.is_some() {
                        "running"
                    } else {
                        "stopped"
                    }
                })
                .unwrap_or("—"),
            self.snapshot
                .as_ref()
                .map(|s| {
                    if s.process.is_some() {
                        C_SUCCESS
                    } else {
                        C_TEXT_3
                    }
                })
                .unwrap_or(C_TEXT_3),
            self.snapshot
                .as_ref()
                .map(|s| s.process.is_some())
                .unwrap_or(false),
        );
        ui.add_space(6.0);
        rail_live_row(
            ui,
            "sync",
            self.snapshot
                .as_ref()
                .map(|s| s.syncthing.short_label())
                .unwrap_or_else(|| "—".into())
                .as_str(),
            self.snapshot
                .as_ref()
                .map(|s| syncthing_color(&s.syncthing))
                .unwrap_or(C_TEXT_3),
            self.snapshot
                .as_ref()
                .map(|s| {
                    matches!(
                        s.syncthing,
                        SyncthingState::InSync | SyncthingState::Syncing
                    )
                })
                .unwrap_or(false),
        );

        // Push footer to bottom
        ui.with_layout(egui::Layout::bottom_up(egui::Align::LEFT), |ui| {
            if let Some(snap) = &self.snapshot {
                ui.label(
                    RichText::new(&snap.my_name)
                        .family(FontFamily::Name(F_MONO.into()))
                        .color(C_TEXT_2)
                        .size(11.0),
                );
                ui.label(
                    RichText::new("THIS MACHINE")
                        .family(FontFamily::Name(F_DISPLAY.into()))
                        .color(C_TEXT_4)
                        .size(8.5),
                );
            }
        });
    }

    fn render_rail_item(&mut self, ui: &mut egui::Ui, tab: Tab) {
        let selected = self.tab == tab;
        let id = ui.id().with(("rail", tab as usize));
        let anim = ui.ctx().animate_bool_with_time(id, selected, 0.22);

        let desired = egui::vec2(ui.available_width(), 48.0);
        let (rect, response) = ui.allocate_exact_size(desired, egui::Sense::click());
        let hovered = response.hovered();
        let hover_anim = ui.ctx().animate_bool_with_time(id.with("h"), hovered, 0.12);

        let fill = lerp_color(C_RAIL, C_SURFACE_3, anim.max(hover_anim * 0.55));
        ui.painter()
            .rect_filled(rect, egui::Rounding::same(10.0), fill);

        // Left amber seam — grows on active/hover
        let seam_h = 20.0 + 18.0 * anim;
        let seam_rect = egui::Rect::from_min_size(
            egui::pos2(rect.min.x + 0.0, rect.center().y - seam_h / 2.0),
            egui::vec2(3.0, seam_h),
        );
        let seam_col = if selected {
            C_ACCENT
        } else {
            Color32::from_rgba_unmultiplied(
                C_ACCENT.r(),
                C_ACCENT.g(),
                C_ACCENT.b(),
                (60.0 * hover_anim) as u8,
            )
        };
        ui.painter()
            .rect_filled(seam_rect, egui::Rounding::same(2.0), seam_col);

        // Glyph
        let glyph_col = lerp_color(C_TEXT_3, C_TEXT, anim);
        ui.painter().text(
            egui::pos2(rect.min.x + 20.0, rect.center().y),
            egui::Align2::LEFT_CENTER,
            tab.glyph(),
            egui::FontId::new(15.0, FontFamily::Name(F_DISPLAY.into())),
            glyph_col,
        );

        // Label + subtitle
        let label_col = lerp_color(C_TEXT_2, C_TEXT, anim);
        ui.painter().text(
            egui::pos2(rect.min.x + 44.0, rect.center().y - 8.0),
            egui::Align2::LEFT_CENTER,
            tab.label(),
            egui::FontId::new(13.0, FontFamily::Name(F_UI_B.into())),
            label_col,
        );
        ui.painter().text(
            egui::pos2(rect.min.x + 44.0, rect.center().y + 8.0),
            egui::Align2::LEFT_CENTER,
            tab.subtitle(),
            egui::FontId::new(10.0, FontFamily::Name(F_UI.into())),
            C_TEXT_4,
        );

        if response.clicked() {
            self.tab = tab;
        }
    }

    // ── TOP STRIP (right of rail) ─────────────────────────────────────
    fn render_top_strip(&mut self, ui: &mut egui::Ui) {
        let frame = egui::Frame::none()
            .fill(C_BG)
            .stroke(Stroke::new(1.0_f32, C_BORDER_1))
            .inner_margin(egui::Margin {
                left: 26.0,
                right: 20.0,
                top: 20.0,
                bottom: 18.0,
            });
        frame.show(ui, |ui| {
            ui.horizontal(|ui| {
                // Big display title of the current section
                ui.vertical(|ui| {
                    ui.label(
                        RichText::new(self.tab.label().to_uppercase())
                            .family(FontFamily::Name(F_DISPLAY.into()))
                            .color(C_TEXT)
                            .size(18.0),
                    );
                    ui.label(
                        RichText::new(section_deck(self.tab))
                            .family(FontFamily::Name(F_UI.into()))
                            .color(C_TEXT_3)
                            .size(12.0),
                    );
                });

                // Right-side: refresh + updated-ago pill
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    let refresh_label = if self.busy.refresh {
                        "REFRESHING…"
                    } else {
                        "REFRESH"
                    };
                    if pill_button(ui, refresh_label, !self.busy.any(), false).clicked() {
                        self.busy.refresh = true;
                        self.push_log("manual refresh");
                        let _ = self.cmd_tx.send(WorkerCmd::Refresh);
                    }
                    ui.add_space(10.0);
                    if let Some(t) = self.last_updated {
                        let secs = t.elapsed().as_secs();
                        ui.label(
                            RichText::new(format!("upd · {}s", secs))
                                .family(FontFamily::Name(F_MONO.into()))
                                .color(C_TEXT_4)
                                .size(11.0),
                        );
                    }
                });
            });
        });
    }

    fn render_banner(&mut self, ui: &mut egui::Ui) {
        let Some(banner) = self.banner.clone() else {
            return;
        };
        let (bg, fg, border) = match banner.kind {
            BannerKind::Error => (
                Color32::from_rgb(0x1e, 0x0b, 0x0b),
                C_DANGER,
                Color32::from_rgb(0x3a, 0x16, 0x16),
            ),
            BannerKind::Warn => (
                Color32::from_rgb(0x24, 0x1a, 0x06),
                C_WARN,
                Color32::from_rgb(0x4a, 0x38, 0x0f),
            ),
            BannerKind::Info => (
                Color32::from_rgb(0x0b, 0x1c, 0x14),
                C_SUCCESS,
                Color32::from_rgb(0x16, 0x3a, 0x24),
            ),
        };
        egui::Frame::none()
            .fill(bg)
            .stroke(Stroke::new(1.0_f32, border))
            .rounding(egui::Rounding::same(10.0))
            .inner_margin(egui::Margin::symmetric(14.0, 10.0))
            .outer_margin(egui::Margin {
                left: 26.0,
                right: 20.0,
                top: 8.0,
                bottom: 0.0,
            })
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    let mark = match banner.kind {
                        BannerKind::Error => "✕",
                        BannerKind::Warn => "!",
                        BannerKind::Info => "✓",
                    };
                    ui.label(
                        RichText::new(mark)
                            .family(FontFamily::Name(F_UI_B.into()))
                            .color(fg)
                            .size(12.0),
                    );
                    ui.label(
                        RichText::new(&banner.text)
                            .family(FontFamily::Name(F_UI.into()))
                            .color(fg)
                            .size(12.0),
                    );
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui
                            .add(
                                egui::Button::new(RichText::new("×").color(fg).size(13.0))
                                    .frame(false),
                            )
                            .clicked()
                        {
                            self.clear_banner();
                        }
                    });
                });
            });
    }

    // ── TABS ──────────────────────────────────────────────────────────
    fn render_status_tab(&mut self, ui: &mut egui::Ui) {
        self.render_role_card(ui);
        ui.add_space(12.0);

        let total = ui.available_width();
        let gap = 12.0;
        if total < 620.0 {
            self.render_process_card(ui);
            ui.add_space(gap);
            self.render_claimed_card(ui);
        } else {
            let col_w = (total - gap) / 2.0;
            ui.horizontal_top(|ui| {
                ui.allocate_ui_with_layout(
                    egui::vec2(col_w, 0.0),
                    egui::Layout::top_down(egui::Align::Min),
                    |ui| {
                        ui.set_max_width(col_w);
                        self.render_process_card(ui);
                    },
                );
                ui.add_space(gap);
                ui.allocate_ui_with_layout(
                    egui::vec2(col_w, 0.0),
                    egui::Layout::top_down(egui::Align::Min),
                    |ui| {
                        ui.set_max_width(col_w);
                        self.render_claimed_card(ui);
                    },
                );
            });
        }
    }

    fn render_network_tab(&mut self, ui: &mut egui::Ui) {
        self.render_peers_card(ui);
        ui.add_space(12.0);

        let total = ui.available_width();
        let gap = 12.0;
        if total < 620.0 {
            self.render_syncthing_card(ui);
            ui.add_space(gap);
            self.render_syncthing_config_card(ui);
        } else {
            let col_w = (total - gap) / 2.0;
            ui.horizontal_top(|ui| {
                ui.allocate_ui_with_layout(
                    egui::vec2(col_w, 0.0),
                    egui::Layout::top_down(egui::Align::Min),
                    |ui| {
                        ui.set_max_width(col_w);
                        self.render_syncthing_card(ui);
                    },
                );
                ui.add_space(gap);
                ui.allocate_ui_with_layout(
                    egui::vec2(col_w, 0.0),
                    egui::Layout::top_down(egui::Align::Min),
                    |ui| {
                        ui.set_max_width(col_w);
                        self.render_syncthing_config_card(ui);
                    },
                );
            });
        }
    }

    // ── SYNC TAB (ChatBucket control plane for Syncthing) ─────────────
    fn render_sync_tab(&mut self, ui: &mut egui::Ui) {
        // Clone the snapshot pieces we render so we can mutate self freely.
        let sync: SyncSnapshot = match &self.snapshot {
            Some(s) => s.sync.clone(),
            None => SyncSnapshot::default(),
        };

        self.render_sync_connection_card(ui, &sync);
        ui.add_space(12.0);

        // Pending requests (event-driven) — shown prominently when present.
        if let Some(pending) = &sync.pending {
            if !pending.chatbucket_requests.is_empty() {
                self.render_pending_requests(ui, pending);
                ui.add_space(12.0);
            }
        }

        let total = ui.available_width();
        let gap = 12.0;
        if total < 640.0 {
            self.render_folders_card(ui, &sync);
            ui.add_space(gap);
            self.render_devices_card(ui, &sync);
        } else {
            let col_w = (total - gap) / 2.0;
            ui.horizontal_top(|ui| {
                ui.allocate_ui_with_layout(
                    egui::vec2(col_w, 0.0),
                    egui::Layout::top_down(egui::Align::Min),
                    |ui| {
                        ui.set_max_width(col_w);
                        self.render_folders_card(ui, &sync);
                    },
                );
                ui.add_space(gap);
                ui.allocate_ui_with_layout(
                    egui::vec2(col_w, 0.0),
                    egui::Layout::top_down(egui::Align::Min),
                    |ui| {
                        ui.set_max_width(col_w);
                        self.render_devices_card(ui, &sync);
                    },
                );
            });
        }
    }

    fn render_sync_connection_card(&mut self, ui: &mut egui::Ui, sync: &SyncSnapshot) {
        card(
            ui,
            "SYNCTHING",
            "ChatBucket synchronization control plane.",
            |ui| {
                // Status chips
                ui.horizontal_wrapped(|ui| {
                    status_chip(
                        ui,
                        "install",
                        sync.install.label(),
                        matches!(
                            sync.install,
                            InstallState::Running | InstallState::InstalledNotRunning
                        ),
                    );
                    status_chip(
                        ui,
                        "connection",
                        sync.connection.label(),
                        sync.connection.is_connected(),
                    );
                    if let Some(v) = &sync.syncthing_version {
                        ui.label(
                            RichText::new(v)
                                .family(FontFamily::Name(F_MONO.into()))
                                .color(C_TEXT_3)
                                .size(11.0),
                        );
                    }
                });
                ui.add_space(8.0);

                // State-specific guidance (§2/§11): never a generic "API error".
                match sync.install {
                    InstallState::NotInstalled => {
                        ui.label(
                            RichText::new("Syncthing is not installed on this machine.")
                                .color(C_WARN)
                                .size(12.0),
                        );
                        ui.label(RichText::new("Install Syncthing, then click Retry. Manager configures it for ChatBucket automatically.")
                        .color(C_TEXT_3).size(11.0));
                        if pill_button(ui, "OPEN INSTALL PAGE", true, false).clicked() {
                            let _ = open::that(crate::syncthing::SYNCTHING_INSTALL_URL);
                        }
                    }
                    InstallState::InstalledNotRunning => {
                        ui.label(
                            RichText::new("Syncthing is installed but not running.")
                                .color(C_WARN)
                                .size(12.0),
                        );
                        if pill_button(ui, "LAUNCH SYNCTHING", !self.busy.sync, false).clicked() {
                            self.busy.sync = true;
                            let _ = self.cmd_tx.send(WorkerCmd::SyncLaunch);
                        }
                    }
                    _ => {
                        if !sync.connection.is_connected() {
                            match &sync.connection {
                                ConnectionState::NotConfigured => {
                                    ui.label(RichText::new("Not connected. Manager will try to read the local Syncthing API key automatically.")
                                    .color(C_TEXT_2).size(11.5));
                                }
                                ConnectionState::AuthFailed => {
                                    ui.label(RichText::new("Syncthing rejected the API key. Re-run auto-connect, or paste a fresh key on the Network tab.")
                                    .color(C_DANGER).size(11.5));
                                }
                                ConnectionState::Unreachable(_) => {
                                    ui.label(
                                        RichText::new("Syncthing is not answering its API yet.")
                                            .color(C_WARN)
                                            .size(11.5),
                                    );
                                }
                                _ => {}
                            }
                            ui.add_space(6.0);
                        }
                        ui.horizontal_wrapped(|ui| {
                            if pill_button(ui, "AUTO-CONNECT", !self.busy.sync, false).clicked() {
                                self.busy.sync = true;
                                let _ = self.cmd_tx.send(WorkerCmd::SyncAutoConnect);
                            }
                            if pill_button(ui, "REFRESH", !self.busy.sync, false).clicked() {
                                let _ = self.cmd_tx.send(WorkerCmd::Refresh);
                                self.busy.refresh = true;
                            }
                            if sync.connection.is_connected() {
                                if pill_button(ui, "REPAIR", !self.busy.sync, false).clicked() {
                                    self.busy.sync = true;
                                    let _ = self.cmd_tx.send(WorkerCmd::SyncReconcile);
                                }
                                if pill_button(ui, "RESCAN ALL", !self.busy.sync, false).clicked() {
                                    self.busy.sync = true;
                                    let _ = self.cmd_tx.send(WorkerCmd::SyncScanAll);
                                }
                                if pill_button(ui, "RESTART SYNCTHING", !self.busy.sync, true)
                                    .clicked()
                                {
                                    self.busy.sync = true;
                                    let _ = self.cmd_tx.send(WorkerCmd::SyncRestart);
                                }
                            }
                        });
                    }
                }
                if let Some(line) = &self.sync_status_line {
                    ui.add_space(6.0);
                    ui.label(
                        RichText::new(line)
                            .family(FontFamily::Name(F_MONO.into()))
                            .color(C_TEXT_3)
                            .size(10.5),
                    );
                }
            },
        );
    }

    fn render_pending_requests(
        &mut self,
        ui: &mut egui::Ui,
        pending: &crate::syncthing::PendingSnapshot,
    ) {
        for req in &pending.chatbucket_requests {
            let names: Vec<&str> = resources::resources_for_ids(&req.folder_ids)
                .iter()
                .map(|r| r.label)
                .collect();
            let list = if names.is_empty() {
                "(device pairing)".to_string()
            } else {
                names.join(", ")
            };
            let device_id = req.device_id.clone();
            let folder_ids = req.folder_ids.clone();
            egui::Frame::none()
                .fill(C_SURFACE_3)
                .stroke(Stroke::new(1.0_f32, C_WARN))
                .rounding(egui::Rounding::same(12.0))
                .inner_margin(egui::Margin::symmetric(14.0, 12.0))
                .show(ui, |ui| {
                    ui.label(
                        RichText::new("New ChatBucket device")
                            .family(FontFamily::Name(F_UI_B.into()))
                            .color(C_WARN)
                            .size(13.0),
                    );
                    ui.label(
                        RichText::new(format!(
                            "{} wants to synchronize ChatBucket data with this machine.",
                            req.device_name
                        ))
                        .family(FontFamily::Name(F_UI.into()))
                        .color(C_TEXT)
                        .size(12.0),
                    );
                    ui.label(
                        RichText::new(format!("Folders: {list}"))
                            .family(FontFamily::Name(F_UI.into()))
                            .color(C_TEXT_2)
                            .size(11.5),
                    );
                    ui.add_space(6.0);
                    ui.horizontal(|ui| {
                        if pill_button(ui, "ACCEPT", !self.busy.sync, false).clicked() {
                            self.busy.sync = true;
                            let _ = self.cmd_tx.send(WorkerCmd::SyncAcceptPending(
                                device_id.clone(),
                                folder_ids.clone(),
                            ));
                        }
                        if pill_button(ui, "REJECT", !self.busy.sync, true).clicked() {
                            self.busy.sync = true;
                            let _ = self
                                .cmd_tx
                                .send(WorkerCmd::SyncRejectPending(device_id.clone()));
                        }
                    });
                });
            ui.add_space(8.0);
        }
    }

    fn render_folders_card(&mut self, ui: &mut egui::Ui, sync: &SyncSnapshot) {
        card(
            ui,
            "CHATBUCKET FOLDERS",
            "Per-resource sync state. managed=false is a deliberate opt-out, not a fault.",
            |ui| {
                if sync.folders.is_empty() {
                    ui.label(
                        RichText::new("No folder state yet.")
                            .color(C_TEXT_3)
                            .size(11.5),
                    );
                    return;
                }
                let folders = sync.folders.clone();
                for f in &folders {
                    self.render_folder_row(ui, f, sync.connection.is_connected());
                    ui.add_space(4.0);
                }
            },
        );
    }

    fn render_folder_row(
        &mut self,
        ui: &mut egui::Ui,
        f: &crate::manager::FolderView,
        connected: bool,
    ) {
        let (dot, color) = health_visual(&f.health);
        egui::Frame::none()
            .fill(C_SURFACE_3)
            .stroke(Stroke::new(1.0_f32, C_BORDER_1))
            .rounding(egui::Rounding::same(10.0))
            .inner_margin(egui::Margin::symmetric(12.0, 9.0))
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(dot).color(color).size(12.0));
                    ui.label(
                        RichText::new(f.label)
                            .family(FontFamily::Name(F_UI_B.into()))
                            .color(C_TEXT)
                            .size(12.5),
                    );
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        // managed toggle (§16.5)
                        let mut m = f.managed;
                        if ui
                            .add_enabled(!self.busy.sync, egui::Checkbox::new(&mut m, ""))
                            .changed()
                        {
                            self.busy.sync = true;
                            let _ = self
                                .cmd_tx
                                .send(WorkerCmd::SyncSetManaged(f.folder_id.clone(), m));
                        }
                        ui.label(RichText::new(f.health.label()).color(color).size(11.0));
                    });
                });
                // Detail line
                if f.managed && connected {
                    let mut parts: Vec<String> = Vec::new();
                    if f.need_files > 0 {
                        parts.push(format!("{} file(s) left", f.need_files));
                    }
                    if f.need_bytes > 0 {
                        parts.push(format_bytes(f.need_bytes));
                    }
                    if f.error_count > 0 {
                        parts.push(format!("{} error(s)", f.error_count));
                    }
                    if !f.last_scan.is_empty() {
                        parts.push(format!("scan {}", short_time(&f.last_scan)));
                    }
                    if !parts.is_empty() {
                        ui.label(
                            RichText::new(parts.join(" · "))
                                .family(FontFamily::Name(F_MONO.into()))
                                .color(C_TEXT_3)
                                .size(10.5),
                        );
                    }
                    ui.horizontal(|ui| {
                        if mini_button(ui, "Rescan", !self.busy.sync) {
                            self.busy.sync = true;
                            let _ = self.cmd_tx.send(WorkerCmd::SyncScan(f.folder_id.clone()));
                        }
                        if mini_button(ui, "Pause", !self.busy.sync) {
                            self.busy.sync = true;
                            let _ = self.cmd_tx.send(WorkerCmd::SyncPause(f.folder_id.clone()));
                        }
                        if mini_button(ui, "Resume", !self.busy.sync) {
                            self.busy.sync = true;
                            let _ = self.cmd_tx.send(WorkerCmd::SyncResume(f.folder_id.clone()));
                        }
                    });
                }
            });
    }

    fn render_devices_card(&mut self, ui: &mut egui::Ui, sync: &SyncSnapshot) {
        card(ui, "DEVICES", "Remote machines sharing ChatBucket data. Add by Syncthing device ID — Manager associates all managed folders.", |ui| {
            // Add-device flow (§16/§21)
            ui.horizontal(|ui| {
                ui.add(egui::TextEdit::singleline(&mut self.sync_new_device_id)
                    .hint_text("Device ID")
                    .desired_width(280.0));
                if pill_button(ui, "ADD", !self.busy.sync && !self.sync_new_device_id.trim().is_empty(), false).clicked() {
                    let id = self.sync_new_device_id.clone();
                    self.busy.sync = true;
                    let _ = self.cmd_tx.send(WorkerCmd::SyncAddDevice(id));
                    self.sync_new_device_id.clear();
                }
            });
            ui.add_space(8.0);

            if sync.devices.is_empty() {
                ui.label(RichText::new("No devices sharing ChatBucket data yet.").color(C_TEXT_3).size(11.5));
            } else {
                for d in &sync.devices {
                    let id = d.device_id.clone();
                    egui::Frame::none()
                        .fill(C_SURFACE_3)
                        .stroke(Stroke::new(1.0_f32, C_BORDER_1))
                        .rounding(egui::Rounding::same(10.0))
                        .inner_margin(egui::Margin::symmetric(12.0, 8.0))
                        .show(ui, |ui| {
                            ui.horizontal(|ui| {
                                let dot = if d.connected { "●" } else { "○" };
                                let c = if d.connected { C_SUCCESS } else { C_TEXT_4 };
                                ui.label(RichText::new(dot).color(c).size(12.0));
                                ui.label(RichText::new(&d.name).family(FontFamily::Name(F_MONO.into()))
                                    .color(C_TEXT).size(12.0));
                                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                    if mini_button(ui, "Remove", !self.busy.sync) {
                                        self.busy.sync = true;
                                        let _ = self.cmd_tx.send(WorkerCmd::SyncRemoveDevice(id.clone()));
                                    }
                                    if !d.fully_associated {
                                        ui.label(RichText::new("partial").color(C_WARN).size(10.5));
                                    }
                                    ui.label(RichText::new(if d.connected {"connected"} else {"offline"})
                                        .color(c).size(11.0));
                                });
                            });
                        });
                    ui.add_space(4.0);
                }
            }

            // State-conflict warning (§15)
            if sync.state_conflict_count > 0 {
                ui.add_space(6.0);
                ui.label(RichText::new(format!("⚠ {} arbitration conflict file(s) detected under state/ — review before restarting.", sync.state_conflict_count))
                    .color(C_WARN).size(11.0));
            }

            ui.add_space(4.0);
            if pill_button(ui, "CLEAR SYNC ERRORS", !self.busy.sync && sync.connection.is_connected(), true).clicked() {
                self.busy.sync = true;
                let _ = self.cmd_tx.send(WorkerCmd::SyncClearErrors);
            }
        });
    }

    fn render_updates_tab(&mut self, ui: &mut egui::Ui) {
        self.render_version_card(ui);
    }

    fn render_logs_tab(&mut self, ui: &mut egui::Ui) {
        card(
            ui,
            "EVENT LOG",
            "Every user action, worker refresh, and state transition, newest first.",
            |ui| {
                ui.horizontal(|ui| {
                    if pill_button(ui, "COPY", !self.logs.is_empty(), false).clicked() {
                        let joined = self
                            .logs
                            .iter()
                            .map(|s| s.as_str())
                            .collect::<Vec<_>>()
                            .join("\n");
                        ui.output_mut(|o| o.copied_text = joined);
                        self.push_log("copied log to clipboard");
                    }
                    if pill_button(ui, "CLEAR", !self.logs.is_empty(), true).clicked() {
                        self.logs.clear();
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.label(
                            RichText::new(format!("{} lines · cap {}", self.logs.len(), LOG_CAP))
                                .family(FontFamily::Name(F_MONO.into()))
                                .color(C_TEXT_4)
                                .size(11.0),
                        );
                    });
                });
                ui.add_space(10.0);
                let mut text = String::new();
                for line in self.logs.iter().rev() {
                    text.push_str(line);
                    text.push('\n');
                }
                egui::Frame::none()
                    .fill(C_SURFACE_3)
                    .stroke(Stroke::new(1.0_f32, C_BORDER_1))
                    .rounding(egui::Rounding::same(10.0))
                    .inner_margin(egui::Margin::symmetric(14.0, 12.0))
                    .show(ui, |ui| {
                        egui::ScrollArea::vertical()
                            .max_height(440.0)
                            .auto_shrink([false, false])
                            .show(ui, |ui| {
                                ui.add(
                                    egui::Label::new(
                                        RichText::new(text)
                                            .family(FontFamily::Name(F_MONO.into()))
                                            .color(C_TEXT_2)
                                            .size(11.5),
                                    )
                                    .wrap(true),
                                );
                            });
                    });
            },
        );
    }

    // ── CARDS ─────────────────────────────────────────────────────────
    fn render_role_card(&self, ui: &mut egui::Ui) {
        card(
            ui,
            "ROLE",
            "What this ChatBucket instance is currently doing on the tailnet.",
            |ui| {
                let (label, detail, color, live) =
                    match self.snapshot.as_ref().map(|s| s.role.clone()) {
                        Some(r) => {
                            let live = matches!(
                                r.state,
                                RoleState::Host | RoleState::Client | RoleState::Starting
                            );
                            (
                                r.state.label().to_string(),
                                r.detail,
                                role_color(&r.state),
                                live,
                            )
                        }
                        None => ("—".to_string(), "loading…".into(), C_TEXT_3, false),
                    };

                ui.horizontal(|ui| {
                    pulse_dot(ui, color, live, 12.0);
                    ui.add_space(10.0);
                    ui.label(
                        RichText::new(label.to_uppercase())
                            .family(FontFamily::Name(F_DISPLAY.into()))
                            .color(color)
                            .size(28.0),
                    );
                });
                ui.add_space(4.0);
                ui.label(
                    RichText::new(detail)
                        .family(FontFamily::Name(F_UI.into()))
                        .color(C_TEXT_2)
                        .size(12.5),
                );

                // Explainer strip so users aren't confused by the badge terms.
                ui.add_space(10.0);
                ui.separator();
                ui.add_space(6.0);
                ui.horizontal_wrapped(|ui| {
                    legend_pill(ui, "HOST", C_SUCCESS, "hosts on this machine");
                    legend_pill(ui, "CLIENT", C_TEXT, "connected to a peer host");
                    legend_pill(ui, "STARTING", C_TEXT_2, "arbitrating (≤45 s)");
                    legend_pill(ui, "STALE", C_WARN, "claim > 45 s old");
                    legend_pill(ui, "CONFLICT", C_DANGER, "two hosts claim it");
                    legend_pill(ui, "IDLE", C_TEXT_3, "no process running");
                });
            },
        );
    }

    fn render_process_card(&mut self, ui: &mut egui::Ui) {
        card(
            ui,
            "PROCESS",
            "Lifecycle of the ChatBucket server / doorman process group.",
            |ui| {
                let running = self.snapshot.as_ref().and_then(|s| s.process.clone());
                match &running {
                    Some(p) => {
                        ui.horizontal(|ui| {
                            pulse_dot(ui, C_SUCCESS, true, 10.0);
                            ui.add_space(8.0);
                            ui.label(
                                RichText::new("RUNNING")
                                    .family(FontFamily::Name(F_UI_B.into()))
                                    .color(C_TEXT)
                                    .size(13.0),
                            );
                        });
                        ui.add_space(6.0);
                        kv_row(ui, "pid", &format!("{}", p.pid));
                        kv_row(ui, "role", p.role.as_str());
                    }
                    None => {
                        ui.horizontal(|ui| {
                            pulse_dot(ui, C_TEXT_3, false, 10.0);
                            ui.add_space(8.0);
                            ui.label(
                                RichText::new("STOPPED")
                                    .family(FontFamily::Name(F_UI_B.into()))
                                    .color(C_TEXT_2)
                                    .size(13.0),
                            );
                        });
                        ui.add_space(6.0);
                        kv_row(ui, "pid", "—");
                        kv_row(ui, "role", "—");
                    }
                }

                ui.add_space(14.0);
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
                        "STARTING…"
                    } else {
                        "START"
                    };
                    let stop_label = if self.busy.stop {
                        "STOPPING…"
                    } else {
                        "STOP"
                    };

                    if pill_button(ui, start_label, can_start, false).clicked() {
                        self.clear_banner();
                        self.busy.start = true;
                        self.push_log("start requested");
                        let _ = self.cmd_tx.send(WorkerCmd::Start);
                    }
                    if pill_button(ui, stop_label, can_stop, true).clicked() {
                        self.clear_banner();
                        self.busy.stop = true;
                        self.push_log("stop requested");
                        let _ = self.cmd_tx.send(WorkerCmd::Stop);
                    }
                });

                ui.add_space(6.0);
                ui.label(
                    RichText::new(
                        "START blocks briefly until arbitration resolves. STOP signals the whole \
                 process group and waits for graceful exit before force-killing.",
                    )
                    .family(FontFamily::Name(F_UI.into()))
                    .color(C_TEXT_4)
                    .size(10.5),
                );
            },
        );
    }

    fn render_claimed_card(&self, ui: &mut egui::Ui) {
        card(ui, "CLAIMED HOST", "The machine currently claiming host-of-record in host-state.json, and whether it's reachable.", |ui| {
            let Some(snap) = &self.snapshot else {
                dot_line(ui, C_TEXT_3, "—", "loading…", false);
                return;
            };
            match (&snap.claimed_machine, &snap.claimed_reachability) {
                (None, _) =>
                    dot_line(ui, C_TEXT_3, "—", "no claim on record", false),
                (Some(name), Some(ClaimedReachability::IsSelf)) =>
                    dot_line(ui, C_ACCENT, name, "this machine", true),
                (Some(name), Some(ClaimedReachability::Online)) =>
                    dot_line(ui, C_SUCCESS, name, "reachable · /health OK", true),
                (Some(name), Some(ClaimedReachability::Offline)) =>
                    dot_line(ui, C_TEXT_3, name, "not reachable", false),
                (Some(name), Some(ClaimedReachability::Error(e))) =>
                    dot_line(ui, C_DANGER, name, e, false),
                (Some(name), None) =>
                    dot_line(ui, C_TEXT_3, name, "checking…", false),
            }
        });
    }

    fn render_peers_card(&self, ui: &mut egui::Ui) {
        card(ui, "TAILNET PEERS", "Every real device on this tailnet. Tailscale infrastructure without a DNSName is counted but hidden.", |ui| {
            let Some(snap) = &self.snapshot else {
                dot_line(ui, C_TEXT_3, "—", "loading…", false); return;
            };
            match &snap.tailnet_peers {
                Err(e) => dot_line(ui, C_DANGER, "—", &format!("error: {e}"), false),
                Ok(PeerList { peers, hidden_count }) => {
                    if peers.is_empty() {
                        dot_line(ui, C_TEXT_3, "—", "no peers found", false);
                    } else {
                        let mut sorted: Vec<&PeerInfo> = peers.iter().collect();
                        sorted.sort_by(|a, b| a.name.cmp(&b.name));
                        for (i, p) in sorted.iter().enumerate() {
                            let (col, state) = if p.online { (C_SUCCESS, "online") } else { (C_TEXT_3, "offline") };
                            dot_line(ui, col, &p.name, state, p.online);
                            if i < sorted.len() - 1 {
                                ui.add_space(2.0);
                                thin_divider(ui);
                                ui.add_space(2.0);
                            }
                        }
                    }
                    if *hidden_count > 0 {
                        ui.add_space(8.0);
                        thin_divider(ui);
                        ui.add_space(6.0);
                        ui.label(RichText::new(format!(
                            "+{} infrastructure peer(s) hidden — no DNSName, likely Tailscale Funnel",
                            hidden_count))
                            .family(FontFamily::Name(F_UI.into()))
                            .color(C_TEXT_4).size(10.5));
                    }
                }
            }
        });
    }

    fn render_syncthing_card(&self, ui: &mut egui::Ui) {
        card(
            ui,
            "SYNCTHING",
            "State of the shared 'sync-state' folder that Syncthing replicates between hosts.",
            |ui| {
                let Some(snap) = &self.snapshot else {
                    dot_line(ui, C_TEXT_3, "sync-state", "loading…", false);
                    return;
                };
                let col = syncthing_color(&snap.syncthing);
                let live = matches!(
                    snap.syncthing,
                    SyncthingState::InSync | SyncthingState::Syncing
                );
                let label = snap.syncthing.short_label();
                dot_line(ui, col, "sync-state", &label, live);

                let detail = snap.syncthing.detail();
                if !detail.is_empty()
                    && !matches!(
                        &snap.syncthing,
                        SyncthingState::InSync | SyncthingState::Syncing
                    )
                {
                    ui.add_space(6.0);
                    ui.label(
                        RichText::new(detail)
                            .family(FontFamily::Name(F_UI.into()))
                            .color(C_TEXT_3)
                            .size(11.5),
                    );
                }

                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    let link_txt = RichText::new("OPEN SYNCTHING GUI  →")
                        .family(FontFamily::Name(F_UI_B.into()))
                        .color(C_ACCENT)
                        .size(11.0);
                    if ui.link(link_txt).clicked() {
                        let url = if self.cfg_url.trim().is_empty() {
                            "http://127.0.0.1:8384".to_string()
                        } else {
                            self.cfg_url.trim().to_string()
                        };
                        let _ = open::that_detached(&url);
                    }
                });
            },
        );
    }

    fn render_syncthing_config_card(&mut self, ui: &mut egui::Ui) {
        card(ui, "SYNCTHING CONFIG", "API key + URL the manager uses to talk to Syncthing's REST API. Saved to manager_config.json.", |ui| {
            if let Some(err) = self.cfg_parse_err.clone() {
                ui.label(RichText::new(format!(
                    "manager_config.json is malformed: {err}. Saving overwrites it with a clean copy."))
                    .family(FontFamily::Name(F_UI.into()))
                    .color(C_DANGER).size(11.0));
                ui.add_space(6.0);
            }

            // API key
            field_label(ui, "API KEY");
            ui.horizontal(|ui| {
                let mut edit = egui::TextEdit::singleline(&mut self.cfg_api_key)
                    .hint_text("paste from Syncthing → Actions → Settings → GUI")
                    .desired_width(f32::INFINITY)
                    .font(egui::FontId::new(12.5, FontFamily::Name(F_MONO.into())));
                if !self.cfg_show_key { edit = edit.password(true); }
                let response = ui.add(edit);
                if response.changed() { self.cfg_dirty = self.detect_dirty(); }
                let toggle_label = if self.cfg_show_key { "HIDE" } else { "SHOW" };
                if pill_button(ui, toggle_label, true, false).clicked() {
                    self.cfg_show_key = !self.cfg_show_key;
                }
            });

            // URL
            ui.add_space(10.0);
            field_label(ui, "URL  ·  optional, defaults to http://127.0.0.1:8384");
            let url_edit = egui::TextEdit::singleline(&mut self.cfg_url)
                .hint_text("http://127.0.0.1:8384")
                .desired_width(f32::INFINITY)
                .font(egui::FontId::new(12.5, FontFamily::Name(F_MONO.into())));
            if ui.add(url_edit).changed() { self.cfg_dirty = self.detect_dirty(); }

            // Buttons
            ui.add_space(14.0);
            ui.horizontal(|ui| {
                let can_save = self.cfg_dirty && !self.busy.any_lifecycle() && !self.busy.save_config;
                let can_test = !self.cfg_api_key.trim().is_empty()
                    && !self.busy.test_syncthing && !self.busy.any_lifecycle();
                let can_reset = self.cfg_dirty && !self.busy.save_config;
                let save_label = if self.busy.save_config    { "SAVING…"  } else { "SAVE"            };
                let test_label = if self.busy.test_syncthing { "TESTING…" } else { "TEST CONNECTION" };

                if pill_button(ui, save_label, can_save, false).clicked() {
                    self.clear_banner(); self.busy.save_config = true;
                    self.push_log("saving syncthing config");
                    // Preserve the on-disk managed state + key source; only
                    // the key/URL fields come from the input boxes.
                    let (mut disk, _) = self.ctx.read_config();
                    disk.syncthing_api_key = if self.cfg_api_key.trim().is_empty() { None }
                                             else { Some(self.cfg_api_key.clone()) };
                    disk.syncthing_url     = if self.cfg_url.trim().is_empty() { None }
                                             else { Some(self.cfg_url.clone()) };
                    disk.api_key_source    = Some("manual".into());
                    let _ = self.cmd_tx.send(WorkerCmd::SaveConfig(disk));
                }
                if pill_button(ui, test_label, can_test, false).clicked() {
                    self.clear_banner(); self.busy.test_syncthing = true;
                    self.push_log("testing syncthing connection");
                    let _ = self.cmd_tx.send(WorkerCmd::TestSyncthing);
                }
                if pill_button(ui, "RESET", can_reset, false).clicked() {
                    self.cfg_api_key = self.cfg_disk_snapshot.0.clone();
                    self.cfg_url     = self.cfg_disk_snapshot.1.clone();
                    self.cfg_dirty   = false;
                    self.cfg_status_line = Some("Reverted to on-disk config.".into());
                    self.push_log("config editor reset to on-disk values");
                }
            });

            if let Some(line) = self.cfg_status_line.clone() {
                ui.add_space(8.0);
                ui.label(RichText::new(line)
                    .family(FontFamily::Name(F_UI.into()))
                    .color(C_TEXT_2).size(11.0));
            }
        });
    }

    fn render_version_card(&mut self, ui: &mut egui::Ui) {
        card(ui, "RELEASE CHANNEL", "Compare the installed VERSION file with the latest GitHub release. Install only replaces code — data is preserved.", |ui| {
            let installed = self.snapshot.as_ref().and_then(|s| s.version.clone())
                .unwrap_or_else(|| "(missing)".to_string());
            let latest = self.latest_tag.clone().unwrap_or_else(|| "not checked".to_string());

            ui.horizontal(|ui| {
                version_column(ui, "INSTALLED", &installed, C_TEXT);
                ui.add_space(24.0);
                version_column(ui, "LATEST", &latest, if self.can_install { C_ACCENT } else { C_TEXT_2 });
            });

            if let Some(url) = self.latest_url.clone() {
                ui.add_space(8.0);
                if ui.link(RichText::new("VIEW RELEASE ON GITHUB  →")
                    .family(FontFamily::Name(F_UI_B.into()))
                    .color(C_ACCENT).size(11.0)).clicked() { let _ = open::that_detached(&url); }
            }

            if let Some(detail) = self.update_detail.clone() {
                ui.add_space(10.0);
                thin_divider(ui);
                ui.add_space(8.0);
                ui.label(RichText::new(detail)
                    .family(FontFamily::Name(F_UI.into()))
                    .color(C_TEXT_2).size(12.0));
            }

            ui.add_space(14.0);
            ui.horizontal(|ui| {
                let can_check   = !self.busy.any();
                let can_install = self.can_install && !self.busy.any();
                let check_label   = if self.busy.check_update   { "CHECKING…"   } else { "CHECK FOR UPDATES" };
                let install_label = if self.busy.install_update { "INSTALLING…" } else { "INSTALL UPDATE" };

                if pill_button(ui, check_label, can_check, false).clicked() {
                    self.clear_banner(); self.busy.check_update = true;
                    self.push_log("checking for updates");
                    let _ = self.cmd_tx.send(WorkerCmd::CheckUpdate);
                }
                if self.can_install
                    && pill_button(ui, install_label, can_install, false).clicked() {
                    self.clear_banner(); self.busy.install_update = true;
                    self.push_log("installing update");
                    let _ = self.cmd_tx.send(WorkerCmd::InstallUpdate);
                }
            });
        });
    }
}

impl Drop for ManagerApp {
    fn drop(&mut self) {
        let _ = self.cmd_tx.send(WorkerCmd::Shutdown);
    }
}

// ── Worker thread (unchanged behaviour) ───────────────────────────────
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
                        WorkerCmd::CheckUpdate  => { let r = ctx.check_for_updates(); let _ = msg_tx.send(WorkerMsg::UpdateCheck(r)); }
                        WorkerCmd::InstallUpdate=> { let r = ctx.install_update();     let _ = msg_tx.send(WorkerMsg::UpdateInstall(r)); }
                        WorkerCmd::SaveConfig(cfg) => { let r = ctx.save_config(cfg);  let _ = msg_tx.send(WorkerMsg::ConfigSaved(r)); }
                        WorkerCmd::TestSyncthing   => { let r = ctx.test_syncthing();  let _ = msg_tx.send(WorkerMsg::ConfigTested(r)); }
                        // ── Syncthing control plane ──────────────────
                        WorkerCmd::SyncAutoConnect => { let m = ctx.sync_auto_connect(); let _ = msg_tx.send(WorkerMsg::SyncDone(m)); }
                        WorkerCmd::SyncLaunch      => { let m = ctx.sync_launch_syncthing(); let _ = msg_tx.send(WorkerMsg::SyncDone(m)); }
                        WorkerCmd::SyncReconcile   => {
                            let m = match ctx.sync_reconcile() {
                                Ok(rep) => format!("Repair complete: {} ensured, {} disabled (untouched), {} conflict(s).",
                                    rep.ensured.len(), rep.skipped_disabled.len(), rep.conflicts.len()),
                                Err(e) => format!("Repair failed: {e}"),
                            };
                            let _ = msg_tx.send(WorkerMsg::SyncDone(m));
                        }
                        WorkerCmd::SyncScan(fid)  => { let m = res_msg(ctx.sync_scan_folder(&fid), "rescan queued"); let _ = msg_tx.send(WorkerMsg::SyncDone(m)); }
                        WorkerCmd::SyncScanAll    => { let m = match ctx.sync_scan_all() { Ok(n) => format!("Rescan queued for {n} ChatBucket folder(s)."), Err(e) => format!("Rescan failed: {e}") }; let _ = msg_tx.send(WorkerMsg::SyncDone(m)); }
                        WorkerCmd::SyncPause(fid)  => { let m = res_msg(ctx.sync_pause_folder(&fid), "paused"); let _ = msg_tx.send(WorkerMsg::SyncDone(m)); }
                        WorkerCmd::SyncResume(fid) => { let m = res_msg(ctx.sync_resume_folder(&fid), "resumed"); let _ = msg_tx.send(WorkerMsg::SyncDone(m)); }
                        WorkerCmd::SyncRestart     => { let m = res_msg(ctx.sync_restart(), "syncthing restarting"); let _ = msg_tx.send(WorkerMsg::SyncDone(m)); }
                        WorkerCmd::SyncClearErrors => { let m = res_msg(ctx.sync_clear_errors(), "errors cleared"); let _ = msg_tx.send(WorkerMsg::SyncDone(m)); }
                        WorkerCmd::SyncSetManaged(fid, m) => {
                            let msg = match ctx.sync_set_managed(&fid, m) {
                                Ok(ManagedTransition::Enabled) => format!("{fid} enabled — folder recreated and reconciled."),
                                Ok(ManagedTransition::Disabled) => format!("{fid} disabled — Syncthing config removed, local files kept."),
                                Ok(ManagedTransition::EnableConflict { existing, expected }) => format!("CONFLICT enabling {fid}: existing {existing} vs expected {expected}. Not repointed."),
                                Err(e) => format!("managed toggle failed: {e}"),
                            };
                            let _ = msg_tx.send(WorkerMsg::SyncDone(msg));
                        }
                        WorkerCmd::SyncAddDevice(id) => {
                            let msg = match ctx.sync_add_device(&id) {
                                Ok(DeviceAddOutcome::Added { associated, .. }) => format!("Device added and associated with {} folder(s).", associated.len()),
                                Ok(DeviceAddOutcome::InvalidId(m)) => format!("Invalid device ID: {m}"),
                                Err(e) => format!("Add device failed: {e}"),
                            };
                            let _ = msg_tx.send(WorkerMsg::SyncDone(msg));
                        }
                        WorkerCmd::SyncRemoveDevice(id) => { let m = res_msg(ctx.sync_remove_device(&id), "device removed from ChatBucket"); let _ = msg_tx.send(WorkerMsg::SyncDone(m)); }
                        WorkerCmd::SyncAcceptPending(id, fids) => {
                            let msg = match ctx.sync_accept_pending(&id, &fids) {
                                Ok(DeviceAddOutcome::Added { associated, .. }) => format!("Accepted — mapped {} ChatBucket folder(s) to local paths.", associated.len()),
                                Ok(DeviceAddOutcome::InvalidId(m)) => format!("Invalid: {m}"),
                                Err(e) => format!("Accept failed: {e}"),
                            };
                            let _ = msg_tx.send(WorkerMsg::SyncDone(msg));
                        }
                        WorkerCmd::SyncRejectPending(id) => { let m = res_msg(ctx.sync_reject_pending(&id), "request rejected"); let _ = msg_tx.send(WorkerMsg::SyncDone(m)); }
                    }
                    if let Some(c) = egui_ctx.lock().unwrap().as_ref() { c.request_repaint(); }
                }

                let due = last_refresh.map(|t| t.elapsed() >= REFRESH_NORMAL).unwrap_or(true);
                if due {
                    let snap = ctx.get_status();
                    let fast = matches!(snap.role.state, RoleState::Starting);
                    let _ = msg_tx.send(WorkerMsg::Snapshot(snap, Instant::now()));
                    last_refresh = Some(Instant::now());
                    if let Some(c) = egui_ctx.lock().unwrap().as_ref() { c.request_repaint(); }
                    if fast { std::thread::sleep(REFRESH_FAST); continue; }
                }
                if !got_cmd { std::thread::sleep(Duration::from_millis(200)); }
            }
        })
        .expect("spawn worker thread");
}

// ── Pending-request event watcher (§17/§24) ───────────────────────────
//
// Long-polls Syncthing's event stream for PendingDevicesChanged /
// PendingFoldersChanged. The event is ONLY the trigger; on receipt we ask
// the worker for a refresh, which re-reads the authoritative pending state.
// This is deliberately NOT a sleep-and-count window — events drive detection.
fn spawn_pending_watcher(
    ctx: Arc<ManagerContext>,
    msg_tx: Sender<WorkerMsg>,
    egui_ctx: Arc<Mutex<Option<egui::Context>>>,
) {
    std::thread::Builder::new()
        .name("cb-sync-events".into())
        .spawn(move || {
            let mut since: u64 = 0;
            loop {
                // Only poll events when connected; otherwise back off.
                match ctx.sync_poll_pending_events(since) {
                    Ok((relevant, next)) => {
                        since = next;
                        if relevant {
                            let _ = msg_tx.send(WorkerMsg::PendingChanged);
                            if let Some(c) = egui_ctx.lock().unwrap().as_ref() {
                                c.request_repaint();
                            }
                        }
                    }
                    Err(_) => {
                        // Not configured / unreachable / auth — wait and retry.
                        std::thread::sleep(Duration::from_secs(10));
                    }
                }
            }
        })
        .expect("spawn pending-event watcher");
}

// ── FONTS ─────────────────────────────────────────────────────────────
fn install_fonts(ctx: &egui::Context) {
    let mut fonts = FontDefinitions::default();

    fonts.font_data.insert(
        F_DISPLAY.into(),
        FontData::from_static(include_bytes!("assets/fonts/Michroma-Regular.ttf")),
    );
    fonts.font_data.insert(
        F_UI.into(),
        FontData::from_static(include_bytes!("assets/fonts/IBMPlexSans-Regular.ttf")),
    );
    fonts.font_data.insert(
        F_UI_B.into(),
        FontData::from_static(include_bytes!("assets/fonts/IBMPlexSans-SemiBold.ttf")),
    );
    fonts.font_data.insert(
        F_MONO.into(),
        FontData::from_static(include_bytes!("assets/fonts/IBMPlexMono-Regular.ttf")),
    );

    fonts
        .families
        .insert(FontFamily::Name(F_DISPLAY.into()), vec![F_DISPLAY.into()]);
    fonts
        .families
        .insert(FontFamily::Name(F_UI.into()), vec![F_UI.into()]);
    fonts
        .families
        .insert(FontFamily::Name(F_UI_B.into()), vec![F_UI_B.into()]);
    fonts
        .families
        .insert(FontFamily::Name(F_MONO.into()), vec![F_MONO.into()]);

    // Also make Plex the default proportional family and Plex Mono the default mono.
    fonts
        .families
        .entry(FontFamily::Proportional)
        .or_default()
        .insert(0, F_UI.into());
    fonts
        .families
        .entry(FontFamily::Monospace)
        .or_default()
        .insert(0, F_MONO.into());

    ctx.set_fonts(fonts);
}

// ── STYLE ─────────────────────────────────────────────────────────────
fn apply_dark_style(ctx: &egui::Context) {
    let mut style = (*ctx.style()).clone();
    style.visuals.window_fill = C_BG;
    style.visuals.panel_fill = C_BG;
    style.visuals.extreme_bg_color = C_SURFACE_3;
    style.visuals.override_text_color = Some(C_TEXT);
    style.visuals.widgets.noninteractive.bg_fill = C_SURFACE_2;
    style.visuals.widgets.inactive.bg_fill = C_SURFACE_4;
    style.visuals.widgets.hovered.bg_fill = C_SURFACE_HI;
    style.visuals.widgets.active.bg_fill = Color32::from_rgb(0x33, 0x33, 0x37);
    style.visuals.widgets.hovered.bg_stroke = Stroke::new(1.0_f32, C_ACCENT);
    style.visuals.selection.bg_fill =
        Color32::from_rgba_unmultiplied(C_ACCENT.r(), C_ACCENT.g(), C_ACCENT.b(), 55);
    style.visuals.selection.stroke = Stroke::new(1.0_f32, C_ACCENT);
    style.spacing.item_spacing = egui::vec2(10.0, 10.0);
    style.spacing.button_padding = egui::vec2(14.0, 8.0);
    ctx.set_style(style);
}

// ── PAINTERS / SMALL HELPERS ──────────────────────────────────────────

fn status_chip(ui: &mut egui::Ui, key: &str, value: &str, ok: bool) {
    let color = if ok { C_SUCCESS } else { C_WARN };
    egui::Frame::none()
        .fill(C_SURFACE_3)
        .stroke(Stroke::new(1.0_f32, C_BORDER_1))
        .rounding(egui::Rounding::same(8.0))
        .inner_margin(egui::Margin::symmetric(8.0, 4.0))
        .show(ui, |ui| {
            ui.label(
                RichText::new(format!("{key}: "))
                    .family(FontFamily::Name(F_MONO.into()))
                    .color(C_TEXT_4)
                    .size(10.5),
            );
            ui.label(
                RichText::new(value)
                    .family(FontFamily::Name(F_UI_B.into()))
                    .color(color)
                    .size(11.0),
            );
        });
}

fn health_visual(h: &FolderHealth) -> (&'static str, Color32) {
    match h {
        FolderHealth::InSync => ("●", C_SUCCESS),
        FolderHealth::Syncing { .. } => ("●", C_ACCENT),
        FolderHealth::Missing | FolderHealth::ConfigMismatch => ("▲", C_WARN),
        FolderHealth::AuthFailed | FolderHealth::SyncError | FolderHealth::Conflict => {
            ("●", C_DANGER)
        }
        FolderHealth::Unreachable => ("●", C_DANGER),
        FolderHealth::Disabled => ("○", C_TEXT_4),
        FolderHealth::Unknown => ("○", C_TEXT_3),
    }
}

fn mini_button(ui: &mut egui::Ui, label: &str, enabled: bool) -> bool {
    ui.add_enabled(
        enabled,
        egui::Button::new(
            RichText::new(label)
                .family(FontFamily::Name(F_UI.into()))
                .size(10.5)
                .color(C_TEXT_2),
        )
        .rounding(egui::Rounding::same(6.0)),
    )
    .clicked()
}

fn format_bytes(b: i64) -> String {
    const UNITS: [&str; 5] = ["B", "KB", "MB", "GB", "TB"];
    let mut v = b as f64;
    let mut i = 0;
    while v >= 1024.0 && i < UNITS.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    format!("{:.1} {} left", v, UNITS[i])
}

fn short_time(ts: &str) -> String {
    // RFC3339 → "YYYY-MM-DD HH:MM" best effort
    let t = ts.trim();
    if t.len() >= 16 {
        let mut s = t[..16].to_string();
        s = s.replace('T', " ");
        s
    } else {
        t.to_string()
    }
}

fn card(ui: &mut egui::Ui, label: &str, deck: &str, body: impl FnOnce(&mut egui::Ui)) {
    // Hover-lift animation on the card body itself
    let id = ui.id().with(("card", label));
    let (rect_probe, hover_resp) =
        ui.allocate_exact_size(egui::vec2(ui.available_width(), 0.0), egui::Sense::hover());
    let _ = rect_probe;
    let hovered = hover_resp.hovered();
    let anim = ui.ctx().animate_bool_with_time(id, hovered, 0.15);

    let border = lerp_color(C_BORDER_1, C_BORDER_2, anim);
    egui::Frame::none()
        .fill(C_SURFACE_2)
        .stroke(Stroke::new(1.0_f32, border))
        .rounding(egui::Rounding::same(14.0))
        .inner_margin(egui::Margin::symmetric(18.0, 16.0))
        .shadow(egui::epaint::Shadow {
            offset: egui::vec2(0.0, 2.0),
            blur: 6.0 * anim,
            spread: 0.0,
            color: Color32::from_rgba_unmultiplied(0, 0, 0, (110.0 * anim) as u8),
        })
        .show(ui, |ui| {
            // Eyebrow (label) + subtitle deck
            ui.horizontal(|ui| {
                let (mark_rect, _) =
                    ui.allocate_exact_size(egui::vec2(4.0, 10.0), egui::Sense::hover());
                ui.painter()
                    .rect_filled(mark_rect, egui::Rounding::same(1.0), C_ACCENT);
                ui.add_space(6.0);
                ui.label(
                    RichText::new(label)
                        .family(FontFamily::Name(F_DISPLAY.into()))
                        .color(C_TEXT_2)
                        .size(10.5),
                );
            });
            if !deck.is_empty() {
                ui.add_space(4.0);
                ui.label(
                    RichText::new(deck)
                        .family(FontFamily::Name(F_UI.into()))
                        .color(C_TEXT_4)
                        .size(11.0),
                );
            }
            ui.add_space(12.0);
            body(ui);
        });
}

/// Two equal-width columns side by side (falls back to stacked on narrow widths).
#[allow(dead_code)] // layout helper retained from v0.2; superseded by the
                    // explicit two-column blocks in render_*_tab but kept for future cards.
fn two_col(ui: &mut egui::Ui, left: impl FnOnce(&mut egui::Ui), right: impl FnOnce(&mut egui::Ui)) {
    let total = ui.available_width();
    let gap = 12.0;
    if total < 620.0 {
        left(ui);
        ui.add_space(gap);
        right(ui);
        return;
    }
    let col_w = (total - gap) / 2.0;
    ui.horizontal_top(|ui| {
        ui.allocate_ui_with_layout(
            egui::vec2(col_w, 0.0),
            egui::Layout::top_down(egui::Align::Min),
            |ui| {
                ui.set_max_width(col_w);
                left(ui);
            },
        );
        ui.add_space(gap);
        ui.allocate_ui_with_layout(
            egui::vec2(col_w, 0.0),
            egui::Layout::top_down(egui::Align::Min),
            |ui| {
                ui.set_max_width(col_w);
                right(ui);
            },
        );
    });
}

fn thin_divider(ui: &mut egui::Ui) {
    let (rect, _) =
        ui.allocate_exact_size(egui::vec2(ui.available_width(), 1.0), egui::Sense::hover());
    ui.painter()
        .rect_filled(rect, egui::Rounding::ZERO, C_BORDER_1);
}

fn field_label(ui: &mut egui::Ui, text: &str) {
    ui.label(
        RichText::new(text)
            .family(FontFamily::Name(F_DISPLAY.into()))
            .color(C_TEXT_3)
            .size(9.5),
    );
    ui.add_space(4.0);
}

/// A soft pulsing dot for "live" states — animation-driven.
fn pulse_dot(ui: &mut egui::Ui, color: Color32, live: bool, size: f32) {
    let (rect, _) = ui.allocate_exact_size(egui::vec2(size, size), egui::Sense::hover());
    if live {
        let t = ui.ctx().input(|i| i.time as f32);
        let pulse = (t * 2.0).sin() * 0.5 + 0.5; // 0..1
        let outer_alpha = (60.0 + 100.0 * pulse) as u8;
        ui.painter().circle_filled(
            rect.center(),
            size * 0.5,
            Color32::from_rgba_unmultiplied(color.r(), color.g(), color.b(), outer_alpha),
        );
        ui.painter()
            .circle_filled(rect.center(), size * 0.28, color);
    } else {
        ui.painter().circle_filled(
            rect.center(),
            size * 0.28,
            Color32::from_rgb(0x55, 0x55, 0x55),
        );
    }
    ui.ctx().request_repaint_after(Duration::from_millis(40));
}

fn dot_line(ui: &mut egui::Ui, color: Color32, name: &str, state: &str, live: bool) {
    ui.horizontal(|ui| {
        pulse_dot(ui, color, live, 10.0);
        ui.add_space(8.0);
        ui.label(
            RichText::new(name)
                .family(FontFamily::Name(F_UI_B.into()))
                .color(C_TEXT)
                .size(12.5),
        );
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.label(
                RichText::new(state)
                    .family(FontFamily::Name(F_MONO.into()))
                    .color(C_TEXT_2)
                    .size(11.0),
            );
        });
    });
}

fn kv_row(ui: &mut egui::Ui, key: &str, value: &str) {
    ui.horizontal(|ui| {
        ui.label(
            RichText::new(key.to_uppercase())
                .family(FontFamily::Name(F_DISPLAY.into()))
                .color(C_TEXT_3)
                .size(9.5),
        );
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            ui.label(
                RichText::new(value)
                    .family(FontFamily::Name(F_MONO.into()))
                    .color(C_TEXT)
                    .size(12.5),
            );
        });
    });
}

fn version_column(ui: &mut egui::Ui, label: &str, value: &str, tone: Color32) {
    ui.vertical(|ui| {
        ui.label(
            RichText::new(label)
                .family(FontFamily::Name(F_DISPLAY.into()))
                .color(C_TEXT_3)
                .size(9.5),
        );
        ui.add_space(4.0);
        ui.label(
            RichText::new(value)
                .family(FontFamily::Name(F_MONO.into()))
                .color(tone)
                .size(20.0),
        );
    });
}

fn legend_pill(ui: &mut egui::Ui, tag: &str, tone: Color32, meaning: &str) {
    egui::Frame::none()
        .fill(C_SURFACE_3)
        .stroke(Stroke::new(1.0_f32, C_BORDER_1))
        .rounding(egui::Rounding::same(999.0))
        .inner_margin(egui::Margin::symmetric(10.0, 4.0))
        .outer_margin(egui::Margin {
            left: 0.0,
            right: 6.0,
            top: 2.0,
            bottom: 2.0,
        })
        .show(ui, |ui| {
            ui.horizontal(|ui| {
                let (r, _) = ui.allocate_exact_size(egui::vec2(6.0, 6.0), egui::Sense::hover());
                ui.painter().circle_filled(r.center(), 3.0, tone);
                ui.add_space(4.0);
                ui.label(
                    RichText::new(tag)
                        .family(FontFamily::Name(F_UI_B.into()))
                        .color(C_TEXT)
                        .size(10.0),
                );
                ui.add_space(4.0);
                ui.label(
                    RichText::new(meaning)
                        .family(FontFamily::Name(F_UI.into()))
                        .color(C_TEXT_3)
                        .size(10.0),
                );
            });
        });
}

fn rail_live_row(ui: &mut egui::Ui, key: &str, value: &str, tone: Color32, live: bool) {
    ui.horizontal(|ui| {
        pulse_dot(ui, tone, live, 8.0);
        ui.add_space(6.0);
        ui.vertical(|ui| {
            ui.label(
                RichText::new(key.to_uppercase())
                    .family(FontFamily::Name(F_DISPLAY.into()))
                    .color(C_TEXT_4)
                    .size(8.5),
            );
            ui.label(
                RichText::new(value)
                    .family(FontFamily::Name(F_UI_B.into()))
                    .color(tone)
                    .size(11.5),
            );
        });
    });
}

/// A pill-shaped action button with animated fill and amber hover border.
fn pill_button(ui: &mut egui::Ui, label: &str, enabled: bool, danger: bool) -> egui::Response {
    let text = RichText::new(label)
        .family(FontFamily::Name(F_UI_B.into()))
        .color(if enabled { C_TEXT } else { C_TEXT_4 })
        .size(11.5);
    let base_fill = if danger {
        Color32::from_rgb(0x25, 0x14, 0x14)
    } else {
        C_SURFACE_4
    };
    let stroke = if danger {
        Stroke::new(1.0_f32, Color32::from_rgb(0x4a, 0x20, 0x20))
    } else {
        Stroke::new(1.0_f32, C_BORDER_2)
    };

    let btn = egui::Button::new(text)
        .fill(base_fill)
        .stroke(stroke)
        .rounding(egui::Rounding::same(999.0))
        .min_size(egui::vec2(0.0, 34.0));
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

fn syncthing_color(s: &SyncthingState) -> Color32 {
    match s {
        SyncthingState::InSync => C_SUCCESS,
        SyncthingState::Syncing => C_WARN,
        SyncthingState::NotConfigured => C_TEXT_3,
        SyncthingState::Unreachable(_) => C_WARN,
        SyncthingState::AuthFailed
        | SyncthingState::FolderMissing
        | SyncthingState::BadConfig(_)
        | SyncthingState::Error(_) => C_DANGER,
    }
}

fn section_deck(t: Tab) -> &'static str {
    match t {
        Tab::Status  => "Reconciled role of this ChatBucket instance, its process, and whoever holds the current host claim.",
        Tab::Sync    => "ChatBucket control plane for Syncthing: folders, devices, incoming requests, repair, and rescan.",
        Tab::Network => "Tailnet peer list and the Syncthing REST bridge that replicates sync-state between hosts.",
        Tab::Updates => "Compare local VERSION against the latest GitHub release. Installs replace code only — data is preserved.",
        Tab::Logs    => "Rolling ring buffer of everything the manager did — refreshes, actions, config writes, update steps.",
    }
}

fn lerp_color(a: Color32, b: Color32, t: f32) -> Color32 {
    let t = t.clamp(0.0, 1.0);
    let l = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t) as u8;
    Color32::from_rgb(l(a.r(), b.r()), l(a.g(), b.g()), l(a.b(), b.b()))
}

// ── CLI PROBE (unchanged) ─────────────────────────────────────────────
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
        (Some(name), Some(ClaimedReachability::IsSelf)) => println!("  {}: (this machine)", name),
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
                println!(
                    "  {}: {}",
                    p.name,
                    if p.online { "online" } else { "offline" }
                );
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
        SyncthingState::AuthFailed => println!("  AUTH FAILED (Syncthing rejected the API key)"),
        SyncthingState::Unreachable(d) => println!("  UNREACHABLE — {d}"),
        SyncthingState::FolderMissing => println!("  FOLDER NOT FOUND ('sync-state' folder id)"),
        SyncthingState::BadConfig(d) => println!("  BAD CONFIG — {d}"),
        SyncthingState::Error(d) => println!("  ERROR — {d}"),
    }
    Ok(())
}

fn print_host_state(s: &HostState) {
    println!("  action:    {}", s.action);
    println!("  machine:   {}", s.machine);
    println!("  timestamp: {}", s.timestamp);
}
