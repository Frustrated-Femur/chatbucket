//! manager.rs — ManagerContext: single source of truth for all status/action logic.
//!
//! Direct port of Python `ManagerApi` + `_derive_role_state()`. Same discipline:
//!
//!   * `derive_role_state()` is the ONLY reducer of (host_state, claimed, my_name,
//!     process_info) into a Role badge. Called by BOTH the window path and the
//!     tray path — never re-derived independently (§16.5 "one source of truth").
//!   * `start()` blocks until the launched instance resolves to a real role
//!     (host/client) or START_GRACE_SECONDS elapses. Same act-then-verify
//!     discipline as Python's version.
//!   * `stop()` handles gunicorn master/worker split: resolves the discovered
//!     pid to the master, signals the process group (killpg on POSIX), verifies
//!     the WHOLE lifecycle group is gone, falls back to force-kill only after
//!     STOP_GRACE_SECONDS with a visible warning. Writes {"action":"stop"} to
//!     host-state.json after verified exit — the "single writer" pattern the
//!     Python code adopted to avoid racing gunicorn's own SIGTERM handler.

use crate::arbitration::{self, ArbitrationError, PeerList};
use crate::host_state::{self, HostState, HostStateError};
use crate::process_scan::{self, ChatBucketProc, ProcRole, SubShape};
use crate::repo;
use crate::syncthing::{self, SyncthingState};
use crate::update::{self, GithubRelease};

use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

pub const STOP_GRACE_SECONDS: u64 = 10;
pub const START_GRACE_SECONDS: u64 = 12;

// ── Role-state vocabulary (mirrors Python `_derive_role_state()`) ───────

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RoleState {
    Host,
    Client,
    Idle,
    Stale,
    Conflict,
    Starting,
    Unknown,
}

impl RoleState {
    pub fn key(&self) -> &'static str {
        match self {
            RoleState::Host => "host",
            RoleState::Client => "client",
            RoleState::Idle => "idle",
            RoleState::Stale => "stale",
            RoleState::Conflict => "conflict",
            RoleState::Starting => "starting",
            RoleState::Unknown => "unknown",
        }
    }
    pub fn label(&self) -> &'static str {
        match self {
            RoleState::Host => "HOST",
            RoleState::Client => "CLIENT",
            RoleState::Idle => "IDLE",
            RoleState::Stale => "STALE CLAIM",
            RoleState::Conflict => "CONFLICT",
            RoleState::Starting => "STARTING",
            RoleState::Unknown => "UNKNOWN",
        }
    }
    /// Hex color from the same palette as web/index.html's :root vars.
    pub fn color_hex(&self) -> &'static str {
        match self {
            RoleState::Host => "#4ade80",     // --success
            RoleState::Client => "#f5f5f5",   // --text
            RoleState::Starting => "#b5b5b5", // --text-2
            RoleState::Idle => "#7a7a7a",     // --text-3
            RoleState::Stale => "#fbbf24",    // --warn
            RoleState::Conflict | RoleState::Unknown => "#ff6b6b", // --danger
        }
    }
}

#[derive(Debug, Clone)]
pub struct RoleDetail {
    pub state: RoleState,
    pub detail: String,
}

// ── Combined snapshot the UI renders every tick ────────────────────────

#[derive(Debug, Clone)]
pub struct StatusSnapshot {
    pub my_name: String,
    pub host_state: Result<Option<HostState>, String>,
    pub claimed_machine: Option<String>,
    pub claimed_reachability: Option<ClaimedReachability>,
    pub tailnet_peers: Result<PeerList, String>,
    pub process: Option<ProcessInfo>,
    pub role: RoleDetail,
    pub version: Option<String>,
    pub syncthing: SyncthingState,
}

#[derive(Debug, Clone)]
pub enum ClaimedReachability {
    IsSelf,
    Online,
    Offline,
    Error(String),
}

#[derive(Debug, Clone)]
pub struct ProcessInfo {
    pub pid: u32,
    pub role: ProcRole,
    pub subshape: SubShape,
}

// ── Action outcomes ────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct ActionResult {
    pub ok: bool,
    pub action: &'static str, // "started" / "graceful" / "forced" / "none" / "error"
    pub detail: String,
}

