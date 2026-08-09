//! syncthing.rs — Read-only status check + Manager-owned config read/write.
//!
//! Failure modes designed against, listed explicitly:
//!
//!   FM-1  Missing config file  -> NotConfigured (not an error). Same behaviour
//!         as before; user is prompted to paste an API key in the GUI.
//!   FM-2  Config file exists but has malformed JSON -> Error("bad config JSON").
//!         Surface, don't silently treat as "no key" (that would loop 401s
//!         forever while claiming "not configured").
//!   FM-3  Key is wrong (HTTP 401/403) -> AuthFailed. Distinct from a network
//!         error so the GUI can tell the user "wrong key" vs "syncthing is down".
//!   FM-4  Syncthing not running at all (connection refused) -> Unreachable.
//!         Also distinct from AuthFailed — different user action (start
//!         Syncthing vs paste correct key).
//!   FM-5  Folder id typo -> HTTP 404 -> FolderMissing. Surface explicitly;
//!         previously would have shown a generic "error" with a JSON parse
//!         message that wasn't useful.
//!   FM-6  User pastes key with surrounding quotes / whitespace -> trimmed
//!         before write, never persisted verbatim. Save-time normalisation
//!         means a subsequent Test button doesn't fail for a reason the
//!         user can't see.
//!   FM-7  Config file directory is read-only or disk full -> save() returns
//!         a real Err with the OS message; GUI displays it inline near the
//!         input. Never silently swallowed.
//!   FM-8  API key accidentally logged -> we NEVER log the key. Only "config
//!         at <path>: parse failed" / "syncthing unreachable" / etc.
//!   FM-9  Stale cache after Save -> `invalidate_cache()` is called by the
//!         config-save path so the next poll re-fetches with the new key
//!         rather than returning the pre-save "not configured" for another
//!         30s.
//!   FM-10 Concurrent write by two Manager instances on the same box (rare
//!         but possible if the user double-launched) -> atomic tempfile +
//!         rename, same discipline as host_state.py.

use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub const FOLDER_ID: &str = "sync-state";
const MIN_INTERVAL: Duration = Duration::from_secs(30);
const HTTP_TIMEOUT: Duration = Duration::from_secs(5);
const CONFIG_FILE: &str = "manager_config.json";
const DEFAULT_URL: &str = "http://127.0.0.1:8384";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SyncthingState {
    /// No key on file yet. Prompt user in the GUI.
    NotConfigured,
    /// Everything happy — folder is idle with 0 needFiles / 0 needBytes.
    InSync,
    /// Actively transferring or scanning.
    Syncing,
    /// Config has a key but Syncthing rejected it (401/403). User action:
    /// paste the correct key.
    AuthFailed,
    /// Config has a key but Syncthing is not reachable at all. User action:
    /// start Syncthing / fix the URL.
    Unreachable(String),
    /// Folder id doesn't exist in this Syncthing instance (404). User action:
    /// fix folder id in config OR create the folder in Syncthing.
    FolderMissing,
    /// Config file itself is malformed.
    BadConfig(String),
    /// Any other well-formed-but-unhappy response (pullErrors > 0, state ==
    /// "error", weird JSON shape).
    Error(String),
}

