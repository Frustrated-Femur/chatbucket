//! manager.rs — ManagerContext: single source of truth for all status/action logic.
//!
//! Under the front-door architecture the Manager's role has narrowed but
//! its "one source of truth" discipline is unchanged. Specifically:
//!
//!   * `derive_role_state()` is the ONLY reducer of (front-door status,
//!     host-state, my_name, process fallback) into a Role badge. Called by
//!     BOTH the window path and the tray path — never re-derived
//!     independently (§16.5 "one source of truth", integration.md §6).
//!   * `get_status()` polls the front door FIRST (127.0.0.1:5050/status)
//!     and derives Role/Process from that live truth. `process_scan` is
//!     kept as an explicit "front door unreachable" fallback ONLY —
//!     matching integration.md §4's design decision.
//!   * `start()` / `stop()` now POST to the front-door control endpoint;
//!     the front door owns the child process lifecycle. The gunicorn
//!     master/worker signalling dance the old code had is dead — the
//!     child is a plain server.py under a supervisor and the supervisor
//!     is what we ask to stop.
//!   * `save_config()` still writes manager_config.json for the Syncthing
//!     keys the Rust side owns; the Python-owned booleans `auto_host` /
//!     `take_host_on_crash` are preserved untouched thanks to the
//!     `#[serde(flatten)] extras` field on `ManagerConfig`. When we want
//!     to *change* those booleans we POST to /control set_config so the
//!     front door's control queue serialises the change with its own
//!     supervisor decisions.

use crate::arbitration::{self, ArbitrationError, PeerList};
use crate::front_door_client::{
    ConfigPatch, FrontDoorClient, FrontDoorError, FrontDoorStatus, Routing,
};
use crate::host_state::{self, HostState, HostStateError};
use crate::process_scan::{self, ProcRole, SubShape};
use crate::resources::{self, FolderHealth};
use crate::syncthing::{
    self, ConnectionState, DeviceAddOutcome, InstallState, ManagedTransition, ManagerConfig,
    PendingSnapshot, ReconcileReport, SyncthingController,
};
use crate::update::{self, GithubRelease};

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;

// STOP_GRACE_SECONDS / START_GRACE_SECONDS from the old process-signalling
// path are gone — the front door owns lifecycle timing now. Kept as a
// harmless constant only for the process_scan fallback's kill_stale path.
pub const STOP_GRACE_SECONDS: u64 = 10;

// ── Role-state vocabulary ──────────────────────────────────────────────
//
// Adds `Redirect` for the front-door "we're a client of another host"
// state that the old process-scan code lumped in with `Client`. The old
// name is kept as the semantic label the GUI shows, but the enum
// distinguishes the two so we can honestly say WHICH host we're routing
// to instead of just "some client". Table in integration.md §4.2.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RoleState {
    Host,
    Client,
    Idle,
    Stale,
    Conflict,
    Starting,
    Unknown,
    /// Front door is currently redirecting to another host. Distinct
    /// from the legacy `Client` (which only meant "doorman is running") —
    /// this is the affirmative "we are a client of X" state.
    #[allow(dead_code)] // constructed by derive_role_state, matched by GUI
    Redirect,
    /// Front door is up but neither hosting locally nor able to redirect.
    /// This is the "no known host, waiting" case that the old process-
    /// scan model couldn't express distinctly from `Idle`.
    Unavailable,
}

impl RoleState {
    pub fn key(&self) -> &'static str {
        match self {
            RoleState::Host => "host",
            RoleState::Client => "client",
            RoleState::Redirect => "redirect",
            RoleState::Idle => "idle",
            RoleState::Stale => "stale",
            RoleState::Conflict => "conflict",
            RoleState::Starting => "starting",
            RoleState::Unknown => "unknown",
            RoleState::Unavailable => "unavailable",
        }
    }
    pub fn label(&self) -> &'static str {
        match self {
            RoleState::Host => "HOST",
            RoleState::Client => "CLIENT",
            RoleState::Redirect => "CLIENT",
            RoleState::Idle => "IDLE",
            RoleState::Stale => "STALE CLAIM",
            RoleState::Conflict => "CONFLICT",
            RoleState::Starting => "STARTING",
            RoleState::Unknown => "UNKNOWN",
            RoleState::Unavailable => "UNAVAILABLE",
        }
    }
    /// Hex color from the same palette as web/index.html's :root vars.
    pub fn color_hex(&self) -> &'static str {
        match self {
            RoleState::Host => "#4ade80",       // --success
            RoleState::Client => "#f5f5f5",     // --text
            RoleState::Redirect => "#f5f5f5",   // --text (same visual)
            RoleState::Starting => "#b5b5b5",   // --text-2
            RoleState::Idle => "#7a7a7a",       // --text-3
            RoleState::Stale => "#fbbf24",      // --warn
            RoleState::Conflict => "#ef4444",   // --danger
            RoleState::Unknown => "#7a7a7a",    // --text-3
            RoleState::Unavailable => "#fbbf24",// --warn
        }
    }
}

#[derive(Debug, Clone)]
pub struct RoleDetail {
    pub state: RoleState,
    pub detail: String,
}

// ── StatusSnapshot ─────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct StatusSnapshot {
    #[allow(dead_code)]
    pub my_name: String,
    pub host_state: Result<Option<HostState>, String>,
    pub claimed_machine: Option<String>,
    pub claimed_reachability: Option<ClaimedReachability>,
    pub tailnet_peers: Result<PeerList, String>,
    /// Live process/routing snapshot from the front door when available;
    /// falls back to a process_scan record when the front door is down.
    pub process: Option<ProcessInfo>,
    /// Raw front-door status if we could reach it — the GUI uses this to
    /// distinguish "front door says starting" from "front door is down".
    pub front_door: Option<FrontDoorStatus>,
    pub role: RoleDetail,
    pub version: Option<String>,
    /// Structured ChatBucket-level Syncthing view (§27). The GUI renders
    /// THIS; there is no separate `syncthing` field any more — the
    /// legacy `sync-state` probe was removed with the front-door work.
    pub sync: SyncSnapshot,
}

