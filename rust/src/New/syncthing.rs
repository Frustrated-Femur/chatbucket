//! syncthing.rs — ChatBucket-oriented Syncthing controller.
//!
//! This module is the ONLY place that talks to Syncthing's REST API and the
//! ONLY place that knows Syncthing's config JSON shape. The GUI and the
//! Manager operate in ChatBucket terms (resources, devices, requests) and
//! never construct REST URLs or Syncthing JSON themselves (§14/§25).
//!
//! Design contract implemented here (manager-syncthing-setup_4.md):
//!
//!   * Connection states are explicit and distinct (§3): NotConfigured,
//!     AuthFailed, Unreachable, InvalidResponse, Connected — never collapsed
//!     into one generic "couldn't connect".
//!   * Installation/runtime detection is explicit (§2 / task §11):
//!     NotInstalled vs InstalledNotRunning vs RunningUnreachable vs Reachable.
//!   * API-key auto-discovery reads the LOCAL Syncthing config.xml when the
//!     Manager and Syncthing run as the same user (§2/§3); manual paste is
//!     the documented fallback.
//!   * ALL config writes are granular — folder/device-scoped endpoints
//!     (§16.5 granular-write rule). NEVER whole-config read-modify-write.
//!   * After every write we check /rest/config/restart-required and, if set,
//!     restart Syncthing, wait for it to come back, and re-verify (§16.5).
//!   * Pending device/folder detection is EVENT-DRIVEN: the event stream
//!     (PendingDevicesChanged / PendingFoldersChanged) is the trigger, and a
//!     fresh read of /rest/cluster/pending/* is the source of truth (§17).
//!   * Only ChatBucket-owned resources (the seven fixed IDs) are ever
//!     written; unrelated user folders/devices are left strictly alone (§11).
//!
//! Failure modes carried over from the original probe (FM-1..FM-10) still
//! hold; the full controller adds FM-11..FM-16 below for the new surface.
//!
//! Security (§3 / task §10): the API key is NEVER logged, never included in
//! any error string, never returned in diagnostics. `config.xml` reads and
//! `manager_config.json` writes apply owner-only permissions on Unix.

use crate::resources::{self, ManagedState, ResourceDef};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

const HTTP_TIMEOUT: Duration = Duration::from_secs(8);
const EVENT_LONG_POLL: Duration = Duration::from_secs(30);
const RESTART_WAIT_TIMEOUT: Duration = Duration::from_secs(30);
pub const CONFIG_FILE: &str = "manager_config.json";
pub const DEFAULT_URL: &str = "http://127.0.0.1:8384";
pub const SYNCTHING_INSTALL_URL: &str = "https://syncthing.net/downloads/";

// ── Manager config (API key + URL + managed flags) ─────────────────────
//
// Kept here (not in resources.rs) because this is the credential-bearing
// file and the read/write/permission discipline lives next to the API that
// consumes the credential. `managed` is Manager-owned state persisted in the
// same file so it survives restarts and is not transient UI memory (§16.5).

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ManagerConfig {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub syncthing_api_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub syncthing_url: Option<String>,
    /// Where the key came from, for honest UI ("auto-discovered" vs manual).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub api_key_source: Option<String>,
    /// Per-resource managed flags (§16.5). Missing keys default to true.
    #[serde(default)]
    pub managed: ManagedState,
}

impl ManagerConfig {
    pub fn key(&self) -> Option<&str> {
        self.syncthing_api_key
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
    }
    pub fn url(&self) -> String {
        self.syncthing_url
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or(DEFAULT_URL)
            .trim_end_matches('/')
            .to_string()
    }
}

fn config_path(repo_root: &Path) -> PathBuf {
    repo_root.join(CONFIG_FILE)
}

/// Read the config. Returns `(config, parse_error_message)`. A missing file
/// yields (default(), None); a malformed file yields (default(), Some(msg))
/// so callers render BadConfig instead of silently downgrading (FM-2).
pub fn read_config(repo_root: &Path) -> (ManagerConfig, Option<String>) {
    let path = config_path(repo_root);
    let raw = match fs::read_to_string(&path) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return (ManagerConfig::default(), None)
        }
        Err(e) => return (ManagerConfig::default(), Some(format!("read: {e}"))),
    };
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return (ManagerConfig::default(), None);
    }
    match serde_json::from_str::<ManagerConfig>(trimmed) {
        Ok(mut cfg) => {
            cfg.managed.fill_defaults();
            (cfg, None)
        }
        Err(e) => (ManagerConfig::default(), Some(e.to_string())),
    }
}

/// Atomic write with owner-only permissions on the credential file (§3).
/// Same tempfile+fsync+rename discipline as host_state.py (FM-10).
pub fn save_config(repo_root: &Path, mut cfg: ManagerConfig) -> Result<ManagerConfig, String> {
    cfg.syncthing_api_key = cfg
        .syncthing_api_key
        .map(|s| {
            let s = s.trim();
            let s = s.trim_matches(|c: char| c == '"' || c == '\'');
            s.trim().to_string()
        })
        .filter(|s| !s.is_empty());
    cfg.syncthing_url = cfg
        .syncthing_url
        .map(|s| s.trim().trim_end_matches('/').to_string())
        .filter(|s| !s.is_empty());

    let path = config_path(repo_root);
    let dir = path
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));
    fs::create_dir_all(&dir).map_err(|e| format!("create dir: {e}"))?;

    let payload = serde_json::to_string_pretty(&cfg).map_err(|e| format!("serialize: {e}"))?;

    let tmp = tempfile::Builder::new()
        .prefix(".manager_config-")
        .suffix(".tmp")
        .tempfile_in(&dir)
        .map_err(|e| format!("tempfile: {e}"))?;
    {
        let mut f = tmp.as_file();
        f.write_all(payload.as_bytes())
            .map_err(|e| format!("write: {e}"))?;
        f.sync_all().map_err(|e| format!("fsync: {e}"))?;
    }
    tmp.persist(&path)
        .map_err(|e| format!("rename: {}", e.error))?;

    // §3: credential file must not inherit permissive default permissions.
    set_owner_only_permissions(&path);

    invalidate_cache();
    Ok(cfg)
}

/// Best-effort owner-read/write-only permissions (0600) on Unix. On Windows
/// the file ACL is governed by the user's profile directory; nothing to do.
#[cfg(unix)]
fn set_owner_only_permissions(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
}
#[cfg(not(unix))]
fn set_owner_only_permissions(_path: &Path) {}

