//! resources.rs — Single source of truth for ChatBucket-managed sync resources.
//!
//! This module is the ONE authoritative place that knows:
//!   * the seven fixed Syncthing folder IDs (protocol identifiers, identical
//!     on every ChatBucket installation — never generated, never random);
//!   * the resource → relative-path mapping (`messages` → `<repo>/messages`);
//!   * the human-facing label for each resource;
//!   * the per-folder ChatBucket policy (versioning, watcher, permissions);
//!   * the `managed` flag semantics (true → repairable, false → do not touch);
//!   * folder-ID collision detection (two repos sharing one Syncthing daemon);
//!   * pending-request subset matching (§17);
//!   * Syncthing device-ID validation;
//!   * `.sync-conflict-*` detection for the `state/` arbitration folder;
//!   * ChatBucket-level status classification.
//!
//! NOTHING outside this module should hardcode a folder ID, a relative path,
//! or a policy value. app.rs / syncthing.rs / manager.rs all consume the
//! definitions here. Duplicating them is the maintenance nightmare §28 of
//! the spec calls out.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

// ── Fixed folder IDs (§16.5) — protocol constants, never change ─────────

pub const ID_MESSAGES: &str = "chatbucket-messages";
pub const ID_PRESENCE: &str = "chatbucket-presence";
pub const ID_STATE: &str = "chatbucket-state";
pub const ID_GIFS: &str = "chatbucket-gifs";
pub const ID_SFX: &str = "chatbucket-sfx";
pub const ID_STICKERS: &str = "chatbucket-stickers";
pub const ID_UPLOADS: &str = "chatbucket-uploads";

/// Every recognized ChatBucket Syncthing folder ID. Used to decide whether
/// a pending folder offer is a ChatBucket request (§17) and to make sure the
/// Manager never mistakes an unrelated user folder for one of its own.
pub const ALL_FOLDER_IDS: &[&str] = &[
    ID_MESSAGES,
    ID_PRESENCE,
    ID_STATE,
    ID_GIFS,
    ID_SFX,
    ID_STICKERS,
    ID_UPLOADS,
];

// ── Resource definitions ────────────────────────────────────────────────

/// Whether a folder keeps bounded version history. The ONLY axis on which
/// the seven ChatBucket folders differ (§20): messages/presence/state are
/// high-churn / ephemeral / arbitration-owned and keep NO versions, while
/// media assets (gifs/sfx/stickers) and uploads are expensive to recreate
/// and keep bounded history.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VersioningPolicy {
    /// No versioning — `messages/`, `presence/`, `state/`.
    None,
    /// Bounded retention — `gifs/`, `sfx/`, `stickers/`, `uploads/`.
    /// Implemented as Syncthing "staggered" versioning with a finite window.
    Bounded,
}

/// One ChatBucket-managed logical resource.
#[derive(Debug, Clone, Copy)]
pub struct ResourceDef {
    /// Stable wire-level identifier (the fixed Syncthing folder ID).
    pub folder_id: &'static str,
    /// Human label shown in the Manager UI (and used as the Syncthing folder
    /// label purely for the Syncthing GUI's benefit — never for identity).
    pub label: &'static str,
    /// Path relative to the ChatBucket repository root.
    pub relative_path: &'static str,
    /// Versioning policy for this resource.
    pub versioning: VersioningPolicy,
    /// True for the arbitration-bearing `state/` folder, which gets extra
    /// `.sync-conflict-*` detection handed to the arbitration layer (§15/§16.5).
    pub is_arbitration_state: bool,
}

/// The single authoritative table of all seven ChatBucket resources.
/// Order is stable and matches the spec's presentation order.
pub const RESOURCES: &[ResourceDef] = &[
    ResourceDef {
        folder_id: ID_MESSAGES,
        label: "Messages",
        relative_path: "messages",
        versioning: VersioningPolicy::None,
        is_arbitration_state: false,
    },
    ResourceDef {
        folder_id: ID_PRESENCE,
        label: "Presence",
        relative_path: "presence",
        versioning: VersioningPolicy::None,
        is_arbitration_state: false,
    },
    ResourceDef {
        folder_id: ID_STATE,
        label: "State",
        relative_path: "state",
        versioning: VersioningPolicy::None,
        is_arbitration_state: true,
    },
    ResourceDef {
        folder_id: ID_GIFS,
        label: "GIFs",
        relative_path: "gifs",
        versioning: VersioningPolicy::Bounded,
        is_arbitration_state: false,
    },
    ResourceDef {
        folder_id: ID_SFX,
        label: "SFX",
        relative_path: "sfx",
        versioning: VersioningPolicy::Bounded,
        is_arbitration_state: false,
    },
    ResourceDef {
        folder_id: ID_STICKERS,
        label: "Stickers",
        relative_path: "stickers",
        versioning: VersioningPolicy::Bounded,
        is_arbitration_state: false,
    },
    ResourceDef {
        folder_id: ID_UPLOADS,
        label: "Uploads",
        relative_path: "uploads",
        versioning: VersioningPolicy::Bounded,
        is_arbitration_state: false,
    },
];