// ── Structured Syncthing state model (task §27) ────────────────────────

/// One row in the folder list: ChatBucket terms, not raw Syncthing JSON.
#[derive(Debug, Clone)]
pub struct FolderView {
    pub folder_id: String,
    pub label: &'static str,
    pub managed: bool,
    pub health: FolderHealth,
    pub need_files: i64,
    pub need_bytes: i64,
    pub pull_errors: i64,
    pub error_count: usize,
    pub last_scan: String,
    pub has_conflict: bool,
}

/// One row in the device list (§6 device list).
#[derive(Debug, Clone)]
pub struct DeviceView {
    pub device_id: String,
    pub name: String,
    pub connected: bool,
    /// Configured but not yet associated with all managed folders (§6).
    pub fully_associated: bool,
}

/// The complete ChatBucket-facing Syncthing state for the UI.
#[derive(Debug, Clone)]
pub struct SyncSnapshot {
    pub install: InstallState,
    pub connection: ConnectionState,
    pub folders: Vec<FolderView>,
    pub devices: Vec<DeviceView>,
    pub pending: Option<PendingSnapshot>,
    /// Number of `.sync-conflict-*.json` files detected under state/ (§15).
    pub state_conflict_count: usize,
    pub syncthing_version: Option<String>,
}

impl Default for SyncSnapshot {
    fn default() -> Self {
        Self {
            install: InstallState::Unknown,
            connection: ConnectionState::NotConfigured,
            folders: Vec::new(),
            devices: Vec::new(),
            pending: None,
            state_conflict_count: 0,
            syncthing_version: None,
        }
    }
}

impl SyncSnapshot {
    /// Worst-of aggregate over the *managed* folders. Used by the nav
    /// rail's sync row so it agrees with the Sync tab's own aggregation
    /// (integration.md §6). Disabled folders are excluded from the
    /// denominator entirely — they aren't "unhealthy", they're
    /// deliberately not managed.
    pub fn aggregate_health(&self) -> SyncAggregate {
        let managed: Vec<&FolderView> = self.folders.iter().filter(|f| f.managed).collect();
        let total = managed.len();
        if total == 0 {
            return SyncAggregate {
                worst: FolderHealth::Disabled, // "nothing to report"
                healthy: 0,
                total: 0,
            };
        }
        // Health order, worst first. Anything that maps to Disabled or
        // Unknown does not count as "unhealthy" for the aggregate colour;
        // only sync-error / auth-failed / conflict / unreachable /
        // missing / configmismatch do.
        fn rank(h: &FolderHealth) -> u8 {
            match h {
                FolderHealth::Conflict => 0,
                FolderHealth::AuthFailed => 1,
                FolderHealth::SyncError => 2,
                FolderHealth::Unreachable => 3,
                FolderHealth::Missing => 4,
                FolderHealth::ConfigMismatch => 5,
                FolderHealth::Syncing { .. } => 6,
                FolderHealth::Unknown => 7,
                FolderHealth::InSync => 8,
                FolderHealth::Disabled => 9,
            }
        }
        let worst = managed
            .iter()
            .map(|f| f.health.clone())
            .min_by_key(rank)
            .unwrap_or(FolderHealth::Unknown);
        let healthy = managed
            .iter()
            .filter(|f| matches!(f.health, FolderHealth::InSync))
            .count();
        SyncAggregate {
            worst,
            healthy,
            total,
        }
    }
}

/// Result of `SyncSnapshot::aggregate_health()`. Consumed by the nav rail
/// row so its badge and count stay identical to the Sync tab's own view.
#[derive(Debug, Clone)]
pub struct SyncAggregate {
    pub worst: FolderHealth,
    pub healthy: usize,
    pub total: usize,
}

#[derive(Debug, Clone)]
pub enum ClaimedReachability {
    IsSelf,
    Online,
    Offline,
    Error(String),
}

/// Rendered "process" line for the GUI. Under the front door design this
/// mostly comes from the /status endpoint (`child_pid`, `child_running`);
/// only if the endpoint is unreachable do we fall back to the OS-level
/// process scanner. The `role`/`subshape` fields keep their old meaning
/// so the process card doesn't need to know which path produced them.
#[derive(Debug, Clone)]
pub struct ProcessInfo {
    pub pid: u32,
    pub role: ProcRole,
    pub subshape: SubShape,
    /// Where this record came from — the GUI uses this to explain the
    /// distinction on the process card ("via front door" vs "via OS scan").
    pub source: ProcessSource,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProcessSource {
    /// Reported by 127.0.0.1:5050/status — the authoritative source.
    FrontDoor,
    /// Discovered by scanning the OS process table — fallback only,
    /// used when the front-door endpoint is unreachable.
    ProcessScan,
}

// ── Action outcomes ────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct ActionResult {
    pub ok: bool,
    pub action: &'static str, // "started" / "stopped" / "none" / "error"
    pub detail: String,
}

#[derive(Debug, Clone)]
pub struct UpdateCheckResult {
    pub ok: bool,
    #[allow(dead_code)]
    pub current: Option<String>,
    pub latest: Option<String>,
    pub newer_available: bool,
    pub release_url: Option<String>,
    pub detail: String,
}

#[derive(Debug, Clone)]
pub struct UpdateInstallResult {
    pub ok: bool,
    pub step: &'static str,
    pub detail: String,
}

// ── ManagerContext ─────────────────────────────────────────────────────

pub struct ManagerContext {
    pub repo_root: PathBuf,
    pub my_name: String,
    last_update_check: Mutex<Option<GithubRelease>>,
    /// The ChatBucket Syncthing controller (§3). Owns all Syncthing REST.
    pub syncthing: Arc<SyncthingController>,
    /// The front-door loopback client (integration §4.4). Cheap struct;
    /// no persistent state.
    pub front_door: FrontDoorClient,
}