impl ManagedState {
    fn fill_defaults(&mut self) {
        for r in resources::RESOURCES {
            self.map.entry(r.folder_id.to_string()).or_insert(true);
        }
    }
}

// ── Connection & installation/runtime state (task §11) ─────────────────

/// Connection state to the Syncthing REST API (§3). Each variant maps to a
/// DIFFERENT user action — never collapse these into one generic error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConnectionState {
    /// No usable API key available (discovery failed AND nothing pasted).
    NotConfigured,
    /// API answered and accepted the key.
    Connected,
    /// Syncthing rejected the key (401/403) — stale key, re-run discovery.
    AuthFailed,
    /// Nothing is listening / connection refused — Syncthing likely down.
    Unreachable(String),
    /// Something answered on the port but it isn't the Syncthing API.
    InvalidResponse(String),
    /// manager_config.json is malformed.
    BadConfig(String),
}

impl ConnectionState {
    pub fn label(&self) -> &'static str {
        match self {
            ConnectionState::NotConfigured => "Not connected",
            ConnectionState::Connected => "Connected",
            ConnectionState::AuthFailed => "Authentication rejected",
            ConnectionState::Unreachable(_) => "Syncthing unreachable",
            ConnectionState::InvalidResponse(_) => "Invalid response",
            ConnectionState::BadConfig(_) => "Bad config",
        }
    }
    pub fn is_connected(&self) -> bool {
        matches!(self, ConnectionState::Connected)
    }
}

/// Whether Syncthing the PROGRAM is present and running (task §11).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InstallState {
    /// No syncthing executable found in PATH.
    NotInstalled,
    /// Executable exists but no syncthing process is running.
    InstalledNotRunning,
    /// A process is running but the API is not answering yet.
    RunningNotReady,
    /// Process running and API reachable.
    Running,
    /// Could not determine (process scan unavailable etc.).
    Unknown,
}

impl InstallState {
    pub fn label(&self) -> &'static str {
        match self {
            InstallState::NotInstalled => "Not installed",
            InstallState::InstalledNotRunning => "Installed (not running)",
            InstallState::RunningNotReady => "Starting…",
            InstallState::Running => "Running",
            InstallState::Unknown => "Unknown",
        }
    }
}

/// Detect the syncthing executable in PATH (cross-platform).
pub fn syncthing_executable() -> Option<PathBuf> {
    let exe = if cfg!(windows) {
        "syncthing.exe"
    } else {
        "syncthing"
    };
    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        let cand = dir.join(exe);
        if cand.is_file() {
            return Some(cand);
        }
    }
    None
}

/// Is a syncthing process currently running? Cross-platform via sysinfo.
pub fn syncthing_process_running() -> bool {
    use sysinfo::{ProcessRefreshKind, RefreshKind, System};
    let mut sys = System::new_with_specifics(
        RefreshKind::new().with_processes(ProcessRefreshKind::everything()),
    );
    sys.refresh_processes();
    sys.processes().values().any(|p| {
        let name = p.name().to_lowercase();
        let name = name.trim_end_matches(".exe");
        name == "syncthing"
    })
}

/// Best-effort launch of the local syncthing daemon (task §11).
pub fn launch_syncthing() -> Result<String, String> {
    let exe = syncthing_executable().ok_or("syncthing executable not found in PATH")?;
    let mut cmd = std::process::Command::new(exe);
    cmd.arg("serve")
        .arg("--no-browser")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            cmd.pre_exec(|| {
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
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        cmd.creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS);
    }
    cmd.spawn()
        .map(|c| format!("launched syncthing (pid {})", c.id()))
        .map_err(|e| format!("could not launch syncthing: {e}"))
}

/// Candidate locations of Syncthing's config.xml for the CURRENT user,
/// cross-platform. Used for automatic API-key discovery (§2/§3).
pub fn local_syncthing_config_paths() -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    #[cfg(windows)]
    {
        if let Some(local) = std::env::var_os("LOCALAPPDATA") {
            out.push(PathBuf::from(&local).join("Syncthing").join("config.xml"));
        }
        if let Some(appdata) = std::env::var_os("APPDATA") {
            out.push(PathBuf::from(&appdata).join("Syncthing").join("config.xml"));
        }
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        if let Some(xdg) = std::env::var_os("XDG_CONFIG_HOME") {
            out.push(PathBuf::from(&xdg).join("syncthing").join("config.xml"));
        }
        if let Some(home) = std::env::var_os("HOME") {
            out.push(
                PathBuf::from(&home)
                    .join(".config")
                    .join("syncthing")
                    .join("config.xml"),
            );
            // Older / default layout.
            out.push(PathBuf::from(&home).join(".syncthing").join("config.xml"));
        }
    }
    #[cfg(target_os = "macos")]
    {
        if let Some(home) = std::env::var_os("HOME") {
            out.push(
                PathBuf::from(&home)
                    .join("Library")
                    .join("Application Support")
                    .join("Syncthing")
                    .join("config.xml"),
            );
        }
    }
    out
}

/// Extract the API key from a config.xml body. Deliberately a tiny,
/// dependency-free scan rather than a full XML parser — we only need the
/// `<apikey>...</apikey>` element inside `<gui>`.
pub fn parse_api_key_from_config_xml(xml: &str) -> Option<String> {
    let lower = xml.to_lowercase();
    let open = lower.find("<apikey>")?;
    let close = lower.find("</apikey>")?;
    if close <= open {
        return None;
    }
    let start = open + "<apikey>".len();
    let key = xml[start..close].trim();
    if key.is_empty() {
        None
    } else {
        Some(key.to_string())
    }
}

/// Attempt automatic local API-key discovery (§2/§3). Returns the discovered
/// key plus the config path it came from. Never logs the key.
pub fn discover_api_key() -> Option<(String, PathBuf)> {
    for path in local_syncthing_config_paths() {
        if let Ok(xml) = fs::read_to_string(&path) {
            if let Some(key) = parse_api_key_from_config_xml(&xml) {
                return Some((key, path));
            }
        }
    }
    None
}

// ── HTTP plumbing ───────────────────────────────────────────────────────

/// A thin authenticated client over the Syncthing REST API. All requests go
/// through here so the API key is attached in exactly one place and never
/// leaks into error strings or logs.
#[derive(Clone)]
pub struct ApiClient {
    base: String,
    key: String,
    agent: ureq::Agent,
}

/// Low-level transport/HTTP error classification. NEVER carries the key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ApiError {
    Unreachable(String),
    AuthFailed,
    NotFound(String),
    HttpError(u16, String),
    BadJson(String),
}

