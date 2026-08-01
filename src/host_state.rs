//! host_state.rs — Read/write host-state.json.
//!
//! Direct port of Python `host_state.py`. Same contract:
//!   * `read_state()` returns None for a missing/empty file (a NORMAL condition
//!     — nobody has ever hosted), Err for a corrupted file (surface loudly,
//!     never silently treat as "no claim" — that would cause every machine to
//!     self-elect on top of a healthy host whose file merely became unreadable).
//!   * `write_state()` writes atomically: temp file in the SAME directory,
//!     fsync before rename, os::replace over the target. Same-directory temp
//!     is required, not incidental — os::replace's atomicity guarantee only
//!     holds within one filesystem, and a cross-mount move silently falls
//!     back to non-atomic copy+delete on some platforms.
//!
//! Microsecond-resolution timestamps: arbitration's jitter re-check compares
//! whole state dicts by equality, so two writes within the same wall-clock
//! second must not collide byte-for-byte.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

pub const STATE_DIR: &str = "state";
pub const STATE_FILE_NAME: &str = "host-state.json";

#[derive(Debug, thiserror::Error)]
pub enum HostStateError {
    #[error("could not read {path}: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("{path} contains invalid JSON — refusing to guess. Parse error: {source}")]
    Parse {
        path: String,
        #[source]
        source: serde_json::Error,
    },
    #[error("{path} is missing required field(s): {fields:?}")]
    MissingFields { path: String, fields: Vec<String> },
    #[error("{path} has invalid action {action:?}, expected 'start' or 'stop'")]
    InvalidAction { path: String, action: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HostState {
    pub action: String,
    pub machine: String,
    pub timestamp: String,
}

impl HostState {
    pub fn is_start_by(&self, machine: &str) -> bool {
        self.action == "start" && self.machine == machine
    }
}

fn state_path(repo_root: &Path) -> PathBuf {
    repo_root.join(STATE_DIR).join(STATE_FILE_NAME)
}

pub fn read_state(repo_root: &Path) -> Result<Option<HostState>, HostStateError> {
    let path = state_path(repo_root);
    if !path.exists() {
        return Ok(None);
    }

    let raw = fs::read_to_string(&path).map_err(|e| HostStateError::Io {
        path: path.display().to_string(),
        source: e,
    })?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Ok(None);
    }

    // Parse to a generic Value first so we can produce field-level error
    // messages that match Python's shape.
    let value: serde_json::Value =
        serde_json::from_str(trimmed).map_err(|e| HostStateError::Parse {
            path: path.display().to_string(),
            source: e,
        })?;

    let obj = value.as_object().ok_or_else(|| HostStateError::MissingFields {
        path: path.display().to_string(),
        fields: vec!["action".into(), "machine".into(), "timestamp".into()],
    })?;

    let missing: Vec<String> = ["action", "machine", "timestamp"]
        .iter()
        .filter(|k| !obj.contains_key(**k))
        .map(|s| s.to_string())
        .collect();
    if !missing.is_empty() {
        return Err(HostStateError::MissingFields {
            path: path.display().to_string(),
            fields: missing,
        });
    }

    let action = obj["action"].as_str().unwrap_or("").to_string();
    let machine = obj["machine"].as_str().unwrap_or("").to_string();
    let timestamp = obj["timestamp"].as_str().unwrap_or("").to_string();

    if action != "start" && action != "stop" {
        return Err(HostStateError::InvalidAction {
            path: path.display().to_string(),
            action,
        });
    }

    Ok(Some(HostState {
        action,
        machine,
        timestamp,
    }))
}

pub fn write_state(
    repo_root: &Path,
    action: &str,
    machine: &str,
) -> Result<HostState, HostStateError> {
    if action != "start" && action != "stop" {
        return Err(HostStateError::InvalidAction {
            path: state_path(repo_root).display().to_string(),
            action: action.to_string(),
        });
    }
    if machine.is_empty() {
        return Err(HostStateError::InvalidAction {
            path: state_path(repo_root).display().to_string(),
            action: "empty machine".to_string(),
        });
    }

    let dir = repo_root.join(STATE_DIR);
    fs::create_dir_all(&dir).map_err(|e| HostStateError::Io {
        path: dir.display().to_string(),
        source: e,
    })?;

    let now: DateTime<Utc> = Utc::now();
    let state = HostState {
        action: action.to_string(),
        machine: machine.to_string(),
        // Microsecond-precision — same rationale as Python (see module doc).
        timestamp: now.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string(),
    };
    let payload = serde_json::to_string(&state).unwrap();

    // Temp file in the SAME directory as the target — see module docstring.
    let tmp = tempfile::Builder::new()
        .prefix(".host-state-")
        .suffix(".tmp")
        .tempfile_in(&dir)
        .map_err(|e| HostStateError::Io {
            path: dir.display().to_string(),
            source: e,
        })?;

    {
        let mut f = tmp.as_file();
        f.write_all(payload.as_bytes()).map_err(|e| HostStateError::Io {
            path: tmp.path().display().to_string(),
            source: e,
        })?;
        f.sync_all().map_err(|e| HostStateError::Io {
            path: tmp.path().display().to_string(),
            source: e,
        })?;
    }

    let final_path = state_path(repo_root);
    tmp.persist(&final_path).map_err(|e| HostStateError::Io {
        path: final_path.display().to_string(),
        source: e.error,
    })?;

    Ok(state)
}