#[derive(Debug, Clone)]
pub struct ConfigSaveResult {
    pub ok: bool,
    pub detail: String,
    /// The stored config AFTER normalisation, so the GUI can refresh its
    /// input boxes with what actually landed on disk (whitespace stripped,
    /// trailing slash removed, empty-string → unset).
    pub saved: Option<ManagerConfig>,
    /// A live test-connect result run against the just-saved config, so
    /// the user gets one round-trip answer. This is the structured
    /// `ConnectionState` — not the removed `LegacyState` — so we no
    /// longer conflate "cannot reach Syncthing" with "cannot see the
    /// old sync-state folder".
    #[allow(dead_code)]
    pub tested: Option<ConnectionState>,
}

impl ManagerContext {
    pub fn new(repo_root: PathBuf) -> Self {
        let my_name = detect_machine_name();
        let syncthing = Arc::new(SyncthingController::new(repo_root.clone()));
        Self {
            repo_root,
            my_name,
            last_update_check: Mutex::new(None),
            syncthing,
            front_door: FrontDoorClient::new(),
        }
    }

    /// Full status poll — front door FIRST, everything else derived
    /// consistently from it. Called from the worker thread.
    pub fn get_status(&self) -> StatusSnapshot {
        let hs_result = host_state::read_state(&self.repo_root);
        let (host_state_ui, claimed_machine) = match &hs_result {
            Ok(Some(state)) => (Ok(Some(state.clone())), Some(state.machine.clone())),
            Ok(None) => (Ok(None), None),
            Err(e) => (Err(e.to_string()), None),
        };

        // 1. Front-door status is the primary source of truth for
        //    "am I hosting / redirecting / starting?" and for the child
        //    process's PID. Everything else in the snapshot is either
        //    file-level (host-state, tailnet peers, syncthing) or a
        //    fallback the GUI marks as such.
        let fd = self.front_door.status().ok();

        // 2. Process line derivation: front door if we have it, else
        //    OS scan. Never both — a two-source blend would be exactly
        //    the "two paths risk disagreement" trap integration.md §6
        //    calls out.
        let process_info = match &fd {
            Some(st) => process_info_from_front_door(st),
            None => process_scan::find_chatbucket_process(&self.repo_root).map(|p| ProcessInfo {
                pid: p.pid,
                role: p.role,
                subshape: p.subshape,
                source: ProcessSource::ProcessScan,
            }),
        };

        // 3. Role reducer — single-source discipline.
        let role = derive_role_state(
            &hs_result,
            claimed_machine.as_deref(),
            &self.my_name,
            fd.as_ref(),
            process_info.as_ref(),
        );

        let claimed_reachability = claimed_machine.as_ref().map(|m| {
            if m == &self.my_name {
                ClaimedReachability::IsSelf
            } else {
                match arbitration::check_machine_online(m) {
                    Ok(true) => ClaimedReachability::Online,
                    Ok(false) => ClaimedReachability::Offline,
                    Err(e) => ClaimedReachability::Error(e.to_string()),
                }
            }
        });

        let tailnet_peers =
            arbitration::list_tailnet_peers().map_err(|e: ArbitrationError| e.to_string());
        let sync = self.sync_snapshot();

        StatusSnapshot {
            my_name: self.my_name.clone(),
            host_state: host_state_ui,
            claimed_machine,
            claimed_reachability,
            tailnet_peers,
            process: process_info,
            front_door: fd,
            role,
            version: update::read_local_version(&self.repo_root),
            sync,
        }
    }

    /// Cheap subset for the tray icon's 5s poll — front door only, plus
    /// host_state for the STALE case. NO tailscale CLI, no process scan.
    /// Same "one reducer" discipline as `get_status`: both call the
    /// SAME `derive_role_state()`, just with fewer inputs.
    pub fn get_role_only(&self) -> RoleDetail {
        let hs = host_state::read_state(&self.repo_root);
        let claimed = match &hs {
            Ok(Some(s)) => Some(s.machine.clone()),
            _ => None,
        };
        let fd = self.front_door.status().ok();
        // Only scan the OS if the front door is down — otherwise we'd
        // pay a process-table walk on every 5s tray tick for no gain.
        let proc = match &fd {
            Some(st) => process_info_from_front_door(st),
            None => process_scan::find_chatbucket_process(&self.repo_root).map(|p| ProcessInfo {
                pid: p.pid,
                role: p.role,
                subshape: p.subshape,
                source: ProcessSource::ProcessScan,
            }),
        };
        derive_role_state(&hs, claimed.as_deref(), &self.my_name, fd.as_ref(), proc.as_ref())
    }

    // ── Manager config (Syncthing API key + URL) ───────────────────

    pub fn read_config(&self) -> (ManagerConfig, Option<String>) {
        syncthing::read_config(&self.repo_root)
    }

    /// Save a new config atomically. Preserves the Python-owned
    /// `auto_host` / `take_host_on_crash` keys via the flatten-extras
    /// discipline in `ManagerConfig`. Runs a live probe against the
    /// saved config so the user gets one round-trip verdict.
    pub fn save_config(&self, cfg: ManagerConfig) -> ConfigSaveResult {
        match syncthing::save_config(&self.repo_root, cfg) {
            Ok(saved) => {
                // Only probe if there IS actually a key to probe with —
                // saving an empty key intentionally clears the config,
                // which should not surface as "unreachable".
                let tested = if saved.key().is_some() {
                    // Force-invalidate the cache and re-probe.
                    self.syncthing.invalidate();
                    Some(self.syncthing.connection_state())
                } else {
                    None
                };
                let detail = match &tested {
                    None => "Config saved. Syncthing probe disabled (no API key).".to_string(),
                    Some(ConnectionState::Connected) => "Saved. Connected to Syncthing.".into(),
                    Some(ConnectionState::AuthFailed) => {
                        "Saved, but Syncthing rejected the key (401/403).".into()
                    }
                    Some(ConnectionState::Unreachable(d)) => {
                        format!("Saved, but could not reach Syncthing: {d}")
                    }
                    Some(ConnectionState::NotConfigured) => {
                        "Saved. No API key configured.".into()
                    }
                    Some(ConnectionState::BadConfig(d)) => {
                        format!("Saved, but config is malformed: {d}")
                    }
                    Some(ConnectionState::InvalidResponse(d)) => {
                        format!("Saved, but Syncthing gave an unexpected response: {d}")
                    }
                };
                ConfigSaveResult {
                    ok: true,
                    detail,
                    saved: Some(saved),
                    tested,
                }
            }
            Err(e) => ConfigSaveResult {
                ok: false,
                detail: format!("Could not save manager_config.json: {e}"),
                saved: None,
                tested: None,
            },
        }
    }

