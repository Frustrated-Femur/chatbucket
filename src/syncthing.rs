//! syncthing.rs — Minimal read-only Syncthing status check.
//!
//! One question, one REST call: is the `sync-state` folder (holding
//! host-state.json) actually in sync right now. No transfer queues, no
//! per-file progress, no device list — Syncthing already has a correct
//! GUI at 127.0.0.1:8384 (Architecture doc §5).
//!
//! Throttled: /rest/db/status is flagged "expensive...use sparingly" in
//! Syncthing's own docs. The window polls status every 15s, but this
//! module caches the result for 30s so the actual HTTP call happens at
//! most every 30s regardless of caller frequency. Same behaviour as the
//! Python `_syncthing_cache` global.

use serde::Deserialize;
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub const FOLDER_ID: &str = "sync-state";
const MIN_INTERVAL: Duration = Duration::from_secs(30);
const HTTP_TIMEOUT: Duration = Duration::from_secs(5);
const CONFIG_FILE: &str = "manager_config.json";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SyncthingState {
    NotConfigured,
    InSync,
    Syncing,
    Error(String),
}

impl SyncthingState {
    pub fn label(&self) -> &str {
        match self {
            SyncthingState::NotConfigured => "not configured",
            SyncthingState::InSync => "in sync",
            SyncthingState::Syncing => "syncing…",
            SyncthingState::Error(_) => "error",
        }
    }
}

#[derive(Debug, Deserialize, Default)]
struct ManagerConfig {
    #[serde(default)]
    syncthing_api_key: Option<String>,
    #[serde(default)]
    syncthing_url: Option<String>,
}

fn read_manager_config(repo_root: &Path) -> ManagerConfig {
    let path = repo_root.join(CONFIG_FILE);
    match std::fs::read_to_string(&path) {
        Ok(raw) => serde_json::from_str(&raw).unwrap_or_default(),
        Err(_) => ManagerConfig::default(),
    }
}

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

pub fn get_status(repo_root: &Path) -> SyncthingState {
    // Throttle
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
    let cfg = read_manager_config(repo_root);
    let Some(api_key) = cfg.syncthing_api_key.filter(|k| !k.is_empty()) else {
        return SyncthingState::NotConfigured;
    };
    let base = cfg
        .syncthing_url
        .unwrap_or_else(|| "http://127.0.0.1:8384".to_string());
    let base = base.trim_end_matches('/').to_string();
    let url = format!("{}/rest/db/status?folder={}", base, FOLDER_ID);

    let resp = ureq::get(&url)
        .set("X-API-Key", &api_key)
        .timeout(HTTP_TIMEOUT)
        .call();
    let body: DbStatus = match resp {
        Ok(r) => match r.into_json() {
            Ok(v) => v,
            Err(e) => return SyncthingState::Error(format!("bad JSON: {e}")),
        },
        Err(e) => return SyncthingState::Error(e.to_string()),
    };

    if body.pull_errors > 0 || body.state == "error" {
        let detail = if body.pull_errors > 0 {
            format!("{} pull error(s)", body.pull_errors)
        } else {
            "folder error".to_string()
        };
        return SyncthingState::Error(detail);
    }
    if body.state == "idle" && body.need_files == 0 && body.need_bytes == 0 {
        return SyncthingState::InSync;
    }
    SyncthingState::Syncing
}