#[derive(Debug, Clone)]
pub struct UpdateCheckResult {
    pub ok: bool,
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

// ── ManagerContext: shared, thread-safe ────────────────────────────────

pub struct ManagerContext {
    pub repo_root: PathBuf,
    pub my_name: String,
    last_update_check: Mutex<Option<GithubRelease>>,
}

impl ManagerContext {
    pub fn new(repo_root: PathBuf) -> Self {
        let my_name = detect_machine_name();
        Self {
            repo_root,
            my_name,
            last_update_check: Mutex::new(None),
        }
    }

    pub fn get_status(&self) -> StatusSnapshot {
        let hs_result = host_state::read_state(&self.repo_root);
        let (host_state_ui, claimed_machine) = match &hs_result {
            Ok(Some(state)) => (Ok(Some(state.clone())), Some(state.machine.clone())),
            Ok(None) => (Ok(None), None),
            Err(e) => (Err(e.to_string()), None),
        };

        let process_info = process_scan::find_chatbucket_process(&self.repo_root)
            .map(|p| ProcessInfo {
                pid: p.pid,
                role: p.role,
                subshape: p.subshape,
            });

        let role = derive_role_state(&hs_result, claimed_machine.as_deref(), &self.my_name, process_info.as_ref());

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

        let tailnet_peers = arbitration::list_tailnet_peers().map_err(|e: ArbitrationError| e.to_string());

        StatusSnapshot {
            my_name: self.my_name.clone(),
            host_state: host_state_ui,
            claimed_machine,
            claimed_reachability,
            tailnet_peers,
            process: process_info,
            role,
            version: update::read_local_version(&self.repo_root),
            syncthing: syncthing::get_status(&self.repo_root),
        }
    }

    /// Cheap subset for the tray icon's 5s poll — host_state + local process
    /// scan only, no tailscale CLI shellout. Same intent as Python's
    /// `_tray_role_state()`.
    pub fn get_role_only(&self) -> RoleDetail {
        let hs = host_state::read_state(&self.repo_root);
        let claimed = match &hs {
            Ok(Some(s)) => Some(s.machine.clone()),
            _ => None,
        };
        let proc = process_scan::find_chatbucket_process(&self.repo_root).map(|p| ProcessInfo {
            pid: p.pid,
            role: p.role,
            subshape: p.subshape,
        });
        derive_role_state(&hs, claimed.as_deref(), &self.my_name, proc.as_ref())
    }

    // ── start() ─────────────────────────────────────────────────────

    pub fn start(&self) -> ActionResult {
        process_scan::kill_stale_arbitrators(&self.repo_root);

        if let Some(existing) = process_scan::find_chatbucket_process(&self.repo_root) {
            return ActionResult {
                ok: true,
                action: "none",
                detail: format!(
                    "Already running (pid {}, role: {})",
                    existing.pid,
                    existing.role.as_str()
                ),
            };
        }

        let python = venv_python(&self.repo_root);
        let main_py = self.repo_root.join("main.py");
        if !python.exists() {
            return ActionResult {
                ok: false,
                action: "error",
                detail: format!("venv Python not found at {}", python.display()),
            };
        }
        if !main_py.exists() {
            return ActionResult {
                ok: false,
                action: "error",
                detail: format!("main.py not found at {}", main_py.display()),
            };
        }

        let child = spawn_main_py(&python, &main_py, &self.my_name, &self.repo_root);
        let pid = match child {
            Ok(c) => c.id(),
            Err(e) => {
                return ActionResult {
                    ok: false,
                    action: "error",
                    detail: format!("Failed to launch: {e}"),
                }
            }
        };

        let resolved = wait_for_role(&self.repo_root, pid, START_GRACE_SECONDS);
        match resolved {
            Some(role) => ActionResult {
                ok: true,
                action: "started",
                detail: format!(
                    "Launched and confirmed as {} (pid {})",
                    role.as_str().to_uppercase(),
                    pid
                ),
            },
            None => {
                // One more scan in case a race put a matching process there
                // under a different pid (gunicorn worker fork, supervisor).
                if let Some(final_proc) = process_scan::find_chatbucket_process(&self.repo_root) {
                    if final_proc.role != ProcRole::Arbitrating {
                        return ActionResult {
                            ok: true,
                            action: "started",
                            detail: format!(
                                "Launched and confirmed as {} (pid {})",
                                final_proc.role.as_str().to_uppercase(),
                                final_proc.pid
                            ),
                        };
                    }
                }
                ActionResult {
                    ok: true,
                    action: "started_unconfirmed",
                    detail: format!(
                        "Launched (pid {}) but couldn't confirm HOST/CLIENT within {}s — \
                         may still be arbitrating. Check again shortly.",
                        pid, START_GRACE_SECONDS
                    ),
                }
            }
        }
    }