    /// Force a fresh Syncthing probe. Used by the "Test connection"
    /// button next to the API key input. Returns the structured
    /// `ConnectionState`; there is no longer a separate legacy probe.
    pub fn test_syncthing(&self) -> ConnectionState {
        self.syncthing.invalidate();
        self.syncthing.connection_state()
    }

    /// Push `auto_host` / `take_host_on_crash` to the front door via
    /// /control set_config. The Python side is what actually persists
    /// them (into manager_config.json) — routing them through the
    /// control endpoint keeps the front door's control queue as the
    /// single writer, avoiding the file-race the .md's §4.4 flags.
    #[allow(dead_code)] // wired into the front-door panel when it lands
    pub fn push_front_door_config(&self, patch: ConfigPatch) -> Result<(), String> {
        self.front_door
            .set_config(patch)
            .map_err(|e| format!("front door rejected config: {e}"))
    }

    // ── Syncthing integration (ChatBucket control plane) ────────────

    pub fn sync_snapshot(&self) -> SyncSnapshot {
        let install = self.syncthing.install_state();
        let connection = self.syncthing.connection_state();
        let (cfg, _) = self.syncthing.read_config();
        let last_scans = self.syncthing.folder_last_scan().unwrap_or_default();
        let state_conflicts = resources::find_state_conflicts(&self.repo_root);
        let state_conflict_count = state_conflicts.len();

        let connected = connection.is_connected();
        let mut folders = Vec::new();
        for def in resources::RESOURCES {
            let managed = cfg.managed.is_managed(def.folder_id);
            let mut view = FolderView {
                folder_id: def.folder_id.to_string(),
                label: def.label,
                managed,
                health: FolderHealth::Unknown,
                need_files: 0,
                need_bytes: 0,
                pull_errors: 0,
                error_count: 0,
                last_scan: last_scans
                    .get(def.folder_id)
                    .map(|s| s.last_scan.clone())
                    .unwrap_or_default(),
                has_conflict: false,
            };
            if !managed {
                view.health = FolderHealth::Disabled;
            } else if !connected {
                view.health = match connection {
                    ConnectionState::AuthFailed => FolderHealth::AuthFailed,
                    ConnectionState::Unreachable(_) => FolderHealth::Unreachable,
                    _ => FolderHealth::Unknown,
                };
            } else {
                match self.syncthing.folder_status(def.folder_id) {
                    Ok(st) => {
                        view.need_files = st.need_files;
                        view.need_bytes = st.need_bytes;
                        view.pull_errors = st.pull_errors;
                        let errs = self
                            .syncthing
                            .folder_errors(def.folder_id)
                            .map(|e| e.len())
                            .unwrap_or(0);
                        view.error_count = errs;
                        let has_conflict = def.is_arbitration_state && state_conflict_count > 0;
                        view.has_conflict = has_conflict;
                        view.health = resources::classify_folder(
                            true,
                            true,
                            &st.state,
                            st.need_files,
                            st.need_bytes,
                            st.pull_errors,
                            has_conflict,
                        );
                    }
                    Err(syncthing::ApiError::NotFound(_)) => {
                        view.health = FolderHealth::Missing;
                    }
                    Err(syncthing::ApiError::AuthFailed) => {
                        view.health = FolderHealth::AuthFailed;
                    }
                    Err(_) => {
                        view.health = FolderHealth::Unknown;
                    }
                }
            }
            folders.push(view);
        }

        // Devices: those referenced by ChatBucket folders (self-filtered
        // inside all_chatbucket_device_associations — see §1), plus
        // pending. If we're not connected we can't build the list.
        let devices = if connected {
            self.build_device_views()
        } else {
            Vec::new()
        };

        let pending = if connected {
            self.syncthing.pending_snapshot().ok()
        } else {
            None
        };

        let syncthing_version = if connected {
            self.syncthing.version().ok().map(|v| v.version)
        } else {
            None
        };

        SyncSnapshot {
            install,
            connection,
            folders,
            devices,
            pending,
            state_conflict_count,
            syncthing_version,
        }
    }

    fn build_device_views(&self) -> Vec<DeviceView> {
        let (cfg, _) = self.syncthing.read_config();
        let managed_ids: Vec<String> = cfg
            .managed
            .managed_resources()
            .iter()
            .map(|r| r.folder_id.to_string())
            .collect();
        // Self-filter and unrelated-folder-filter both live inside this
        // call — see the docstring on all_chatbucket_device_associations.
        let configured = self.syncthing.all_chatbucket_device_associations();
        let mut out = Vec::new();
        for (device_id, folders) in configured {
            let fully = managed_ids.iter().all(|m| folders.contains(m));
            out.push(DeviceView {
                device_id: device_id.clone(),
                name: short_id(&device_id),
                connected: self.syncthing.device_connected(&device_id),
                fully_associated: fully,
            });
        }
        out
    }

    // ── Syncthing actions (called from the worker thread) ──────────

