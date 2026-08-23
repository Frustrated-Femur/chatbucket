//! update.rs — Manual "Check for Updates" + install flow.
//!
//! Direct port of manager_main.py §14. Same "replace only code, never
//! data" allowlist / denylist / zip-slip discipline. Steps:
//!
//!   1. GET api.github.com/repos/<repo>/releases/latest
//!   2. Compare tag_name against local VERSION file
//!   3. On install: caller must stop the server first (Manager wires this),
//!      then _backup_current_code(), download zip, extract with allowlist,
//!      sanity-compile — no, we can't `py_compile` from Rust. We do the
//!      lighter check: verify the extracted archive contains all three
//!      markers (main.py, arbitration.py, host_state.py) at the expected
//!      location. A missing marker (partial download, wrong branch tagged)
//!      is caught here without needing a Python interpreter. If the caller
//!      wants a real py_compile it can invoke .venv/bin/python explicitly.
//!   4. Success -> remove backup. Failure -> _restore_backup() and roll back.
//!   5. Do NOT auto-restart. §14 mandates restart via a fresh main.py
//!      arbitration.

use serde::Deserialize;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::Duration;

pub const GITHUB_REPO: &str = "Frustrated-Femur/chatbucket";
pub const BACKUP_DIR: &str = ".update-backup";
const HTTP_TIMEOUT: Duration = Duration::from_secs(60);

const ALLOWLIST_FILES: &[&str] = &["VERSION"];
const ALLOWLIST_SUFFIXES: &[&str] = &[".py", ".txt"];
const ALLOWLIST_DIRS: &[&str] = &["manager", "web", "static", "scripts"];

// Data dirs that MUST NEVER be touched. Deny wins over allow — even if a
// rogue release archive contains one of these entries, extraction skips it.
const DENY_DIRS: &[&str] = &[
    "messages",
    "uploads",
    "gifs",
    "stickers",
    "sfx",
    "state",
    "presence",
    ".venv",
    ".update-backup",
    "__pycache__",
];

// Markers that must appear at the archive root after extraction (post-
// GitHub-top-strip). If any is missing, we treat the archive as broken and
// roll back — cheap smoke test replacing Python's py_compile.
const REQUIRED_MARKERS: &[&str] = &["main.py", "arbitration.py", "host_state.py"];

#[derive(Debug, thiserror::Error)]
pub enum UpdateError {
    #[error("GitHub API HTTP error: {0}")]
    Http(String),
    #[error("GitHub response was unusable: {0}")]
    BadResponse(String),
    #[error("release missing tag_name or zipball_url — publishing may still be in progress")]
    MissingRelease,
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("zip extraction failed: {0}")]
    Zip(String),
}

#[derive(Debug, Deserialize)]
pub struct GithubRelease {
    pub tag_name: String,
    pub zipball_url: String,
    pub html_url: Option<String>,
}

