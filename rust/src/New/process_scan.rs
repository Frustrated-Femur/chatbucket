//! process_scan.rs — Find ChatBucket lifecycle processes.
//!
//! Direct port of Python `_iter_chatbucket_procs()` /
//! `find_chatbucket_process()` / `_classify_cmdline()`. Handles the same
//! three cmdline shapes across the SAME logical instance (PID preserved
//! by os.execv()):
//!
//!   1. `main.py`     -> role "arbitrating" (pre-exec — jitter + health-check)
//!   2. `server.py` OR `gunicorn ... server:app` -> role "host" (post-exec)
//!   3. `doorman.py`  -> role "client" (post-exec)
//!
//! Gunicorn master vs worker discrimination: master carries -k/-w/-b/--bind
//! flags in its argv; workers just show `gunicorn: worker [server:app]` and
//! must NOT be signalled directly (master respawns them — root cause of the
//! "did not complete within 8s" symptom in the Python code before it was
//! fixed).

use crate::repo;
use std::path::{Path, PathBuf};
use sysinfo::{Pid, ProcessRefreshKind, RefreshKind, System};

pub const ARBITRATION_STALE_SECONDS: u64 = 45;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProcRole {
    Host,
    Client,
    Arbitrating,
}

impl ProcRole {
    pub fn as_str(&self) -> &'static str {
        match self {
            ProcRole::Host => "host",
            ProcRole::Client => "client",
            ProcRole::Arbitrating => "arbitrating",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SubShape {
    GunicornMaster,
    GunicornWorker,
    PythonServer,
    Doorman,
    Arbitrating,
}

#[derive(Debug, Clone)]
pub struct ChatBucketProc {
    pub pid: u32,
    /// Parent pid — used when resolving gunicorn master/worker trees.
    #[allow(dead_code)]
    pub ppid: Option<u32>,
    pub role: ProcRole,
    pub subshape: SubShape,
    /// Full argv, retained for diagnostics of the classification decision.
    #[allow(dead_code)]
    pub cmdline: Vec<String>,
    pub create_time_secs: u64, // epoch seconds
    /// Working dir, retained for the repo-root-anchored identity check.
    #[allow(dead_code)]
    pub cwd: Option<PathBuf>,
}

fn cmdline_matches(joined_lower: &str, script_basename: &str) -> bool {
    // Reject substring-only mentions: require the token to appear as its own
    // word (either bare basename or path tail). Same reasoning as Python's
    // `_cmdline_matches()` — otherwise `grep main.py` piped into ps would
    // match, and so would an editor's file-open list.
    if !joined_lower.contains(script_basename) {
        return false;
    }
    for token in joined_lower.split_whitespace() {
        let tail = token.rsplit(['/', '\\']).next().unwrap_or(token);
        if tail == script_basename {
            return true;
        }
    }
    false
}

fn classify_cmdline(joined_lower: &str) -> Option<(ProcRole, SubShape)> {
    let tokens: Vec<&str> = joined_lower.split_whitespace().collect();
    let is_gunicorn = joined_lower.contains("gunicorn") && joined_lower.contains("server:app");
    if is_gunicorn {
        let looks_like_master = tokens.contains(&"-k")
            || joined_lower.contains("--worker-class")
            || tokens.contains(&"-w")
            || tokens.contains(&"-b")
            || joined_lower.contains("--bind");
        return Some(if looks_like_master {
            (ProcRole::Host, SubShape::GunicornMaster)
        } else {
            (ProcRole::Host, SubShape::GunicornWorker)
        });
    }
    if joined_lower.contains("server.py") {
        return Some((ProcRole::Host, SubShape::PythonServer));
    }
    if cmdline_matches(joined_lower, "doorman.py") {
        return Some((ProcRole::Client, SubShape::Doorman));
    }
    if cmdline_matches(joined_lower, "main.py") {
        return Some((ProcRole::Arbitrating, SubShape::Arbitrating));
    }
    None
}

/// Iterate every psutil-equivalent process that looks like a ChatBucket
/// lifecycle process under `repo_root`. Includes workers, arbitrating
/// pre-exec shape, and any duplicates. Higher-level callers reduce this
/// to a single canonical target.
pub fn iter_chatbucket_procs(repo_root: &Path) -> Vec<ChatBucketProc> {
    let mut sys = System::new_with_specifics(
        RefreshKind::new().with_processes(ProcessRefreshKind::everything()),
    );
    sys.refresh_processes();

    let mut out = Vec::new();

    for (pid, proc) in sys.processes() {
        let cmdline_vec: Vec<String> = proc.cmd().to_vec();
        if cmdline_vec.is_empty() {
            continue;
        }
        let joined = cmdline_vec.join(" ").to_lowercase();

        let cwd = proc.cwd().map(|p| p.to_path_buf());
        let cwd_matches = match &cwd {
            Some(c) => repo::paths_equal(c, repo_root),
            None => false,
        };

        // Match if cmdline references any script that resolves inside
        // repo_root — closes the case where cwd is unavailable (permission
        // denied on another user's process on Linux, some sysinfo edge
        // cases on Windows).
        let mut script_under_repo = false;
        for token in &cmdline_vec {
            if token.is_empty() {
                continue;
            }
            let looks_like_path =
                token.contains('/') || token.contains('\\') || token.ends_with(".py");
            if !looks_like_path {
                continue;
            }
            let abs = if Path::new(token).is_absolute() {
                PathBuf::from(token)
            } else {
                cwd.clone()
                    .unwrap_or_else(|| repo_root.to_path_buf())
                    .join(token)
            };
            if repo::path_under(&abs, repo_root) {
                script_under_repo = true;
                break;
            }
        }

        if !(cwd_matches || script_under_repo) {
            continue;
        }

        let Some((role, subshape)) = classify_cmdline(&joined) else {
            continue;
        };

        out.push(ChatBucketProc {
            pid: pid.as_u32(),
            ppid: proc.parent().map(|p| p.as_u32()),
            role,
            subshape,
            cmdline: cmdline_vec,
            create_time_secs: proc.start_time(),
            cwd,
        });
    }

    out
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Pick the canonical ChatBucket process. Priority order:
///   0 = gunicorn master / direct python server / doorman
///   1 = gunicorn worker (visible but never the canonical target)
///   2 = arbitrating (main.py, pre-exec)
/// Stale-arbitrating processes (older than ARBITRATION_STALE_SECONDS with
/// role=arbitrating) are dropped — they're leftovers from a crashed launch
/// that would otherwise re-poison every fresh Start attempt.
pub fn find_chatbucket_process(repo_root: &Path) -> Option<ChatBucketProc> {
    let now = now_secs();
    let mut candidates: Vec<ChatBucketProc> = iter_chatbucket_procs(repo_root)
        .into_iter()
        .filter(|p| {
            if p.role == ProcRole::Arbitrating && p.create_time_secs > 0 {
                (now.saturating_sub(p.create_time_secs)) <= ARBITRATION_STALE_SECONDS
            } else {
                true
            }
        })
        .collect();

    if candidates.is_empty() {
        return None;
    }

    fn rank(c: &ChatBucketProc) -> u8 {
        match (&c.role, &c.subshape) {
            (ProcRole::Host, SubShape::GunicornWorker) => 1,
            (ProcRole::Host, _) | (ProcRole::Client, _) => 0,
            (ProcRole::Arbitrating, _) => 2,
        }
    }

    candidates.sort_by(|a, b| {
        rank(a)
            .cmp(&rank(b))
            .then_with(|| b.create_time_secs.cmp(&a.create_time_secs))
    });
    candidates.into_iter().next()
}

/// Walk up parent chain from a gunicorn worker until we hit its master,
/// bounded to 6 hops. Same logic as Python `_resolve_stop_target()`.
pub fn resolve_master(pid: u32, repo_root: &Path) -> u32 {
    let mut sys = System::new_with_specifics(
        RefreshKind::new().with_processes(ProcessRefreshKind::everything()),
    );
    sys.refresh_processes();

    let mut cursor = Pid::from_u32(pid);
    for _ in 0..6 {
        let Some(proc) = sys.process(cursor) else {
            break;
        };
        let Some(parent_pid) = proc.parent() else {
            break;
        };
        let Some(parent) = sys.process(parent_pid) else {
            break;
        };
        let joined = parent.cmd().join(" ").to_lowercase();
        if let Some((role, sub)) = classify_cmdline(&joined) {
            if role == ProcRole::Host && sub == SubShape::GunicornMaster {
                // Confirm it's actually under the repo (defence in depth).
                let cwd_ok = parent
                    .cwd()
                    .map(|c| repo::paths_equal(c, repo_root))
                    .unwrap_or(true);
                if cwd_ok {
                    return parent_pid.as_u32();
                }
            }
        }
        cursor = parent_pid;
    }
    pid
}

/// Kill leftover wedged-arbitrating main.py processes (role=arbitrating
/// older than ARBITRATION_STALE_SECONDS). Matches Python's
/// `_kill_stale_arbitrators()` — deliberately narrow, never touches a real
/// host/client process.
pub fn kill_stale_arbitrators(repo_root: &Path) {
    let now = now_secs();
    for p in iter_chatbucket_procs(repo_root) {
        if p.role != ProcRole::Arbitrating {
            continue;
        }
        if p.create_time_secs == 0
            || now.saturating_sub(p.create_time_secs) <= ARBITRATION_STALE_SECONDS
        {
            continue;
        }
        force_kill_pid(p.pid);
    }
}

pub fn pid_is_alive(pid: u32) -> bool {
    let mut sys =
        System::new_with_specifics(RefreshKind::new().with_processes(ProcessRefreshKind::new()));
    sys.refresh_processes();
    sys.process(Pid::from_u32(pid)).is_some()
}

/// Wait until every pid is gone (or a zombie — treated as gone; matches
/// Python's `_wait_for_all_gone()` handling for Linux zombies still
/// answering pid_exists() as True until wait()ed).
pub fn wait_for_all_gone(pids: &[u32], timeout_secs: u64) -> bool {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
    let poll = std::time::Duration::from_millis(400);
    loop {
        let any_alive = pids.iter().any(|&p| pid_is_alive(p));
        if !any_alive {
            return true;
        }
        if std::time::Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(poll);
    }
}

// ── platform-specific signalling ───────────────────────────────────────

#[cfg(unix)]
pub fn send_sigterm_pgid(pgid: i32) -> bool {
    use nix::sys::signal::{killpg, Signal};
    use nix::unistd::Pid;
    killpg(Pid::from_raw(pgid), Signal::SIGTERM).is_ok()
}

#[cfg(unix)]
pub fn send_sigterm_pid(pid: u32) -> bool {
    use nix::sys::signal::{kill, Signal};
    use nix::unistd::Pid;
    kill(Pid::from_raw(pid as i32), Signal::SIGTERM).is_ok()
}

#[cfg(unix)]
pub fn get_pgid(pid: u32) -> Option<i32> {
    // Only return a pgid when the target IS its own group leader (pgid ==
    // its own pid). Guaranteed true for anything we spawn (setsid); NOT
    // guaranteed for an externally-started instance that shares its
    // terminal's process group with unrelated jobs. Same discipline as
    // Python's `_resolve_stop_target()`.
    let rc = unsafe { libc::getpgid(pid as libc::pid_t) };
    if rc < 0 {
        return None;
    }
    if rc as u32 == pid {
        Some(rc)
    } else {
        None
    }
}

#[cfg(windows)]
pub fn send_ctrl_break_windows(pid: u32) -> bool {
    // Only reliable if the target was created with CREATE_NEW_PROCESS_GROUP.
    // Return value is NOT trusted as proof — caller always verifies actual
    // exit and force-kills with a visible warning if this didn't work.
    unsafe {
        let ok =
            winapi::um::wincon::GenerateConsoleCtrlEvent(winapi::um::wincon::CTRL_BREAK_EVENT, pid);
        ok != 0
    }
}

pub fn force_kill_pid(pid: u32) {
    let mut sys =
        System::new_with_specifics(RefreshKind::new().with_processes(ProcessRefreshKind::new()));
    sys.refresh_processes();
    if let Some(p) = sys.process(Pid::from_u32(pid)) {
        p.kill();
    }
}