    pub fn sync_auto_connect(&self) -> String {
        match self.syncthing.auto_connect() {
            ConnectionState::Connected => "Connected to Syncthing automatically.".into(),
            other => format!("Auto-connect: {}", other.label()),
        }
    }

    pub fn sync_launch_syncthing(&self) -> String {
        match syncthing::launch_syncthing() {
            Ok(m) => m,
            Err(e) => e,
        }
    }

    pub fn sync_reconcile(&self) -> Result<ReconcileReport, String> {
        self.syncthing.reconcile().map_err(|e| e.to_string())
    }

    pub fn sync_scan_folder(&self, folder_id: &str) -> Result<(), String> {
        self.syncthing
            .scan_folder(folder_id)
            .map_err(|e| e.to_string())
    }

    pub fn sync_scan_all(&self) -> Result<usize, String> {
        self.syncthing
            .scan_all_chatbucket()
            .map_err(|e| e.to_string())
    }

    pub fn sync_pause_folder(&self, folder_id: &str) -> Result<(), String> {
        self.syncthing
            .pause_folder(folder_id)
            .map_err(|e| e.to_string())
    }

    pub fn sync_resume_folder(&self, folder_id: &str) -> Result<(), String> {
        self.syncthing
            .resume_folder(folder_id)
            .map_err(|e| e.to_string())
    }

    pub fn sync_restart(&self) -> Result<(), String> {
        self.syncthing
            .restart_syncthing()
            .map_err(|e| e.to_string())
    }

    pub fn sync_clear_errors(&self) -> Result<(), String> {
        self.syncthing.clear_errors().map_err(|e| e.to_string())
    }

    pub fn sync_set_managed(
        &self,
        folder_id: &str,
        managed: bool,
    ) -> Result<ManagedTransition, String> {
        self.syncthing
            .set_managed(folder_id, managed)
            .map_err(|e| e.to_string())
    }

    pub fn sync_add_device(&self, device_id: &str) -> Result<DeviceAddOutcome, String> {
        self.syncthing
            .add_remote_device(device_id, None)
            .map_err(|e| e.to_string())
    }

    pub fn sync_remove_device(&self, device_id: &str) -> Result<(), String> {
        self.syncthing
            .remove_remote_device(device_id)
            .map_err(|e| e.to_string())
    }

    pub fn sync_accept_pending(
        &self,
        device_id: &str,
        folder_ids: &[String],
    ) -> Result<DeviceAddOutcome, String> {
        self.syncthing
            .accept_pending(device_id, folder_ids)
            .map_err(|e| e.to_string())
    }

    pub fn sync_reject_pending(&self, device_id: &str) -> Result<(), String> {
        self.syncthing
            .reject_pending(device_id)
            .map_err(|e| e.to_string())
    }

    pub fn sync_poll_pending_events(&self, since: u64) -> Result<(bool, u64), String> {
        self.syncthing
            .wait_for_pending_event(since)
            .map_err(|e| e.to_string())
    }

    #[allow(dead_code)]
    pub fn sync_pending_snapshot(&self) -> Result<PendingSnapshot, String> {
        self.syncthing.pending_snapshot().map_err(|e| e.to_string())
    }

    // ── start() / stop() — now delegated to the front door ─────────
    //
    // These used to spawn/kill the whole Python process; under the
    // front-door design that process is persistent and owns its own
    // child. We just POST to /control and the supervisor does the work.
    // If /control is unreachable we surface that honestly — we DO NOT
    // try to spawn a Python process ourselves as a fallback, because
    // that would race the front door if it happens to come back up.

    pub fn start(&self) -> ActionResult {
        match self.front_door.start() {
            Ok(()) => ActionResult {
                ok: true,
                action: "started",
                detail: "Start requested — the front door will attempt to claim host."
                    .into(),
            },
            Err(FrontDoorError::Unreachable(d)) => ActionResult {
                ok: false,
                action: "error",
                detail: format!(
                    "Front door is not running on 127.0.0.1:5050 ({d}). Launch main.py first \
                     — the Manager no longer spawns it directly under the front-door design."
                ),
            },
            Err(e) => ActionResult {
                ok: false,
                action: "error",
                detail: format!("Start rejected: {e}"),
            },
        }
    }

    pub fn stop(&self) -> ActionResult {
        match self.front_door.stop() {
            Ok(()) => ActionResult {
                ok: true,
                action: "stopped",
                detail: "Stop requested — the front door will release its child.".into(),
            },
            Err(FrontDoorError::Unreachable(d)) => {
                // The old code force-killed the process group here.
                // Under the front-door design that would only orphan the
                // supervisor's tracking, not actually stop anything if
                // the supervisor is up. Surface honestly.
                ActionResult {
                    ok: false,
                    action: "error",
                    detail: format!(
                        "Front door is not responding ({d}). If a stray server.py is running \
                         you can kill it manually — but under normal operation the front door \
                         is the only thing that should be signalling the child."
                    ),
                }
            }
            Err(e) => ActionResult {
                ok: false,
                action: "error",
                detail: format!("Stop rejected: {e}"),
            },
        }
    }

    // ── Updates ─────────────────────────────────────────────────────

    pub fn check_for_updates(&self) -> UpdateCheckResult {
        let current = update::read_local_version(&self.repo_root);
        let release = match update::fetch_latest_release() {
            Ok(r) => r,
            Err(e) => {
                return UpdateCheckResult {
                    ok: false,
                    current,
                    latest: None,
                    newer_available: false,
                    release_url: None,
                    detail: e.to_string(),
                }
            }
        };
        let latest = release.tag_name.clone();
        let release_url = release.html_url.clone();

        let newer = match &current {
            None => true,
            Some(cur) => update::is_newer(&latest, cur),
        };
        let detail = match (&current, newer) {
            (None, _) => format!(
                "No local VERSION file found. Latest release is {}. Install to set the baseline.",
                latest
            ),
            (Some(cur), true) => format!("Update available: {} → {}.", cur, latest),
            (Some(cur), false) => format!("Up to date (installed {}, latest {}).", cur, latest),
        };

        *self.last_update_check.lock().unwrap() = Some(release);

        UpdateCheckResult {
            ok: true,
            current,
            latest: Some(latest),
            newer_available: newer,
            release_url,
            detail,
        }
    }