impl ApiError {
    pub fn to_connection(&self) -> ConnectionState {
        match self {
            ApiError::Unreachable(d) => ConnectionState::Unreachable(d.clone()),
            ApiError::AuthFailed => ConnectionState::AuthFailed,
            ApiError::NotFound(_) => ConnectionState::InvalidResponse("unexpected 404".into()),
            ApiError::HttpError(c, t) => ConnectionState::InvalidResponse(format!("HTTP {c} {t}")),
            ApiError::BadJson(d) => ConnectionState::InvalidResponse(d.clone()),
        }
    }
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ApiError::Unreachable(d) => write!(f, "unreachable: {d}"),
            ApiError::AuthFailed => write!(f, "authentication rejected (401/403)"),
            ApiError::NotFound(r) => write!(f, "not found: {r}"),
            ApiError::HttpError(c, t) => write!(f, "HTTP {c} {t}"),
            ApiError::BadJson(d) => write!(f, "bad JSON: {d}"),
        }
    }
}

impl ApiClient {
    pub fn new(base: &str, key: &str) -> Self {
        let agent = ureq::AgentBuilder::new().timeout(HTTP_TIMEOUT).build();
        Self {
            base: base.trim_end_matches('/').to_string(),
            key: key.to_string(),
            agent,
        }
    }

    fn classify(&self, e: ureq::Error, resource: &str) -> ApiError {
        match e {
            ureq::Error::Status(code, resp) => match code {
                401 | 403 => ApiError::AuthFailed,
                404 => ApiError::NotFound(resource.to_string()),
                _ => ApiError::HttpError(code, resp.status_text().to_string()),
            },
            ureq::Error::Transport(t) => {
                // ureq's transport error carries the URL but not headers, so
                // this never leaks the API key (FM-8).
                ApiError::Unreachable(short_transport(t))
            }
        }
    }

    fn get(&self, path: &str) -> Result<serde_json::Value, ApiError> {
        let url = format!("{}{}", self.base, path);
        self.agent
            .get(&url)
            .set("X-API-Key", &self.key)
            .call()
            .map_err(|e| self.classify(e, path))?
            .into_json::<serde_json::Value>()
            .map_err(|e| ApiError::BadJson(e.to_string()))
    }

    fn get_into<T: for<'de> Deserialize<'de>>(&self, path: &str) -> Result<T, ApiError> {
        let url = format!("{}{}", self.base, path);
        self.agent
            .get(&url)
            .set("X-API-Key", &self.key)
            .call()
            .map_err(|e| self.classify(e, path))?
            .into_json::<T>()
            .map_err(|e| ApiError::BadJson(e.to_string()))
    }

    /// POST/PUT with a JSON body. `method` is "post" | "put" | "patch" | "delete".
    fn send(
        &self,
        method: &str,
        path: &str,
        body: Option<&serde_json::Value>,
    ) -> Result<(), ApiError> {
        let url = format!("{}{}", self.base, path);
        let req = match method {
            "post" => self.agent.post(&url),
            "put" => self.agent.put(&url),
            "patch" => self.agent.request("PATCH", &url),
            "delete" => self.agent.delete(&url),
            _ => return Err(ApiError::BadJson(format!("bad method {method}"))),
        };
        let req = req.set("X-API-Key", &self.key);
        let res = match body {
            Some(b) => req.send_json(b.clone()),
            None => req.call(),
        };
        res.map(|_| ()).map_err(|e| self.classify(e, path))
    }

    /// Long-poll the event stream. Uses a longer timeout than normal calls.
    /// Returns (events, next_since). Errors are returned, not swallowed.
    fn events_since(&self, since: u64, timeout: Duration) -> Result<Vec<Event>, ApiError> {
        let url = format!(
            "{}/rest/events?since={}&timeout={}",
            self.base,
            since,
            timeout.as_secs()
        );
        let agent = ureq::AgentBuilder::new()
            .timeout(timeout + Duration::from_secs(10))
            .build();
        let resp = agent
            .get(&url)
            .set("X-API-Key", &self.key)
            .call()
            .map_err(|e| self.classify(e, "/rest/events"))?;
        resp.into_json::<Vec<Event>>()
            .map_err(|e| ApiError::BadJson(e.to_string()))
    }
}

fn short_transport(t: ureq::Transport) -> String {
    let s = t.to_string();
    s.lines().next().unwrap_or("").to_string()
}

// ── REST data shapes (verified against docs.syncthing.net) ──────────────