/// Look up a resource definition by its fixed folder ID.
pub fn by_folder_id(folder_id: &str) -> Option<&'static ResourceDef> {
    RESOURCES.iter().find(|r| r.folder_id == folder_id)
}

/// Look up a resource definition by its relative path (e.g. "uploads").
pub fn by_relative_path(rel: &str) -> Option<&'static ResourceDef> {
    RESOURCES.iter().find(|r| r.relative_path == rel)
}

/// True iff `folder_id` is one of the seven fixed ChatBucket IDs.
pub fn is_chatbucket_folder_id(folder_id: &str) -> bool {
    ALL_FOLDER_IDS.contains(&folder_id)
}

/// Resolve the absolute local path for a resource under `repo_root`.
/// Never ask the user to type this — derive it (§6 of the task).
pub fn local_path(repo_root: &Path, def: &ResourceDef) -> PathBuf {
    repo_root.join(def.relative_path)
}

// ── managed flag (§16.5) ────────────────────────────────────────────────

/// The Manager-owned `managed` state for all resources. Persisted in the
/// Manager's config (NOT in transient UI memory). Default for every resource
/// is `true` — a fresh installation manages everything.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManagedState {
    /// folder_id -> managed. Missing keys default to `true`.
    #[serde(default)]
    pub map: BTreeMap<String, bool>,
}

impl Default for ManagedState {
    fn default() -> Self {
        let mut map = BTreeMap::new();
        for r in RESOURCES {
            map.insert(r.folder_id.to_string(), true);
        }
        Self { map }
    }
}

impl ManagedState {
    /// managed(folder) — defaults to true when not explicitly recorded.
    pub fn is_managed(&self, folder_id: &str) -> bool {
        self.map.get(folder_id).copied().unwrap_or(true)
    }

    pub fn set_managed(&mut self, folder_id: &str, managed: bool) {
        self.map.insert(folder_id.to_string(), managed);
    }

    /// All resources currently marked managed:true. This is the set that
    /// "all ChatBucket folders" refers to when adding a device (§6/§21) —
    /// NOT an unconditional seven.
    pub fn managed_resources(&self) -> Vec<&'static ResourceDef> {
        RESOURCES
            .iter()
            .filter(|r| self.is_managed(r.folder_id))
            .collect()
    }
}

// ── Folder-ID collision detection (§16.5 / task §8) ─────────────────────

/// Outcome of checking whether a fixed ChatBucket folder ID may be written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CollisionCheck {
    /// No folder with this ID exists yet — safe to create.
    Clear,
    /// A folder with this ID exists AND already points at this install's
    /// expected path — same installation, safe to proceed.
    SameInstall,
    /// A folder with this ID exists but points SOMEWHERE ELSE — a second
    /// ChatBucket repo shares this daemon. STOP. Do not repoint.
    Conflict { existing_path: String },
}

/// Decide what to do before writing config for `def`.
///
/// * `existing_path`: the path Syncthing currently has configured for this
///   folder ID, or None if the folder doesn't exist.
/// * `repo_root`: this installation's detected repository root.
pub fn check_collision(
    def: &ResourceDef,
    existing_path: Option<&str>,
    repo_root: &Path,
) -> CollisionCheck {
    let Some(existing) = existing_path else {
        return CollisionCheck::Clear;
    };
    let expected = local_path(repo_root, def);
    if crate::repo::paths_equal(Path::new(existing), &expected) {
        CollisionCheck::SameInstall
    } else {
        CollisionCheck::Conflict {
            existing_path: existing.to_string(),
        }
    }
}

/// Render the explicit user-facing conflict message (task §8). Used by the
/// UI layer when a CollisionCheck::Conflict is surfaced.
#[allow(dead_code)]
pub fn conflict_message(def: &ResourceDef, existing_path: &str, repo_root: &Path) -> String {
    let expected = local_path(repo_root, def);
    format!(
        "ChatBucket folder ID conflict\n\n\
         {} is already configured for another local ChatBucket directory.\n\n\
         Expected:\n  {}\n\nExisting:\n  {}\n\n\
         Manager will not repoint it automatically.",
        def.folder_id,
        expected.display(),
        existing_path
    )
}