    pub fn install_update(&self) -> UpdateInstallResult {
        let pre = self.check_for_updates();
        if !pre.ok {
            return UpdateInstallResult {
                ok: false,
                step: "check",
                detail: pre.detail,
            };
        }
        if !pre.newer_available {
            return UpdateInstallResult {
                ok: true,
                step: "noop",
                detail: pre.detail,
            };
        }
        // Ask the front door to stop its child before we swap code.
        let stop_result = self.stop();
        if !stop_result.ok {
            return UpdateInstallResult {
                ok: false,
                step: "stop",
                detail: format!(
                    "Refusing to update while ChatBucket is still running: {}",
                    stop_result.detail
                ),
            };
        }

        let release = match update::fetch_latest_release() {
            Ok(r) => r,
            Err(e) => {
                return UpdateInstallResult {
                    ok: false,
                    step: "fetch",
                    detail: e.to_string(),
                }
            }
        };

        let backed_up = match update::backup_current_code(&self.repo_root) {
            Ok(v) => v,
            Err(e) => {
                return UpdateInstallResult {
                    ok: false,
                    step: "backup",
                    detail: format!("Could not back up current code: {e}"),
                }
            }
        };
        if backed_up.is_empty() {
            return UpdateInstallResult {
                ok: false,
                step: "backup",
                detail: "No code files were backed up — refusing to extract on top of an unrecognised repo layout.".into(),
            };
        }

        let zip_path = match update::download_release_zip(&release.zipball_url) {
            Ok(p) => p,
            Err(e) => {
                let _ = update::restore_backup(&self.repo_root);
                return UpdateInstallResult {
                    ok: false,
                    step: "download",
                    detail: format!("Failed to download release ZIP: {e}"),
                };
            }
        };

        if let Err(e) = update::read_and_verify_zip_head(&zip_path) {
            let _ = std::fs::remove_file(&zip_path);
            let _ = update::restore_backup(&self.repo_root);
            return UpdateInstallResult {
                ok: false,
                step: "download",
                detail: e,
            };
        }

        let written = match update::extract_release_zip(&zip_path, &self.repo_root) {
            Ok(n) => n,
            Err(e) => {
                let _ = std::fs::remove_file(&zip_path);
                let _ = update::restore_backup(&self.repo_root);
                return UpdateInstallResult {
                    ok: false,
                    step: "extract",
                    detail: format!("Release ZIP could not be extracted: {e}"),
                };
            }
        };
        let _ = std::fs::remove_file(&zip_path);

        if written == 0 {
            let _ = update::restore_backup(&self.repo_root);
            return UpdateInstallResult {
                ok: false,
                step: "extract",
                detail:
                    "Release ZIP contained no allow-listed files — treated as broken; rolled back."
                        .into(),
            };
        }

        if let Err(e) = update::sanity_check_extracted(&self.repo_root) {
            let _ = update::restore_backup(&self.repo_root);
            return UpdateInstallResult {
                ok: false,
                step: "verify",
                detail: format!(
                    "New code failed sanity check ({e}). Rolled back to previous version."
                ),
            };
        }

        let _ = std::fs::remove_dir_all(self.repo_root.join(update::BACKUP_DIR));

        let new_v = update::read_local_version(&self.repo_root).unwrap_or(release.tag_name);
        UpdateInstallResult {
            ok: true,
            step: "done",
            detail: format!(
                "Updated to {}. Ask the front door (or restart main.py) to relaunch — \
                 restart goes through arbitration on purpose, not straight back into the prior role.",
                new_v
            ),
        }
    }
}

// ── Free-standing helpers ─────────────────────────────────────────────

fn short_id(id: &str) -> String {
    id.chars().take(7).collect()
}

pub fn detect_machine_name() -> String {
    let raw = hostname::get()
        .ok()
        .and_then(|h| h.into_string().ok())
        .or_else(|| std::env::var("COMPUTERNAME").ok())
        .or_else(|| std::env::var("HOSTNAME").ok())
        .unwrap_or_default();
    normalize_name(&raw)
}

pub fn normalize_name(name: &str) -> String {
    name.split('.').next().unwrap_or("").trim().to_lowercase()
}

/// Convert a `FrontDoorStatus` into a `ProcessInfo` record, or None if
/// the front door reports no child. This is the "front door tells us
/// directly" path — no OS scan, no gunicorn shape guessing.
fn process_info_from_front_door(st: &FrontDoorStatus) -> Option<ProcessInfo> {
    let pid = st.child_pid?;
    if !st.child_running {
        // The front door tracks child_pid across the brief post-exit
        // window; if child_running is false the process is gone or
        // going, and we should not claim it as running.
        return None;
    }
    // The child is server.py under a supervisor: role is always Host in
    // ChatBucket terms (the doorman/client roles used to be a separate
    // process; now they're just routing decisions in the front door and
    // there is no distinct client process to point at). Use the shape
    // enum to record that this is a plain-server child, not gunicorn.
    let role = match st.routing {
        Routing::Local => ProcRole::Host,
        _ => ProcRole::Host, // child_running is only true when we're hosting
    };
    Some(ProcessInfo {
        pid,
        role,
        subshape: SubShape::PythonServer,
        source: ProcessSource::FrontDoor,
    })
}