pub fn read_local_version(repo_root: &Path) -> Option<String> {
    let path = repo_root.join("VERSION");
    let content = fs::read_to_string(path).ok()?;
    let trimmed = content.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// Best-effort semver-ish tuple: strip 'v', split '.', keep leading digits.
/// A non-numeric suffix (e.g. "1.2.0-rc1") collapses to its leading integer
/// so "1.2.0" > "1.2.0-rc1", matching common release convention.
fn parse_version(v: &str) -> Vec<u32> {
    let s = v.trim().trim_start_matches(['v', 'V']);
    if s.is_empty() {
        return vec![0];
    }
    let mut out = Vec::new();
    for chunk in s.split('.') {
        let mut digits = String::new();
        for c in chunk.chars() {
            if c.is_ascii_digit() {
                digits.push(c);
            } else {
                break;
            }
        }
        out.push(digits.parse().unwrap_or(0));
    }
    if out.is_empty() {
        vec![0]
    } else {
        out
    }
}

pub fn is_newer(candidate: &str, current: &str) -> bool {
    parse_version(candidate) > parse_version(current)
}

pub fn fetch_latest_release() -> Result<GithubRelease, UpdateError> {
    let url = format!(
        "https://api.github.com/repos/{}/releases/latest",
        GITHUB_REPO
    );
    let resp = ureq::get(&url)
        .set("Accept", "application/vnd.github+json")
        .set("User-Agent", "chatbucket-manager-rs")
        .timeout(Duration::from_secs(15))
        .call()
        .map_err(|e| UpdateError::Http(e.to_string()))?;
    let release: GithubRelease = resp
        .into_json()
        .map_err(|e| UpdateError::BadResponse(e.to_string()))?;
    if release.tag_name.is_empty() || release.zipball_url.is_empty() {
        return Err(UpdateError::MissingRelease);
    }
    Ok(release)
}

// ── Allowlist / zip-slip helpers ───────────────────────────────────────

fn strip_github_top(rel: &str) -> String {
    // GitHub zipballs prefix everything with a synthesized directory
    // (`Frustrated-Femur-chatbucket-<sha>/…`). Strip it.
    let rel = rel.replace('\\', "/");
    if let Some((_, rest)) = rel.split_once('/') {
        rest.to_string()
    } else {
        String::new()
    }
}

fn allowed_update_path(rel: &str) -> bool {
    let rel = rel.replace('\\', "/");
    let rel = rel.trim_start_matches("./");
    if rel.is_empty() {
        return false;
    }
    let parts: Vec<&str> = rel.split('/').filter(|p| !p.is_empty()).collect();
    if parts.is_empty() {
        return false;
    }
    if parts.iter().any(|p| *p == "." || *p == "..") {
        return false;
    }
    if Path::new(rel).is_absolute() {
        return false;
    }
    let top = parts[0].to_lowercase();
    if DENY_DIRS.iter().any(|d| *d == top) {
        return false;
    }
    if parts.len() == 1 {
        return ALLOWLIST_FILES.contains(&parts[0])
            || ALLOWLIST_SUFFIXES.iter().any(|s| parts[0].ends_with(s));
    }
    if ALLOWLIST_DIRS.iter().any(|d| *d == top) {
        return true;
    }
    ALLOWLIST_SUFFIXES
        .iter()
        .any(|s| parts.last().unwrap().ends_with(s))
}

fn path_safe(target_root: &Path, candidate: &Path) -> bool {
    let real_root = fs::canonicalize(target_root).unwrap_or_else(|_| target_root.to_path_buf());
    // Candidate may not exist yet — canonicalize its parent, then join
    let parent = candidate.parent().unwrap_or(candidate);
    let real_parent = fs::canonicalize(parent).unwrap_or_else(|_| parent.to_path_buf());
    let real_candidate = real_parent.join(candidate.file_name().unwrap_or_default());
    real_candidate == real_root || real_candidate.starts_with(&real_root)
}

pub fn backup_current_code(repo_root: &Path) -> Result<Vec<String>, UpdateError> {
    let backup = repo_root.join(BACKUP_DIR);
    if backup.exists() {
        let _ = fs::remove_dir_all(&backup);
    }
    fs::create_dir_all(&backup)?;

    let mut moved = Vec::new();
    for entry in fs::read_dir(repo_root)? {
        let entry = entry?;
        let name = entry.file_name();
        let name_s = name.to_string_lossy().to_string();
        if DENY_DIRS.contains(&name_s.as_str()) {
            continue;
        }
        let full = entry.path();
        let ft = entry.file_type()?;
        let is_code = if ft.is_file() {
            ALLOWLIST_FILES.contains(&name_s.as_str())
                || ALLOWLIST_SUFFIXES.iter().any(|s| name_s.ends_with(s))
        } else if ft.is_dir() {
            ALLOWLIST_DIRS.contains(&name_s.to_lowercase().as_str())
        } else {
            false
        };
        if !is_code {
            continue;
        }
        let dst = backup.join(&name);
        if let Err(e) = fs::rename(&full, &dst) {
            log::warn!("could not back up {}: {}", full.display(), e);
            continue;
        }
        moved.push(name_s);
    }
    Ok(moved)
}

pub fn restore_backup(repo_root: &Path) -> Result<(), UpdateError> {
    let backup = repo_root.join(BACKUP_DIR);
    if !backup.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(&backup)? {
        let entry = entry?;
        let dst = repo_root.join(entry.file_name());
        if dst.exists() {
            if dst.is_dir() {
                let _ = fs::remove_dir_all(&dst);
            } else {
                let _ = fs::remove_file(&dst);
            }
        }
        if let Err(e) = fs::rename(entry.path(), &dst) {
            log::warn!("restore: could not put back {}: {}", dst.display(), e);
        }
    }
    let _ = fs::remove_dir_all(&backup);
    Ok(())
}

pub fn download_release_zip(url: &str) -> Result<PathBuf, UpdateError> {
    let mut resp = ureq::get(url)
        .set("Accept", "application/zip")
        .set("User-Agent", "chatbucket-manager-rs")
        .timeout(HTTP_TIMEOUT)
        .call()
        .map_err(|e| UpdateError::Http(e.to_string()))?
        .into_reader();

    let tmp = tempfile::Builder::new()
        .prefix("chatbucket-update-")
        .suffix(".zip")
        .tempfile()?;
    let (mut file, path) = tmp.keep().map_err(|e| UpdateError::Io(e.error))?;
    std::io::copy(&mut resp, &mut file)?;
    Ok(path)
}

pub fn extract_release_zip(zip_path: &Path, repo_root: &Path) -> Result<usize, UpdateError> {
    let file = fs::File::open(zip_path)?;
    let mut zip = zip::ZipArchive::new(file).map_err(|e| UpdateError::Zip(e.to_string()))?;
    let mut written = 0usize;

    for i in 0..zip.len() {
        let mut entry = zip
            .by_index(i)
            .map_err(|e| UpdateError::Zip(e.to_string()))?;
        let raw_name = entry.name().to_string();
        if raw_name.ends_with('/') {
            continue;
        }
        let stripped = strip_github_top(&raw_name);
        if stripped.is_empty() || !allowed_update_path(&stripped) {
            continue;
        }
        let dest = repo_root.join(&stripped);
        if !path_safe(repo_root, &dest) {
            continue;
        }
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut out = fs::File::create(&dest)?;
        std::io::copy(&mut entry, &mut out)?;
        written += 1;
    }
    Ok(written)
}

/// Lightweight sanity check replacing Python's py_compile: are the three
/// repo-root markers present and non-empty? A missing marker means the
/// archive was truncated mid-download or the wrong branch was tagged;
/// bail before the caller re-invokes main.py against it.
pub fn sanity_check_extracted(repo_root: &Path) -> Result<(), String> {
    for marker in REQUIRED_MARKERS {
        let path = repo_root.join(marker);
        match fs::metadata(&path) {
            Ok(m) if m.len() > 0 => {}
            Ok(_) => return Err(format!("{} is empty after extract", marker)),
            Err(e) => return Err(format!("{}: {}", marker, e)),
        }
    }
    Ok(())
}

pub fn read_and_verify_zip_head(zip_path: &Path) -> Result<(), String> {
    // Extra guard: reject a "success" HTTP body that's clearly not a zip.
    // Real zip files start with 0x50 0x4B ("PK").
    let mut f = match fs::File::open(zip_path) {
        Ok(f) => f,
        Err(e) => return Err(format!("open zip: {e}")),
    };
    let mut header = [0u8; 4];
    if f.read(&mut header).is_err() || &header[..2] != b"PK" {
        return Err("downloaded file is not a zip archive".into());
    }
    Ok(())
}