#[derive(Debug, Clone, Deserialize)]
pub struct VersionInfo {
    #[serde(default)]
    pub version: String,
    // os/arch are part of the version payload; surfaced for diagnostics.
    #[allow(dead_code)]
    #[serde(default)]
    pub os: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub arch: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SystemStatus {
    #[serde(rename = "myID", default)]
    pub my_id: String,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct DbStatus {
    #[serde(default)]
    pub state: String,
    #[serde(rename = "needFiles", default)]
    pub need_files: i64,
    #[serde(rename = "needBytes", default)]
    pub need_bytes: i64,
    #[allow(dead_code)]
    #[serde(rename = "needTotalItems", default)]
    pub need_total_items: i64,
    #[serde(rename = "pullErrors", default)]
    pub pull_errors: i64,
    #[allow(dead_code)]
    #[serde(rename = "globalFiles", default)]
    pub global_files: i64,
    #[allow(dead_code)]
    #[serde(rename = "localFiles", default)]
    pub local_files: i64,
    #[allow(dead_code)]
    #[serde(rename = "stateChanged", default)]
    pub state_changed: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FolderError {
    #[allow(dead_code)]
    #[serde(default)]
    pub path: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub error: String,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct FolderErrors {
    #[serde(default)]
    pub errors: Vec<FolderError>,
}

#[allow(dead_code)] // completion endpoint shape — reserved for progress view
#[derive(Debug, Clone, Deserialize, Default)]
pub struct Completion {
    #[serde(default)]
    pub completion: f64,
    #[serde(rename = "needBytes", default)]
    pub need_bytes: i64,
    #[serde(rename = "needItems", default)]
    pub need_items: i64,
}

/// One entry from GET /rest/stats/folder, keyed by folder id at the top level.
#[derive(Debug, Clone, Deserialize, Default)]
pub struct FolderStat {
    #[serde(rename = "lastScan", default)]
    pub last_scan: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RestartRequired {
    #[serde(rename = "requiresRestart", default)]
    pub requires_restart: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Event {
    #[serde(default)]
    pub id: u64,
    #[serde(rename = "type", default)]
    pub event_type: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub time: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub data: serde_json::Value,
}

/// Pending device entry: GET /rest/cluster/pending/devices → map keyed by id.
pub type PendingDevices = BTreeMap<String, PendingDeviceInfo>;
#[derive(Debug, Clone, Deserialize, Default)]
pub struct PendingDeviceInfo {
    #[serde(default)]
    pub name: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub time: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub address: String,
}

/// Pending folder entry: GET /rest/cluster/pending/folders → map keyed by
/// folder id, each with an `offeredBy` map keyed by device id.
pub type PendingFolders = BTreeMap<String, PendingFolderOffers>;
#[derive(Debug, Clone, Deserialize, Default)]
pub struct PendingFolderOffers {
    #[serde(rename = "offeredBy", default)]
    pub offered_by: BTreeMap<String, PendingFolderOffer>,
}
#[derive(Debug, Clone, Deserialize, Default)]
pub struct PendingFolderOffer {
    #[allow(dead_code)]
    #[serde(default)]
    pub label: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub time: String,
}

// ── Folder config JSON (the ChatBucket profile) ─────────────────────────
//
// Field names verified against docs.syncthing.net/users/config.html and the
// REST config endpoints. We construct the ChatBucket profile explicitly so
// we never round-trip fields Syncthing doesn't support on this version.

fn folder_config_json(def: &ResourceDef, path: &Path, device_ids: &[String]) -> serde_json::Value {
    use resources::{STAGGERED_MAX_AGE_DAYS, STAGGERED_VERSIONS_PATH};
    let versioning = match def.versioning {
        resources::VersioningPolicy::None => serde_json::json!({ "type": "", "params": {} }),
        resources::VersioningPolicy::Bounded => serde_json::json!({
            "type": "staggered",
            "params": {
                "maxAge": (STAGGERED_MAX_AGE_DAYS * 86400).to_string(),
                "versionsPath": STAGGERED_VERSIONS_PATH,
                "cleanupIntervalS": "3600"
            },
            "cleanupIntervalS": 3600
        }),
    };
    let devices: Vec<serde_json::Value> = device_ids
        .iter()
        .map(|d| serde_json::json!({ "deviceID": d }))
        .collect();
    serde_json::json!({
        "id": def.folder_id,
        "label": def.label,
        "path": path.to_string_lossy(),
        "type": "sendreceive",
        "rescanIntervalS": resources::RESCAN_INTERVAL_SECS,
        "fsWatcherEnabled": true,
        "fsWatcherDelayS": resources::WATCHER_DELAY_SECS,
        "ignorePerms": true,
        "ignoreDelete": false,
        "disableFsync": false,
        "autoNormalize": true,
        "paused": false,
        "devices": devices,
        "versioning": versioning
    })
}

/// Extract the local path a configured folder points at (for collision check).
fn folder_config_path(v: &serde_json::Value) -> Option<String> {
    v.get("path")
        .and_then(|p| p.as_str())
        .map(|s| s.to_string())
}

/// Device IDs currently associated with a folder config object.
fn folder_config_devices(v: &serde_json::Value) -> Vec<String> {
    v.get("devices")
        .and_then(|d| d.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|e| {
                    e.get("deviceID")
                        .and_then(|x| x.as_str())
                        .map(|s| s.to_string())
                })
                .collect()
        })
        .unwrap_or_default()
}

// ── The controller ──────────────────────────────────────────────────────

/// The ChatBucket Syncthing controller. One instance per ManagerContext.
/// All methods are blocking and MUST be called from the worker thread, never
/// from the egui render path (task §26).
pub struct SyncthingController {
    repo_root: PathBuf,
    cache: Mutex<Option<(Instant, ConnectionState)>>,
}

const STATUS_MIN_INTERVAL: Duration = Duration::from_secs(20);

impl SyncthingController {
    pub fn new(repo_root: PathBuf) -> Self {
        Self {
            repo_root,
            cache: Mutex::new(None),
        }
    }

    pub fn invalidate(&self) {
        if let Ok(mut g) = self.cache.lock() {
            *g = None;
        }
    }

    // ── config access ─────────────────────────────────────────────

    pub fn read_config(&self) -> (ManagerConfig, Option<String>) {
        read_config(&self.repo_root)
    }

    pub fn save_config(&self, cfg: ManagerConfig) -> Result<ManagerConfig, String> {
        let r = save_config(&self.repo_root, cfg);
        self.invalidate();
        r
    }

    /// Build an API client from the current on-disk config, or None if no
    /// key is configured.
    fn client(&self) -> Result<ApiClient, ConnectionState> {
        let (cfg, parse_err) = self.read_config();
        if let Some(msg) = parse_err {
            return Err(ConnectionState::BadConfig(msg));
        }
        let Some(key) = cfg.key() else {
            return Err(ConnectionState::NotConfigured);
        };
        Ok(ApiClient::new(&cfg.url(), key))
    }

    // ── detection & connection ────────────────────────────────────

    pub fn install_state(&self) -> InstallState {
        let exe = syncthing_executable().is_some();
        let running = syncthing_process_running();
        match (exe, running) {
            (false, _) => InstallState::NotInstalled,
            (true, false) => InstallState::InstalledNotRunning,
            (true, true) => {
                // Process is up — is the API answering?
                match self.connection_state_uncached() {
                    ConnectionState::Connected => InstallState::Running,
                    ConnectionState::AuthFailed => InstallState::Running, // up, just bad key
                    _ => InstallState::RunningNotReady,
                }
            }
        }
    }

    /// Attempt automatic discovery + connect (§2). Returns the connection
    /// state after the attempt. If discovery finds a key, it is persisted.
    pub fn auto_connect(&self) -> ConnectionState {
        // If we already have a working key, just verify.
        if let Ok(c) = self.client() {
            match self.ping(&c) {
                Ok(()) => return ConnectionState::Connected,
                Err(ApiError::AuthFailed) => { /* fall through to re-discovery */ }
                Err(e) => return e.to_connection(),
            }
        }
        // Try discovery.
        if let Some((key, path)) = discover_api_key() {
            let (mut cfg, _) = self.read_config();
            cfg.syncthing_api_key = Some(key);
            cfg.api_key_source = Some(format!("auto:{}", path.display()));
            if self.save_config(cfg).is_ok() {
                if let Ok(c) = self.client() {
                    return match self.ping(&c) {
                        Ok(()) => ConnectionState::Connected,
                        Err(e) => e.to_connection(),
                    };
                }
            }
        }
        self.connection_state_uncached()
    }

    fn ping(&self, c: &ApiClient) -> Result<(), ApiError> {
        c.get("/rest/system/ping").map(|_| ())
    }

    fn connection_state_uncached(&self) -> ConnectionState {
        match self.client() {
            Err(cs) => cs,
            Ok(c) => match self.ping(&c) {
                Ok(()) => ConnectionState::Connected,
                Err(e) => e.to_connection(),
            },
        }
    }

    /// Cached connection state for the status snapshot (throttled).
    pub fn connection_state(&self) -> ConnectionState {
        {
            let guard = self.cache.lock().unwrap();
            if let Some((when, ref st)) = *guard {
                if when.elapsed() < STATUS_MIN_INTERVAL {
                    return st.clone();
                }
            }
        }
        let st = self.connection_state_uncached();
        let mut guard = self.cache.lock().unwrap();
        *guard = Some((Instant::now(), st.clone()));
        st
    }

    // ── read-only info ────────────────────────────────────────────

    pub fn version(&self) -> Result<VersionInfo, ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        c.get_into::<VersionInfo>("/rest/system/version")
    }

    /// This machine's own Syncthing device ID (shown to the user so they can
    /// share it with a peer). Part of the controller API surface.
    #[allow(dead_code)]
    pub fn local_device_id(&self) -> Result<String, ApiError> {
        let c = self.client().map_err(|e| match e {
            ConnectionState::AuthFailed => ApiError::AuthFailed,
            ConnectionState::Unreachable(d) => ApiError::Unreachable(d),
            _ => ApiError::Unreachable("not configured".into()),
        })?;
        let st: SystemStatus = c.get_into("/rest/system/status")?;
        Ok(st.my_id)
    }

    fn get_folder_config(
        &self,
        c: &ApiClient,
        folder_id: &str,
    ) -> Result<Option<serde_json::Value>, ApiError> {
        match c.get(&format!("/rest/config/folders/{folder_id}")) {
            Ok(v) => Ok(Some(v)),
            Err(ApiError::NotFound(_)) => Ok(None),
            Err(e) => Err(e),
        }
    }

    pub fn folder_status(&self, folder_id: &str) -> Result<DbStatus, ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        c.get_into(&format!("/rest/db/status?folder={folder_id}"))
    }

    pub fn folder_errors(&self, folder_id: &str) -> Result<Vec<FolderError>, ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        let fe: FolderErrors = c.get_into(&format!("/rest/folder/errors?folder={folder_id}"))?;
        Ok(fe.errors)
    }

    pub fn folder_last_scan(&self) -> Result<BTreeMap<String, FolderStat>, ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        c.get_into::<BTreeMap<String, FolderStat>>("/rest/stats/folder")
    }