/// The single reducer of (host_state, front-door status, process info)
/// into a Role badge. Both `get_status` and `get_role_only` MUST call
/// this — no ad-hoc derivations elsewhere.
///
/// Priority order (integration.md §4.2, adapted for the real Python
/// contract we verified against front_door.py):
///
///   1. host_state.json corrupted → Unknown (surface loudly).
///   2. Front door reachable → treat its `routing` + `starting` as
///      authoritative. process_info here is coming from the front door,
///      so this branch is single-source by construction.
///   3. Front door unreachable → we fall back to (host_state + process
///      scan) exactly like the pre-front-door code. process_info here
///      is from the OS scan; we mark this in `detail` so the user
///      knows the front door is down.
///   4. No front door + no process + no claim → Idle.
pub fn derive_role_state(
    hs: &Result<Option<HostState>, HostStateError>,
    claimed_machine: Option<&str>,
    my_name: &str,
    front_door: Option<&FrontDoorStatus>,
    process: Option<&ProcessInfo>,
) -> RoleDetail {
    if hs.is_err() {
        return RoleDetail {
            state: RoleState::Unknown,
            detail: "host-state.json is corrupted.".into(),
        };
    }

    // ── Front-door-authoritative branch ─────────────────────────────
    if let Some(fd) = front_door {
        // `starting` means the pointer is transitioning — the front
        // door itself calls this state out explicitly and we should
        // not try to second-guess it.
        if fd.starting {
            let hint = fd
                .last_error
                .as_deref()
                .map(|e| format!(" ({e})"))
                .unwrap_or_default();
            return RoleDetail {
                state: RoleState::Starting,
                detail: format!("Front door is arbitrating / transitioning{hint}"),
            };
        }

        return match &fd.routing {
            Routing::Local => {
                // We are hosting locally. child_running=false here
                // during the tiny gap between claim-decided and
                // child-bound; distinguish that from real Host.
                if fd.child_running {
                    RoleDetail {
                        state: RoleState::Host,
                        detail: "This machine is currently serving ChatBucket.".into(),
                    }
                } else {
                    RoleDetail {
                        state: RoleState::Starting,
                        detail: "Front door claimed HOST but child hasn't come up yet."
                            .into(),
                    }
                }
            }
            Routing::Redirect(m) => RoleDetail {
                state: RoleState::Redirect,
                detail: format!("Redirecting to host: {m}"),
            },
            Routing::Unavailable => {
                // Front door is up but no host is known. If host-state
                // claims someone specific we surface that, otherwise
                // Idle. Distinguished from the pre-front-door Idle
                // because the front door being up gives us a positive
                // "no one is hosting" signal rather than a
                // "we don't know" one.
                match claimed_machine {
                    Some(m) if m == my_name => RoleDetail {
                        state: RoleState::Stale,
                        detail: "host-state.json claims THIS machine but the front door isn't hosting — likely a crashed-then-cleared claim.".into(),
                    },
                    Some(m) => RoleDetail {
                        state: RoleState::Unavailable,
                        detail: format!(
                            "No host reachable. host-state.json claims {m}, but it's offline."
                        ),
                    },
                    None => RoleDetail {
                        state: RoleState::Idle,
                        detail: "No claim on record — nobody has ever hosted.".into(),
                    },
                }
            }
        };
    }

    // ── Fallback branch: front door unreachable ─────────────────────
    // This is the pre-front-door path, unchanged, except that we tell
    // the user in `detail` that the front door is down. It's OK to be
    // here on first boot (front door hasn't launched yet) or during a
    // Manager-launched-first sequence — the GUI should not treat this
    // as an error.
    let running = process.is_some();
    let proc_role = process.map(|p| &p.role);

    if running && proc_role == Some(&ProcRole::Arbitrating) {
        return RoleDetail {
            state: RoleState::Starting,
            detail: "Arbitrating host/client role — this takes a few seconds…".into(),
        };
    }

    let Some(claimed) = claimed_machine else {
        return RoleDetail {
            state: RoleState::Idle,
            detail: "No claim on record — nobody has ever hosted.".into(),
        };
    };

    if claimed == my_name {
        if running && proc_role == Some(&ProcRole::Host) {
            return RoleDetail {
                state: RoleState::Host,
                detail: "This machine is currently serving ChatBucket (front door unreachable — falling back to OS scan).".into(),
            };
        }
        return RoleDetail {
            state: RoleState::Stale,
            detail: "host-state.json claims this machine as host, but no live server process was found — it likely crashed right after claiming. Click Start to relaunch.".into(),
        };
    }

    if running && proc_role == Some(&ProcRole::Client) {
        return RoleDetail {
            state: RoleState::Client,
            detail: format!("Host is currently: {}", claimed),
        };
    }
    if running && proc_role == Some(&ProcRole::Host) {
        return RoleDetail {
            state: RoleState::Conflict,
            detail: format!(
                "host-state.json claims {} as host, but THIS machine is ALSO running as host locally — should not happen under normal arbitration; treat as a bug.",
                claimed
            ),
        };
    }
    RoleDetail {
        state: RoleState::Client,
        detail: format!("Host is currently: {} (front door unreachable, no local server running).", claimed),
    }
}