    // ── stop() ──────────────────────────────────────────────────────

    pub fn stop(&self) -> ActionResult {
        let Some(proc) = process_scan::find_chatbucket_process(&self.repo_root) else {
            process_scan::kill_stale_arbitrators(&self.repo_root);
            self.mark_stopped_if_mine();
            return ActionResult {
                ok: true,
                action: "none",
                detail: "ChatBucket is not running.".into(),
            };
        };

        let target_pid = if proc.subshape == SubShape::GunicornWorker {
            process_scan::resolve_master(proc.pid, &self.repo_root)
        } else {
            proc.pid
        };

        // Every lifecycle pid discoverable globally, so verification confirms
        // the WHOLE group is down — matches Python's `related` set.
        let related_pids: Vec<u32> = process_scan::iter_chatbucket_procs(&self.repo_root)
            .into_iter()
            .map(|p| p.pid)
            .collect::<std::collections::HashSet<_>>()
            .into_iter()
            .chain(std::iter::once(target_pid))
            .collect();

        // ── graceful signal ───────────────────────────────────────
        let graceful_label = self.send_graceful_signal(target_pid);

        if process_scan::wait_for_all_gone(&related_pids, STOP_GRACE_SECONDS) {
            process_scan::kill_stale_arbitrators(&self.repo_root);
            self.mark_stopped_if_mine();
            return ActionResult {
                ok: true,
                action: "graceful",
                detail: format!(
                    "Stopped via {} (target pid {}).",
                    graceful_label, target_pid
                ),
            };
        }

        // ── force stop path ──────────────────────────────────────
        // Kill the master first so it stops respawning workers, then the rest.
        process_scan::force_kill_pid(target_pid);
        for pid in &related_pids {
            if *pid != target_pid {
                process_scan::force_kill_pid(*pid);
            }
        }
        let force_ok = process_scan::wait_for_all_gone(&related_pids, 5);
        process_scan::kill_stale_arbitrators(&self.repo_root);
        if force_ok {
            self.mark_stopped_if_mine();
        }
        ActionResult {
            ok: force_ok,
            action: "forced",
            detail: format!(
                "Graceful stop via {} did not complete within {}s — force-stopped \
                 {} process(es). host-state.json may be stale until another \
                 machine's health check catches it.",
                graceful_label,
                STOP_GRACE_SECONDS,
                related_pids.len()
            ),
        }
    }

    #[cfg(unix)]
    fn send_graceful_signal(&self, target_pid: u32) -> String {
        if let Some(pgid) = process_scan::get_pgid(target_pid) {
            if process_scan::send_sigterm_pgid(pgid) {
                return format!("SIGTERM to pgid {}", pgid);
            }
        }
        if process_scan::send_sigterm_pid(target_pid) {
            "SIGTERM".into()
        } else {
            "SIGTERM (send failed)".into()
        }
    }

    #[cfg(windows)]
    fn send_graceful_signal(&self, target_pid: u32) -> String {
        if process_scan::send_ctrl_break_windows(target_pid) {
            "CTRL_BREAK_EVENT".into()
        } else {
            "CTRL_BREAK_EVENT (call failed)".into()
        }
    }

    fn mark_stopped_if_mine(&self) {
        let Ok(Some(state)) = host_state::read_state(&self.repo_root) else {
            return;
        };
        if state.machine != self.my_name || state.action == "stop" {
            return;
        }
        if let Err(e) = host_state::write_state(&self.repo_root, "stop", &self.my_name) {
            log::warn!("could not mark stop in host-state.json: {e}");
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
        // Re-check every time (release could be yanked between clicks).
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
                detail: "Release ZIP contained no allow-listed files — treated as broken; rolled back.".into(),
            };
        }