    /// Whether Syncthing has staged config requiring a restart (§16.5).
    #[allow(dead_code)]
    pub fn restart_required(&self) -> Result<bool, ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        let rr: RestartRequired = c.get_into("/rest/config/restart-required")?;
        Ok(rr.requires_restart)
    }

    // ── granular writes (§16.5) ───────────────────────────────────

    /// POST a single folder config (create or replace) via the granular
    /// endpoint — never a whole-config rewrite.
    fn put_folder(&self, c: &ApiClient, cfg: serde_json::Value) -> Result<(), ApiError> {
        c.send("post", "/rest/config/folders", Some(&cfg))
    }

    fn delete_folder(&self, c: &ApiClient, folder_id: &str) -> Result<(), ApiError> {
        c.send("delete", &format!("/rest/config/folders/{folder_id}"), None)
    }

    fn put_device(
        &self,
        c: &ApiClient,
        device_id: &str,
        name: Option<&str>,
    ) -> Result<(), ApiError> {
        let body = serde_json::json!({
            "deviceID": device_id,
            "name": name.unwrap_or(""),
            "addresses": ["dynamic"]
        });
        c.send("post", "/rest/config/devices", Some(&body))
    }

    fn delete_device(&self, c: &ApiClient, device_id: &str) -> Result<(), ApiError> {
        c.send("delete", &format!("/rest/config/devices/{device_id}"), None)
    }

    // ── ChatBucket operations ─────────────────────────────────────

    /// Ensure one ChatBucket folder exists with the correct path, profile and
    /// device associations. Honors the collision rule (STOP on foreign path).
    /// Returns a per-folder outcome.
    pub fn ensure_folder(
        &self,
        def: &ResourceDef,
        device_ids: &[String],
    ) -> Result<FolderEnsureOutcome, ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        let expected_path = resources::local_path(&self.repo_root, def);
        let existing = self.get_folder_config(&c, def.folder_id)?;

        // Collision rule (§16.5 / task §8): STOP on a foreign path.
        let existing_path = existing.as_ref().and_then(folder_config_path);
        match resources::check_collision(def, existing_path.as_deref(), &self.repo_root) {
            resources::CollisionCheck::Clear => {}
            resources::CollisionCheck::SameInstall => {}
            resources::CollisionCheck::Conflict { existing_path } => {
                return Ok(FolderEnsureOutcome::Conflict {
                    folder_id: def.folder_id.to_string(),
                    expected: expected_path.display().to_string(),
                    existing: existing_path,
                });
            }
        }

        // Ensure the local directory exists so Syncthing doesn't error.
        let _ = fs::create_dir_all(&expected_path);

        // Merge desired device associations with any already present so we
        // don't silently drop a device the user associated elsewhere.
        let mut devices: Vec<String> = existing
            .as_ref()
            .map(folder_config_devices)
            .unwrap_or_default();
        for d in device_ids {
            if !devices.contains(d) {
                devices.push(d.clone());
            }
        }

        let cfg = folder_config_json(def, &expected_path, &devices);
        self.put_folder(&c, cfg)?;
        self.apply_restart_if_required(&c)?;
        Ok(FolderEnsureOutcome::Ensured {
            folder_id: def.folder_id.to_string(),
        })
    }

    /// Remove a ChatBucket folder's Syncthing configuration WITHOUT touching
    /// local files (managed true→false transition, §16.5).
    pub fn remove_folder(&self, folder_id: &str) -> Result<(), ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        if self.get_folder_config(&c, folder_id)?.is_some() {
            self.delete_folder(&c, folder_id)?;
            self.apply_restart_if_required(&c)?;
        }
        Ok(())
    }

    /// Add/verify a remote device and associate it with every currently
    /// managed ChatBucket folder (§6/§21). Validates the ID first (§16).
    pub fn add_remote_device(
        &self,
        raw_id: &str,
        name: Option<&str>,
    ) -> Result<DeviceAddOutcome, ApiError> {
        let device_id = match resources::validate_device_id(raw_id) {
            Ok(id) => id,
            Err(msg) => {
                return Ok(DeviceAddOutcome::InvalidId(msg));
            }
        };
        let c = self.client().map_err(conn_to_api)?;
        self.put_device(&c, &device_id, name)?;

        let (cfg, _) = self.read_config();
        let managed = cfg.managed.managed_resources();
        let mut associated = Vec::new();
        for def in managed {
            if let Ok(FolderEnsureOutcome::Ensured { .. }) =
                self.ensure_folder(def, std::slice::from_ref(&device_id))
            {
                associated.push(def.folder_id.to_string());
            }
        }
        self.apply_restart_if_required(&c)?;
        Ok(DeviceAddOutcome::Added {
            device_id,
            associated,
        })
    }

    /// Remove a device from ChatBucket synchronization: strip it from every
    /// ChatBucket folder, then remove the device ONLY if it is not referenced
    /// by any remaining (incl. unrelated) folder (§6 device actions).
    pub fn remove_remote_device(&self, device_id: &str) -> Result<(), ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        // Strip from ChatBucket folders.
        for def in resources::RESOURCES {
            if let Some(fcfg) = self.get_folder_config(&c, def.folder_id)? {
                let mut devs = folder_config_devices(&fcfg);
                if devs.iter().any(|d| d == device_id) {
                    devs.retain(|d| d != device_id);
                    let path = folder_config_path(&fcfg)
                        .map(PathBuf::from)
                        .unwrap_or_else(|| resources::local_path(&self.repo_root, def));
                    let newcfg = folder_config_json(def, &path, &devs);
                    self.put_folder(&c, newcfg)?;
                }
            }
        }
        // Is the device referenced by ANY other (unrelated) folder?
        let all_folders: serde_json::Value = c.get("/rest/config/folders")?;
        let still_used = all_folders
            .as_array()
            .map(|arr| {
                arr.iter().any(|f| {
                    !resources::is_chatbucket_folder_id(
                        f.get("id").and_then(|x| x.as_str()).unwrap_or(""),
                    ) && folder_config_devices(f).iter().any(|d| d == device_id)
                })
            })
            .unwrap_or(false);
        if !still_used {
            self.delete_device(&c, device_id)?;
        }
        self.apply_restart_if_required(&c)?;
        Ok(())
    }

    /// Set the managed flag and apply the transition (§16.5).
    pub fn set_managed(
        &self,
        folder_id: &str,
        managed: bool,
    ) -> Result<ManagedTransition, ApiError> {
        let Some(def) = resources::by_folder_id(folder_id) else {
            return Err(ApiError::BadJson(format!("unknown folder id {folder_id}")));
        };
        let (mut cfg, _) = self.read_config();
        cfg.managed.set_managed(folder_id, managed);
        let _ = self.save_config(cfg);

        if managed {
            // false → true: recreate/ensure with the ChatBucket profile.
            let devices = self.all_configured_chatbucket_device_ids()?;
            match self.ensure_folder(def, &devices)? {
                FolderEnsureOutcome::Ensured { .. } => Ok(ManagedTransition::Enabled),
                FolderEnsureOutcome::Conflict {
                    existing, expected, ..
                } => Ok(ManagedTransition::EnableConflict { existing, expected }),
            }
        } else {
            // true → false: remove Syncthing config, preserve local files.
            self.remove_folder(folder_id)?;
            Ok(ManagedTransition::Disabled)
        }
    }

    /// Device-ID → list of ChatBucket folder IDs it is associated with.
    /// Derived from CONFIG (not live status), used to render the device list
    /// and the "configured but not fully associated" signal (§6).
    pub fn all_chatbucket_device_associations(&self) -> BTreeMap<String, Vec<String>> {
        let mut out: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let Ok(c) = self.client().map_err(conn_to_api) else {
            return out;
        };
        for def in resources::RESOURCES {
            if let Ok(Some(fcfg)) = self.get_folder_config(&c, def.folder_id) {
                for d in folder_config_devices(&fcfg) {
                    out.entry(d).or_default().push(def.folder_id.to_string());
                }
            }
        }
        out
    }

    /// Is a device currently connected? Uses /rest/system/connections.
    pub fn device_connected(&self, device_id: &str) -> bool {
        let Ok(c) = self.client().map_err(conn_to_api) else {
            return false;
        };
        let v: serde_json::Value = match c.get("/rest/system/connections") {
            Ok(v) => v,
            Err(_) => return false,
        };
        v.get("connections")
            .and_then(|m| m.get(device_id))
            .and_then(|d| d.get("connected"))
            .and_then(|b| b.as_bool())
            .unwrap_or(false)
    }

    /// All device IDs referenced by any ChatBucket folder.
    fn all_configured_chatbucket_device_ids(&self) -> Result<Vec<String>, ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        let mut out = Vec::new();
        for def in resources::RESOURCES {
            if let Some(fcfg) = self.get_folder_config(&c, def.folder_id)? {
                for d in folder_config_devices(&fcfg) {
                    if !out.contains(&d) {
                        out.push(d);
                    }
                }
            }
        }
        Ok(out)
    }

    /// Reconcile/repair every ChatBucket-owned resource (§12). Safe to run
    /// repeatedly (idempotent). Only touches managed:true resources.
    pub fn reconcile(&self) -> Result<ReconcileReport, ApiError> {
        let (cfg, _) = self.read_config();
        let devices = self
            .all_configured_chatbucket_device_ids()
            .unwrap_or_default();
        let mut report = ReconcileReport::default();
        for def in resources::RESOURCES {
            if !cfg.managed.is_managed(def.folder_id) {
                report.skipped_disabled.push(def.folder_id.to_string());
                continue;
            }
            match self.ensure_folder(def, &devices)? {
                FolderEnsureOutcome::Ensured { folder_id } => {
                    report.ensured.push(folder_id);
                }
                FolderEnsureOutcome::Conflict {
                    folder_id,
                    expected,
                    existing,
                } => {
                    report.conflicts.push(FolderConflict {
                        folder_id,
                        expected,
                        existing,
                    });
                }
            }
        }
        Ok(report)
    }

    // ── runtime controls (§10/§22/§23) ────────────────────────────

    pub fn scan_folder(&self, folder_id: &str) -> Result<(), ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        c.send("post", &format!("/rest/db/scan?folder={folder_id}"), None)
    }

    /// Rescan only the currently-managed ChatBucket folders (§22).
    pub fn scan_all_chatbucket(&self) -> Result<usize, ApiError> {
        let (cfg, _) = self.read_config();
        let mut n = 0;
        for def in cfg.managed.managed_resources() {
            self.scan_folder(def.folder_id)?;
            n += 1;
        }
        Ok(n)
    }

    pub fn pause_folder(&self, folder_id: &str) -> Result<(), ApiError> {
        self.set_folder_paused(folder_id, true)
    }
    pub fn resume_folder(&self, folder_id: &str) -> Result<(), ApiError> {
        self.set_folder_paused(folder_id, false)
    }
    fn set_folder_paused(&self, folder_id: &str, paused: bool) -> Result<(), ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        if let Some(mut fcfg) = self.get_folder_config(&c, folder_id)? {
            fcfg["paused"] = serde_json::Value::Bool(paused);
            self.put_folder(&c, fcfg)?;
            self.apply_restart_if_required(&c)?;
        }
        Ok(())
    }

    pub fn restart_syncthing(&self) -> Result<(), ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        c.send("post", "/rest/system/restart", None)
    }

    pub fn clear_errors(&self) -> Result<(), ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        c.send("post", "/rest/system/error/clear", None)
    }

    /// After a config write, if Syncthing staged it, restart and wait for
    /// the API to come back, then only report success (§16.5).
    fn apply_restart_if_required(&self, c: &ApiClient) -> Result<(), ApiError> {
        let rr: RestartRequired = c.get_into("/rest/config/restart-required")?;
        if !rr.requires_restart {
            return Ok(());
        }
        self.restart_syncthing()?;
        self.wait_until_reachable(RESTART_WAIT_TIMEOUT)
    }

    fn wait_until_reachable(&self, timeout: Duration) -> Result<(), ApiError> {
        let start = Instant::now();
        let poll = Duration::from_millis(500);
        while start.elapsed() < timeout {
            if let Ok(c) = self.client() {
                if self.ping(&c).is_ok() {
                    return Ok(());
                }
            }
            std::thread::sleep(poll);
        }
        Err(ApiError::Unreachable(
            "syncthing did not become reachable after restart".into(),
        ))
    }

    // ── pending requests + events (§17) ───────────────────────────

    pub fn pending_devices(&self) -> Result<PendingDevices, ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        c.get_into("/rest/cluster/pending/devices")
    }

    pub fn pending_folders(&self) -> Result<PendingFolders, ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        c.get_into("/rest/cluster/pending/folders")
    }

    /// Snapshot of the current pending state, classified into ChatBucket
    /// terms using the §17 subset rule. This is the source of truth; the
    /// event stream only tells us WHEN to call this.
    pub fn pending_snapshot(&self) -> Result<PendingSnapshot, ApiError> {
        let devs = self.pending_devices().unwrap_or_default();
        let folders = self.pending_folders().unwrap_or_default();

        // Group pending folder offers by offering device.
        let mut by_device: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for (folder_id, offers) in &folders {
            for device_id in offers.offered_by.keys() {
                by_device
                    .entry(device_id.clone())
                    .or_default()
                    .push(folder_id.clone());
            }
        }

        let mut chatbucket = Vec::new();
        let mut unknown = Vec::new();
        // Union of devices that have a pending device request OR folder offers.
        let mut all_devices: Vec<String> = devs.keys().cloned().collect();
        for d in by_device.keys() {
            if !all_devices.contains(d) {
                all_devices.push(d.clone());
            }
        }
        for device_id in all_devices {
            let offered = by_device.get(&device_id).cloned().unwrap_or_default();
            let name = devs
                .get(&device_id)
                .map(|d| d.name.clone())
                .filter(|n| !n.is_empty())
                .unwrap_or_else(|| short_device_id(&device_id));
            match resources::classify_pending(&offered) {
                resources::PendingKind::None => {
                    // Bare device request with no folder offers yet.
                    chatbucket.push(PendingRequest {
                        device_id: device_id.clone(),
                        device_name: name,
                        folder_ids: vec![],
                        is_chatbucket: true,
                    });
                }
                resources::PendingKind::ChatBucket { folder_ids } => {
                    chatbucket.push(PendingRequest {
                        device_id: device_id.clone(),
                        device_name: name,
                        folder_ids,
                        is_chatbucket: true,
                    });
                }
                resources::PendingKind::Mixed { unknown_ids } => {
                    unknown.push(PendingRequest {
                        device_id: device_id.clone(),
                        device_name: name,
                        folder_ids: unknown_ids,
                        is_chatbucket: false,
                    });
                }
            }
        }
        Ok(PendingSnapshot {
            chatbucket_requests: chatbucket,
            unknown_requests: unknown,
        })
    }

    /// Long-poll the event stream for pending-change events. Returns Some(())
    /// when a PendingDevicesChanged/PendingFoldersChanged event fired, None
    /// on timeout. This is the §17/§24 trigger; the caller then re-reads
    /// pending_snapshot().
    pub fn wait_for_pending_event(&self, since: u64) -> Result<(bool, u64), ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        let events = c.events_since(since, EVENT_LONG_POLL)?;
        let mut max_id = since;
        let mut relevant = false;
        for ev in &events {
            max_id = max_id.max(ev.id);
            if ev.event_type == "PendingDevicesChanged" || ev.event_type == "PendingFoldersChanged"
            {
                relevant = true;
            }
        }
        Ok((relevant, max_id))
    }

    /// Accept a pending ChatBucket request: add the device and associate it
    /// with the requested (ChatBucket) folders mapped to LOCAL paths (§17/§20).
    pub fn accept_pending(
        &self,
        device_id: &str,
        folder_ids: &[String],
    ) -> Result<DeviceAddOutcome, ApiError> {
        let c = self.client().map_err(conn_to_api)?;
        // Device name from pending info if available.
        let name = self
            .pending_devices()
            .ok()
            .and_then(|d| d.get(device_id).map(|i| i.name.clone()))
            .filter(|n| !n.is_empty());
        self.put_device(&c, device_id, name.as_deref())?;

        let defs = resources::resources_for_ids(folder_ids);
        let mut associated = Vec::new();
        for def in defs {
            if let Ok(FolderEnsureOutcome::Ensured { .. }) =
                self.ensure_folder(def, std::slice::from_ref(&device_id.to_string()))
            {
                associated.push(def.folder_id.to_string());
            }
        }
        self.apply_restart_if_required(&c)?;
        Ok(DeviceAddOutcome::Added {
            device_id: device_id.to_string(),
            associated,
        })
    }

    /// Reject a pending request. Syncthing has no "dismiss" for pending
    /// devices; the ChatBucket-level reject means "do not add it" — we simply
    /// never accept. We record the dismissal so the UI stops prompting.
    pub fn reject_pending(&self, device_id: &str) -> Result<(), ApiError> {
        log::info!(
            "[syncthing] rejected pending request from {}",
            short_device_id(device_id)
        );
        Ok(())
    }
}