// ── Tests ────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::front_door_client::Routing;

    fn fd(routing: Routing, child_running: bool, starting: bool) -> FrontDoorStatus {
        FrontDoorStatus {
            routing,
            machine: "archlinux".into(),
            child_running,
            child_pid: if child_running { Some(999) } else { None },
            starting,
            last_error: None,
            auto_host: true,
            take_host_on_crash: false,
        }
    }

    fn hs_start(m: &str) -> Result<Option<HostState>, HostStateError> {
        Ok(Some(HostState {
            action: "start".into(),
            machine: m.into(),
            timestamp: "2026-01-01T00:00:00.000000Z".into(),
        }))
    }

    // ── §4.2 table: every row of the front-door-reachable branch ────

    #[test]
    fn role_map_local_child_up_is_host() {
        let s = derive_role_state(
            &hs_start("archlinux"),
            Some("archlinux"),
            "archlinux",
            Some(&fd(Routing::Local, true, false)),
            None,
        );
        assert_eq!(s.state, RoleState::Host);
    }

    #[test]
    fn role_map_local_child_down_is_starting() {
        // Local pointer but child hasn't bound yet — the brief gap
        // between claim-decided and 5001-listening.
        let s = derive_role_state(
            &hs_start("archlinux"),
            Some("archlinux"),
            "archlinux",
            Some(&fd(Routing::Local, false, false)),
            None,
        );
        assert_eq!(s.state, RoleState::Starting);
    }

    #[test]
    fn role_map_redirect_is_redirect() {
        let s = derive_role_state(
            &hs_start("archlinux"),
            Some("archlinux"),
            "win1",
            Some(&fd(Routing::Redirect("archlinux".into()), false, false)),
            None,
        );
        assert_eq!(s.state, RoleState::Redirect);
        assert!(s.detail.contains("archlinux"));
    }

    #[test]
    fn role_map_unavailable_no_claim_is_idle() {
        let s = derive_role_state(
            &Ok(None),
            None,
            "archlinux",
            Some(&fd(Routing::Unavailable, false, false)),
            None,
        );
        assert_eq!(s.state, RoleState::Idle);
    }

    #[test]
    fn role_map_unavailable_with_claim_someone_else_is_unavailable() {
        let s = derive_role_state(
            &hs_start("win1"),
            Some("win1"),
            "archlinux",
            Some(&fd(Routing::Unavailable, false, false)),
            None,
        );
        assert_eq!(s.state, RoleState::Unavailable);
        assert!(s.detail.contains("win1"));
    }

    #[test]
    fn role_map_unavailable_with_self_claim_is_stale() {
        // We used to host but the front door isn't hosting now — that's
        // the "crashed then cleared" case that only STALE captures.
        let s = derive_role_state(
            &hs_start("archlinux"),
            Some("archlinux"),
            "archlinux",
            Some(&fd(Routing::Unavailable, false, false)),
            None,
        );
        assert_eq!(s.state, RoleState::Stale);
    }

    #[test]
    fn role_map_starting_flag_wins_over_routing() {
        // If starting=true, we must not read Local/Redirect literally —
        // the front door itself is between decisions.
        let s = derive_role_state(
            &hs_start("archlinux"),
            Some("archlinux"),
            "archlinux",
            Some(&fd(Routing::Local, true, true)),
            None,
        );
        assert_eq!(s.state, RoleState::Starting);
    }

    #[test]
    fn role_map_corrupt_host_state_is_unknown() {
        let hs: Result<Option<HostState>, HostStateError> =
            Err(HostStateError::InvalidAction {
                path: "x".into(),
                action: "wat".into(),
            });
        let s = derive_role_state(
            &hs,
            None,
            "archlinux",
            Some(&fd(Routing::Local, true, false)),
            None,
        );
        assert_eq!(s.state, RoleState::Unknown);
    }

    // ── §4.2 fallback branch — front door down, process scan drives ──

    #[test]
    fn fallback_no_process_no_claim_is_idle() {
        let s = derive_role_state(&Ok(None), None, "archlinux", None, None);
        assert_eq!(s.state, RoleState::Idle);
    }

    #[test]
    fn fallback_self_claim_no_process_is_stale() {
        let s = derive_role_state(
            &hs_start("archlinux"),
            Some("archlinux"),
            "archlinux",
            None,
            None,
        );
        assert_eq!(s.state, RoleState::Stale);
    }

    #[test]
    fn fallback_someone_else_claim_no_process_is_client() {
        let s = derive_role_state(&hs_start("win1"), Some("win1"), "archlinux", None, None);
        assert_eq!(s.state, RoleState::Client);
        assert!(s.detail.contains("front door unreachable"));
    }

    #[test]
    fn fallback_arbitrating_process_is_starting() {
        let p = ProcessInfo {
            pid: 1,
            role: ProcRole::Arbitrating,
            subshape: SubShape::Arbitrating,
            source: ProcessSource::ProcessScan,
        };
        let s = derive_role_state(&Ok(None), None, "archlinux", None, Some(&p));
        assert_eq!(s.state, RoleState::Starting);
    }

    // ── §6: sync-aggregate rail row ────────────────────────────────

    #[test]
    fn aggregate_ignores_disabled_folders() {
        let mut snap = SyncSnapshot::default();
        for def in resources::RESOURCES {
            snap.folders.push(FolderView {
                folder_id: def.folder_id.to_string(),
                label: def.label,
                managed: def.folder_id != resources::ID_STICKERS, // stickers disabled
                health: if def.folder_id == resources::ID_STICKERS {
                    FolderHealth::Disabled
                } else {
                    FolderHealth::InSync
                },
                need_files: 0,
                need_bytes: 0,
                pull_errors: 0,
                error_count: 0,
                last_scan: String::new(),
                has_conflict: false,
            });
        }
        let agg = snap.aggregate_health();
        // 7 folders total, 1 disabled → denominator is 6, all healthy.
        assert_eq!(agg.total, 6);
        assert_eq!(agg.healthy, 6);
        assert!(matches!(agg.worst, FolderHealth::InSync));
    }

    #[test]
    fn aggregate_worst_wins() {
        let mut snap = SyncSnapshot::default();
        for def in resources::RESOURCES {
            let health = match def.folder_id {
                x if x == resources::ID_STATE => FolderHealth::Conflict,
                x if x == resources::ID_UPLOADS => FolderHealth::Syncing {
                    need_files: 3,
                    need_bytes: 1024,
                },
                _ => FolderHealth::InSync,
            };
            snap.folders.push(FolderView {
                folder_id: def.folder_id.to_string(),
                label: def.label,
                managed: true,
                health,
                need_files: 0,
                need_bytes: 0,
                pull_errors: 0,
                error_count: 0,
                last_scan: String::new(),
                has_conflict: false,
            });
        }
        let agg = snap.aggregate_health();
        assert_eq!(agg.total, 7);
        assert_eq!(agg.healthy, 5); // 7 - 1 conflict - 1 syncing
        assert!(matches!(agg.worst, FolderHealth::Conflict));
    }
}