// ── Pending-request matching (§17) ──────────────────────────────────────

/// Classification of a device's pending folder-offer set.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PendingKind {
    /// Nothing pending — show nothing.
    None,
    /// Non-empty, and EVERY pending folder ID is a recognized ChatBucket ID.
    /// Present ONE ChatBucket-level approval listing exactly these resources.
    /// This is the corrected subset rule: does NOT require == all seven,
    /// does NOT require == this machine's managed set.
    ChatBucket { folder_ids: Vec<String> },
    /// At least one pending folder ID is NOT a recognized ChatBucket ID.
    /// Never fold these into a ChatBucket request — leave them as ordinary
    /// Syncthing / manual-intervention requests.
    Mixed { unknown_ids: Vec<String> },
}

/// Apply the §17 subset-matching rule to a set of pending folder IDs.
///
/// Rule: non-empty AND every ID is a recognized ChatBucket ID → approvable
/// as a single ChatBucket request. Any unknown ID → not auto-approvable.
pub fn classify_pending(pending_folder_ids: &[String]) -> PendingKind {
    if pending_folder_ids.is_empty() {
        return PendingKind::None;
    }
    let unknown: Vec<String> = pending_folder_ids
        .iter()
        .filter(|id| !is_chatbucket_folder_id(id))
        .cloned()
        .collect();
    if !unknown.is_empty() {
        return PendingKind::Mixed {
            unknown_ids: unknown,
        };
    }
    let mut ids = pending_folder_ids.to_vec();
    ids.sort();
    ids.dedup();
    PendingKind::ChatBucket { folder_ids: ids }
}

/// Map a set of pending ChatBucket folder IDs to their resource defs,
/// preserving the canonical RESOURCES display order. Unknown IDs are dropped
/// (callers should have already filtered via classify_pending).
pub fn resources_for_ids(folder_ids: &[String]) -> Vec<&'static ResourceDef> {
    RESOURCES
        .iter()
        .filter(|r| folder_ids.iter().any(|id| id == r.folder_id))
        .collect()
}

// ── Syncthing device-ID validation (task §16) ───────────────────────────

/// Syncthing device IDs are 52-character base32 encodings grouped by '-'
/// into 7-char chunks (e.g. `XXXXXXX-XXXXXXX-...-XXXXXXX`, 7 groups of 7,
/// plus check digits, canonically 63 chars with 7 dashes). We accept the
/// dashed or undashed form and validate shape, length, and character set.
/// Full Luhn-style check-digit verification is Syncthing-internal; we
/// enforce everything that catches a typo / partial copy reliably.
pub fn validate_device_id(input: &str) -> Result<String, String> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return Err("Device ID is empty.".into());
    }
    // Strip dashes and uppercase — device IDs are case-insensitive base32.
    let compact: String = trimmed
        .chars()
        .filter(|c| *c != '-')
        .map(|c| c.to_ascii_uppercase())
        .collect();

    // Canonical device ID is 52 base32 chars (data + check digits).
    if compact.len() != 52 {
        return Err(format!(
            "Device ID should be 52 characters (got {}). \
             It looks like a partial copy.",
            compact.len()
        ));
    }
    // base32 alphabet (RFC 4648, uppercase, no padding as used by Syncthing).
    if !compact.chars().all(|c| matches!(c, 'A'..='Z' | '2'..='7')) {
        return Err("Device ID contains characters outside the base32 alphabet (A-Z, 2-7).".into());
    }

    // Re-emit in canonical dashed form: 7 groups of 7 separated by '-'.
    // (52 data chars + luhn check digit per 13-char quarter = 56, grouped 7×8.
    //  Syncthing renders 8 groups of 7 = 56 chars total. We normalize to
    //  undashed uppercase for storage; Syncthing accepts both forms.)
    Ok(compact)
}

// ── state/ sync-conflict detection (§15/§16.5) ──────────────────────────

/// True iff `file_name` is a Syncthing conflict copy of a JSON file, e.g.
/// `host-state.sync-conflict-20240101-120000-ABCDEFG.json`.
pub fn is_sync_conflict_json(file_name: &str) -> bool {
    file_name.contains(".sync-conflict-") && file_name.ends_with(".json")
}

/// Scan the local `state/` directory for `.sync-conflict-*.json` files.
/// These are handed to the arbitration layer — the Manager must NOT pick a
/// winner itself; Syncthing's conflict resolution is not ChatBucket's
/// arbitration correctness.
pub fn find_state_conflicts(repo_root: &Path) -> Vec<PathBuf> {
    let state_dir = local_path(repo_root, by_relative_path("state").unwrap());
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&state_dir) {
        for entry in rd.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if is_sync_conflict_json(&name) {
                out.push(entry.path());
            }
        }
    }
    out.sort();
    out
}