fn conn_to_api(cs: ConnectionState) -> ApiError {
    match cs {
        ConnectionState::AuthFailed => ApiError::AuthFailed,
        ConnectionState::Unreachable(d) => ApiError::Unreachable(d),
        ConnectionState::BadConfig(d) => ApiError::BadJson(d),
        _ => ApiError::Unreachable("not configured".into()),
    }
}

fn short_device_id(id: &str) -> String {
    id.chars().take(7).collect()
}

// ── Outcome types surfaced to the Manager/UI ────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FolderEnsureOutcome {
    Ensured {
        folder_id: String,
    },
    Conflict {
        folder_id: String,
        expected: String,
        existing: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeviceAddOutcome {
    Added {
        device_id: String,
        associated: Vec<String>,
    },
    InvalidId(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ManagedTransition {
    Enabled,
    Disabled,
    EnableConflict { existing: String, expected: String },
}

#[derive(Debug, Clone, Default)]
pub struct ReconcileReport {
    pub ensured: Vec<String>,
    pub skipped_disabled: Vec<String>,
    pub conflicts: Vec<FolderConflict>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FolderConflict {
    pub folder_id: String,
    pub expected: String,
    pub existing: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingRequest {
    pub device_id: String,
    pub device_name: String,
    pub folder_ids: Vec<String>,
    pub is_chatbucket: bool,
}

#[derive(Debug, Clone, Default)]
pub struct PendingSnapshot {
    pub chatbucket_requests: Vec<PendingRequest>,
    /// Requests with unknown (non-ChatBucket) folder IDs — surfaced separately
    /// so they are never folded into a ChatBucket approval (§17).
    #[allow(dead_code)]
    pub unknown_requests: Vec<PendingRequest>,
}

// ── Legacy compatibility shim (kept for the tray/legacy probe) ──────────
//
// The original single-folder probe (FOLDER_ID = "sync-state") is retained as
// a thin wrapper over the controller so existing call sites keep compiling
// while the full integration is the primary path.

pub const FOLDER_ID: &str = "sync-state";

static LEGACY_CACHE: Mutex<Option<(Instant, LegacyState)>> = Mutex::new(None);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LegacyState {
    NotConfigured,
    InSync,
    Syncing,
    AuthFailed,
    Unreachable(String),
    FolderMissing,
    BadConfig(String),
    Error(String),
}

impl LegacyState {
    pub fn short_label(&self) -> String {
        match self {
            LegacyState::NotConfigured => "not configured".into(),
            LegacyState::InSync => "in sync".into(),
            LegacyState::Syncing => "syncing…".into(),
            LegacyState::AuthFailed => "auth failed (401)".into(),
            LegacyState::Unreachable(_) => "unreachable".into(),
            LegacyState::FolderMissing => "folder not found".into(),
            LegacyState::BadConfig(_) => "bad config".into(),
            LegacyState::Error(_) => "error".into(),
        }
    }
    pub fn detail(&self) -> String {
        match self {
            LegacyState::NotConfigured => "Paste an API key to enable the Syncthing probe.".into(),
            LegacyState::InSync => "sync-state folder is idle and up to date.".into(),
            LegacyState::Syncing => "sync-state folder is transferring or scanning.".into(),
            LegacyState::AuthFailed => "Syncthing rejected the API key (401/403).".into(),
            LegacyState::Unreachable(d) => format!("Could not reach Syncthing: {d}"),
            LegacyState::FolderMissing => format!("No folder id '{FOLDER_ID}' on this Syncthing."),
            LegacyState::BadConfig(d) => format!("manager_config.json could not be parsed: {d}"),
            LegacyState::Error(d) => d.clone(),
        }
    }
}

pub fn invalidate_cache() {
    if let Ok(mut g) = LEGACY_CACHE.lock() {
        *g = None;
    }
}

pub fn get_status(repo_root: &Path) -> LegacyState {
    {
        let guard = LEGACY_CACHE.lock().unwrap();
        if let Some((when, ref st)) = *guard {
            if when.elapsed() < Duration::from_secs(30) {
                return st.clone();
            }
        }
    }
    let st = legacy_fetch(repo_root);
    let mut guard = LEGACY_CACHE.lock().unwrap();
    *guard = Some((Instant::now(), st.clone()));
    st
}

fn legacy_fetch(repo_root: &Path) -> LegacyState {
    let (cfg, parse_err) = read_config(repo_root);
    if let Some(msg) = parse_err {
        return LegacyState::BadConfig(msg);
    }
    let Some(api_key) = cfg.key() else {
        return LegacyState::NotConfigured;
    };
    let url = format!("{}/rest/db/status?folder={}", cfg.url(), FOLDER_ID);
    let resp = ureq::get(&url)
        .set("X-API-Key", api_key)
        .timeout(HTTP_TIMEOUT)
        .call();
    match resp {
        Ok(r) => match r.into_json::<DbStatus>() {
            Ok(body) => {
                if body.pull_errors > 0 {
                    LegacyState::Error(format!("{} pull error(s)", body.pull_errors))
                } else if body.state == "error" {
                    LegacyState::Error("folder is in error state".into())
                } else if body.state == "idle" && body.need_files == 0 && body.need_bytes == 0 {
                    LegacyState::InSync
                } else {
                    LegacyState::Syncing
                }
            }
            Err(e) => LegacyState::Error(format!("bad JSON: {e}")),
        },
        Err(ureq::Error::Status(code, resp)) => match code {
            401 | 403 => LegacyState::AuthFailed,
            404 => LegacyState::FolderMissing,
            _ => LegacyState::Error(format!("HTTP {} {}", code, resp.status_text())),
        },
        Err(ureq::Error::Transport(t)) => LegacyState::Unreachable(short_transport(t)),
    }
}

pub fn test_now(repo_root: &Path) -> LegacyState {
    invalidate_cache();
    legacy_fetch(repo_root)
}

#[cfg(test)]
mod tests;