impl SyncthingState {
    pub fn short_label(&self) -> String {
        match self {
            SyncthingState::NotConfigured => "not configured".into(),
            SyncthingState::InSync => "in sync".into(),
            SyncthingState::Syncing => "syncing…".into(),
            SyncthingState::AuthFailed => "auth failed (401)".into(),
            SyncthingState::Unreachable(_) => "unreachable".into(),
            SyncthingState::FolderMissing => "folder not found".into(),
            SyncthingState::BadConfig(_) => "bad config".into(),
            SyncthingState::Error(_) => "error".into(),
        }
    }
    pub fn detail(&self) -> String {
        match self {
            SyncthingState::NotConfigured => {
                "Paste an API key below to enable the Syncthing status probe. \
                 Manager-only setting — never touched by ChatBucket itself."
                    .into()
            }
            SyncthingState::InSync => "sync-state folder is idle and up to date.".into(),
            SyncthingState::Syncing => {
                "sync-state folder is transferring or scanning right now.".into()
            }
            SyncthingState::AuthFailed => {
                "Syncthing rejected the API key (HTTP 401/403). Copy a fresh key from \
                 Syncthing → Actions → Settings → GUI API Key and paste it again."
                    .into()
            }
            SyncthingState::Unreachable(d) => format!(
                "Could not reach Syncthing at the configured URL. {} Is Syncthing running? \
                 Default URL is {DEFAULT_URL}.",
                d
            ),
            SyncthingState::FolderMissing => format!(
                "Syncthing responded, but has no folder with id '{FOLDER_ID}'. \
                 The state/ folder must exist in Syncthing on THIS machine."
            ),
            SyncthingState::BadConfig(d) => {
                format!("manager_config.json could not be parsed: {}", d)
            }
            SyncthingState::Error(d) => d.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ManagerConfig {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub syncthing_api_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub syncthing_url: Option<String>,
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
/// yields (default(), None). A malformed file yields (default(), Some(msg))
/// so callers can render BadConfig instead of silently downgrading to
/// NotConfigured — the whole point of FM-2 above.
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
        Ok(cfg) => (cfg, None),
        Err(e) => (ManagerConfig::default(), Some(e.to_string())),
    }
}

/// Atomic write. Same discipline as host_state.py: tempfile in the same
/// directory, fsync, rename over the target. Never partial. Never touches
/// keys other than the ones we own (we round-trip the whole struct through
/// serde so an unknown future field would be lost — but for THIS release
/// the config only has two fields, so that's fine).
///
/// User-facing normalisation applied on save:
///   * `syncthing_api_key`: trimmed, surrounding "quotes" stripped, empty →
///     None (removes the key entirely rather than persisting an empty string
///     that later parses to "configured with a broken key").
///   * `syncthing_url`: trimmed, trailing slash removed, empty → None.
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

    // FM-9: invalidate cache so the next poll re-fetches with the new key.
    invalidate_cache();
    Ok(cfg)
}

// ── polling / caching ──────────────────────────────────────────────────

#[derive(Debug, Deserialize, Default)]
struct DbStatus {
    #[serde(default)]
    state: String,
    #[serde(rename = "needFiles", default)]
    need_files: i64,
    #[serde(rename = "needBytes", default)]
    need_bytes: i64,
    #[serde(rename = "pullErrors", default)]
    pull_errors: i64,
}

static CACHE: Mutex<Option<(Instant, SyncthingState)>> = Mutex::new(None);

pub fn invalidate_cache() {
    if let Ok(mut g) = CACHE.lock() {
        *g = None;
    }
}

pub fn get_status(repo_root: &Path) -> SyncthingState {
    {
        let guard = CACHE.lock().unwrap();
        if let Some((when, ref state)) = *guard {
            if when.elapsed() < MIN_INTERVAL {
                return state.clone();
            }
        }
    }
    let state = fetch(repo_root);
    let mut guard = CACHE.lock().unwrap();
    *guard = Some((Instant::now(), state.clone()));
    state
}

fn fetch(repo_root: &Path) -> SyncthingState {
    let (cfg, parse_err) = read_config(repo_root);
    if let Some(msg) = parse_err {
        return SyncthingState::BadConfig(msg);
    }
    let Some(api_key) = cfg.key() else {
        return SyncthingState::NotConfigured;
    };
    let url = format!("{}/rest/db/status?folder={}", cfg.url(), FOLDER_ID);

    let resp = ureq::get(&url)
        .set("X-API-Key", api_key)
        .timeout(HTTP_TIMEOUT)
        .call();

    match resp {
        Ok(r) => match r.into_json::<DbStatus>() {
            Ok(body) => classify_body(&body),
            Err(e) => SyncthingState::Error(format!("bad JSON: {e}")),
        },
        Err(ureq::Error::Status(code, resp)) => match code {
            401 | 403 => SyncthingState::AuthFailed,
            404 => SyncthingState::FolderMissing,
            _ => SyncthingState::Error(format!("HTTP {} {}", code, resp.status_text())),
        },
        Err(ureq::Error::Transport(t)) => {
            // Never leak the API key into logs; ureq's transport error
            // includes the URL but not headers, so this is safe.
            SyncthingState::Unreachable(format!("{}", short_transport(t)))
        }
    }
}

fn classify_body(body: &DbStatus) -> SyncthingState {
    if body.pull_errors > 0 {
        return SyncthingState::Error(format!("{} pull error(s)", body.pull_errors));
    }
    if body.state == "error" {
        return SyncthingState::Error("folder is in error state".into());
    }
    if body.state == "idle" && body.need_files == 0 && body.need_bytes == 0 {
        return SyncthingState::InSync;
    }
    SyncthingState::Syncing
}

fn short_transport(t: ureq::Transport) -> String {
    // Trim the multi-line noise ureq produces; we want one useful line.
    let s = t.to_string();
    s.lines().next().unwrap_or("").to_string()
}

/// One-shot connectivity check for the GUI "Test" button. Uses the CURRENT
/// on-disk config (i.e. call save_config() first if you want to test what's
/// in the input boxes). Skips the 30s cache.
pub fn test_now(repo_root: &Path) -> SyncthingState {
    invalidate_cache();
    fetch(repo_root)
}