// ── ChatBucket-level status classification (task §21/§30) ───────────────

/// The ChatBucket-facing lifecycle state of a single managed folder.
/// Distinct from raw Syncthing JSON — this is what the UI renders.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FolderHealth {
    /// Idle, zero files/bytes needed, no errors.
    InSync,
    /// Actively transferring / scanning / nonzero need.
    Syncing { need_files: i64, need_bytes: i64 },
    /// Folder not present in Syncthing config (and managed:true → drift).
    Missing,
    /// Folder exists but path/settings differ from the ChatBucket standard.
    /// (Produced by reconciliation drift-detection; reserved for the UI.)
    #[allow(dead_code)]
    ConfigMismatch,
    /// Syncthing rejected the API key.
    AuthFailed,
    /// Syncthing process not reachable at all.
    Unreachable,
    /// Folder reports pull errors or an error state.
    SyncError,
    /// A `.sync-conflict-*.json` was detected in this folder (state/ only).
    Conflict,
    /// User set managed:false — intentionally off, not broken.
    Disabled,
    /// No live status available yet.
    Unknown,
}

impl FolderHealth {
    pub fn label(&self) -> String {
        match self {
            FolderHealth::InSync => "In sync".into(),
            FolderHealth::Syncing { need_files, .. } => {
                format!("Syncing · {} file(s) remaining", need_files)
            }
            FolderHealth::Missing => "Missing".into(),
            FolderHealth::ConfigMismatch => "Config mismatch".into(),
            FolderHealth::AuthFailed => "Auth failed".into(),
            FolderHealth::Unreachable => "Syncthing unreachable".into(),
            FolderHealth::SyncError => "Sync error".into(),
            FolderHealth::Conflict => "State conflict".into(),
            FolderHealth::Disabled => "Not synced (disabled)".into(),
            FolderHealth::Unknown => "Unknown".into(),
        }
    }

    /// True for healthy, fully-caught-up states.
    #[allow(dead_code)]
    pub fn is_healthy(&self) -> bool {
        matches!(self, FolderHealth::InSync)
    }
}

/// Classify one folder's health from its managed flag, presence in config,
/// and live DB status. Pure — unit-testable without any HTTP.
///
/// * `managed`: the Manager flag for this resource.
/// * `configured`: does Syncthing currently have this folder ID?
/// * `state`: Syncthing folder state string ("" if unknown).
/// * `need_files`/`need_bytes`/`pull_errors`: from the DB status endpoint.
/// * `has_conflict`: a `.sync-conflict-*.json` was found in this folder.
pub fn classify_folder(
    managed: bool,
    configured: bool,
    state: &str,
    need_files: i64,
    need_bytes: i64,
    pull_errors: i64,
    has_conflict: bool,
) -> FolderHealth {
    if !managed {
        return FolderHealth::Disabled;
    }
    if has_conflict {
        return FolderHealth::Conflict;
    }
    if !configured {
        return FolderHealth::Missing;
    }
    if pull_errors > 0 || state == "error" {
        return FolderHealth::SyncError;
    }
    match state {
        "" => FolderHealth::Unknown,
        "idle" => {
            if need_files == 0 && need_bytes == 0 {
                FolderHealth::InSync
            } else {
                FolderHealth::Syncing {
                    need_files,
                    need_bytes,
                }
            }
        }
        "scanning" | "syncing" | "sync-preparing" | "cleaning" | "scan-waiting"
        | "sync-waiting" => FolderHealth::Syncing {
            need_files,
            need_bytes,
        },
        "paused" => FolderHealth::Unknown, // paused is a deliberate user state
        _ => {
            if need_files == 0 && need_bytes == 0 {
                FolderHealth::InSync
            } else {
                FolderHealth::Syncing {
                    need_files,
                    need_bytes,
                }
            }
        }
    }
}

// ── ChatBucket default folder profile (§18/§19/§20) ─────────────────────

/// The Manager-owned tuning parameters applied to every ChatBucket folder.
/// These are deliberately NOT user-facing — they're ChatBucket defaults.
pub const WATCHER_DELAY_SECS: u32 = 3; // §18: ~3s settling delay
pub const RESCAN_INTERVAL_SECS: u32 = 3600; // conservative periodic safety net
pub const STAGGERED_MAX_AGE_DAYS: u32 = 30; // bounded versioning window
pub const STAGGERED_VERSIONS_PATH: &str = ".stversions";

#[cfg(test)]
mod tests;