        if let Err(e) = update::sanity_check_extracted(&self.repo_root) {
            let _ = update::restore_backup(&self.repo_root);
            return UpdateInstallResult {
                ok: false,
                step: "verify",
                detail: format!("New code failed sanity check ({e}). Rolled back to previous version."),
            };
        }

        let _ = std::fs::remove_dir_all(self.repo_root.join(update::BACKUP_DIR));

        let new_v = update::read_local_version(&self.repo_root).unwrap_or(release.tag_name);
        UpdateInstallResult {
            ok: true,
            step: "done",
            detail: format!(
                "Updated to {}. Click Start to relaunch — restart goes through arbitration on purpose, not straight back into the prior role.",
                new_v
            ),
        }
    }
}

// ── Free-standing helpers ─────────────────────────────────────────────

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

pub fn venv_python(repo_root: &std::path::Path) -> PathBuf {
    #[cfg(windows)]
    {
        repo_root.join(".venv").join("Scripts").join("python.exe")
    }
    #[cfg(not(windows))]
    {
        repo_root.join(".venv").join("bin").join("python")
    }
}

fn spawn_main_py(
    python: &std::path::Path,
    main_py: &std::path::Path,
    machine: &str,
    cwd: &std::path::Path,
) -> std::io::Result<std::process::Child> {
    let mut cmd = Command::new(python);
    cmd.arg(main_py)
        .arg(machine)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    #[cfg(unix)]
    {
        // New session so a SIGTERM aimed at our pid can, if needed, be
        // turned into a process-group signal without also signalling the
        // Manager itself. Same intent as Python's start_new_session=True.
        use std::os::unix::process::CommandExt;
        unsafe {
            cmd.pre_exec(|| {
                // setsid() — become session leader, new process group.
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NEW_PROCESS_GROUP — required for later CTRL_BREAK_EVENT.
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        cmd.creation_flags(CREATE_NEW_PROCESS_GROUP);
    }

    cmd.spawn()
}

fn wait_for_role(repo_root: &std::path::Path, pid: u32, timeout_secs: u64) -> Option<ProcRole> {
    let deadline = std::time::Instant::now() + Duration::from_secs(timeout_secs);
    let poll = Duration::from_millis(400);
    loop {
        if let Some(proc) = process_scan::find_chatbucket_process(repo_root) {
            if proc.role != ProcRole::Arbitrating {
                // Accept the same pid, or any resolved role — the launched
                // main.py may have execv'd into gunicorn (pid preserved) or
                // gunicorn's master may have forked a worker (child pid).
                if proc.pid == pid || pid_close_relative(pid, proc.pid) {
                    return Some(proc.role);
                }
                // Even for an unrelated pid: if it's the only ChatBucket
                // process we can see and it just appeared, accept it.
                return Some(proc.role);
            }
        }
        if std::time::Instant::now() >= deadline {
            return None;
        }
        std::thread::sleep(poll);
    }
}

fn pid_close_relative(a: u32, b: u32) -> bool {
    // If both pids are alive and appeared close together in time, treat as
    // "same lifecycle group". Mirrors Python's create_time proximity check.
    let mut sys = sysinfo::System::new();
    sys.refresh_processes();
    let ta = sys
        .process(sysinfo::Pid::from_u32(a))
        .map(|p| p.start_time());
    let tb = sys
        .process(sysinfo::Pid::from_u32(b))
        .map(|p| p.start_time());
    match (ta, tb) {
        (Some(ta), Some(tb)) => (ta as i64 - tb as i64).unsigned_abs() < START_GRACE_SECONDS,
        _ => false,
    }
}

pub fn derive_role_state(
    hs: &Result<Option<HostState>, HostStateError>,
    claimed_machine: Option<&str>,
    my_name: &str,
    process: Option<&ProcessInfo>,
) -> RoleDetail {
    if hs.is_err() {
        return RoleDetail {
            state: RoleState::Unknown,
            detail: "host-state.json is corrupted.".into(),
        };
    }

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
                detail: "This machine is currently serving ChatBucket.".into(),
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
        detail: format!("Host is currently: {} (no local doorman running).", claimed),
    }
}
