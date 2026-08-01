//! repo.rs — Content-based repo-root discovery.
//!
//! Direct port of Python's `_find_repo_root()` (manager_main.py §16.5 fix): walk
//! upward from a starting path until a directory containing arbitration.py,
//! host_state.py, AND main.py together is found. Content-based, not
//! fixed-depth — correct regardless of where this binary sits, what the repo
//! folder is named, or whether it moves. §10 of the architecture doc gets its
//! "installer target path is not hardcoded" property directly from this.

use std::path::{Path, PathBuf};

const MARKERS: &[&str] = &["arbitration.py", "host_state.py", "main.py"];

/// Walk upward from `start` until a directory holds all `MARKERS`.
pub fn find_repo_root<P: AsRef<Path>>(start: P) -> anyhow::Result<PathBuf> {
    let mut current = std::fs::canonicalize(start.as_ref())
        .unwrap_or_else(|_| start.as_ref().to_path_buf());
    if current.is_file() {
        current = current
            .parent()
            .map(|p| p.to_path_buf())
            .unwrap_or(current);
    }

    loop {
        if MARKERS.iter().all(|m| current.join(m).is_file()) {
            return Ok(current);
        }
        match current.parent() {
            Some(parent) if parent != current => current = parent.to_path_buf(),
            _ => {
                anyhow::bail!(
                    "could not locate ChatBucket repo root (need {:?} together)",
                    MARKERS
                )
            }
        }
    }
}

/// Start search from the current exe's directory, then fall back to CWD.
/// This matches the Python code's implicit "run from anywhere in the repo"
/// contract: the Python module lives at repo root, so `__file__` is right
/// next to the markers. The Rust binary may live in `target/release/` or
/// `%USERPROFILE%\ChatBucket\`, so we try both.
pub fn find_repo_root_from_exe() -> anyhow::Result<PathBuf> {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            if let Ok(root) = find_repo_root(dir) {
                return Ok(root);
            }
        }
    }
    let cwd = std::env::current_dir()?;
    find_repo_root(cwd)
}

/// Normalise a filesystem path for equality comparison. Handles case
/// (Windows), separators, symlinks, trailing slashes — same reasons
/// `_norm()` in manager_main.py exists (symlink /home/user vs /home/ankit
/// made cwd-based matching silently fail).
pub fn norm(p: &Path) -> PathBuf {
    let canonical = std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
    // Windows path comparison is case-insensitive; PathBuf handles separators.
    #[cfg(windows)]
    {
        let s = canonical.to_string_lossy().to_lowercase();
        PathBuf::from(s)
    }
    #[cfg(not(windows))]
    {
        canonical
    }
}

pub fn paths_equal(a: &Path, b: &Path) -> bool {
    norm(a) == norm(b)
}

pub fn path_under(child: &Path, root: &Path) -> bool {
    let child_n = norm(child);
    let root_n = norm(root);
    child_n == root_n || child_n.starts_with(&root_n)
}
