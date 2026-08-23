//! arbitration.rs — Tailscale liveness + peer discovery + health-check.
//!
//! Port of the tailscale-status section of Python `arbitration.py`
//! (specifically the `arbitration_tailscale_section.py` rewrite that
//! fixed the "reachable devices reporting Offline" issue). The Manager
//! never invokes the full decision-tree arbitration itself — that lives
//! in `main.py` and runs once at process launch. The Manager only needs
//! the read-only helpers: is a named machine online, list all real tailnet
//! peers (excluding Tailscale-operated infrastructure without DNSName),
//! and does an HTTP GET to /health succeed.
//!
//! Single fetch/parse path (`fetch_tailscale_status()`) shared by every
//! caller — same "one place that knows how to invoke the CLI and parse
//! its output" boundary the Python code establishes.

use serde::Deserialize;
use std::process::Command;
use std::time::Duration;

pub const TAILNET_SUFFIX: &str = "tail888cf2.ts.net";
// The next three constants back `health_check()`, which the Manager's
// read-only path does not call (arbitration itself lives in main.py). They
// are kept as part of the protocol surface so the constants stay in one place.
#[allow(dead_code)]
pub const APP_PORT: u16 = 5000;
#[allow(dead_code)]
pub const SCHEME: &str = "http"; // see main.py's note — flip only once server.py terminates TLS
#[allow(dead_code)]
pub const HEALTH_CHECK_TIMEOUT: Duration = Duration::from_secs(3);
pub const TAILSCALE_CLI_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, thiserror::Error, Clone)]
pub enum ArbitrationError {
    #[error("`tailscale` binary not found in PATH")]
    CliMissing,
    #[error("`tailscale status --json` timed out")]
    Timeout,
    #[error("`tailscale status --json` failed: {0}")]
    CliFailed(String),
    #[error("tailscale status returned invalid JSON: {0}")]
    BadJson(String),
    #[error("Machine '{0}' not found in `tailscale status` peer list")]
    UnknownMachine(String),
}

#[derive(Debug, Deserialize)]
struct TailscalePeer {
    #[serde(rename = "DNSName", default)]
    dns_name: String,
    #[serde(rename = "HostName", default)]
    host_name: String,
    #[serde(rename = "Online", default)]
    online: bool,
}

#[derive(Debug, Deserialize)]
struct TailscaleStatus {
    #[serde(rename = "Peer", default)]
    peer: std::collections::HashMap<String, TailscalePeer>,
}

/// One place that shells out to `tailscale status --json`. Every other
/// function here calls THIS. Never reparse tailscale output anywhere else.
fn fetch_tailscale_status() -> Result<TailscaleStatus, ArbitrationError> {
    // std::process::Command has no built-in timeout, but `tailscale status
    // --json` returns near-instantly under normal conditions. For the rare
    // hung-daemon case we spawn on a thread with a channel and timeout —
    // matches the Python code's subprocess timeout guarantee.
    use std::sync::mpsc;

    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let out = Command::new("tailscale")
            .args(["status", "--json"])
            .output();
        let _ = tx.send(out);
    });

    let result = match rx.recv_timeout(TAILSCALE_CLI_TIMEOUT) {
        Ok(r) => r,
        Err(_) => return Err(ArbitrationError::Timeout),
    };

    let out = match result {
        Ok(o) => o,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(ArbitrationError::CliMissing)
        }
        Err(e) => return Err(ArbitrationError::CliFailed(e.to_string())),
    };

    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr).to_string();
        return Err(ArbitrationError::CliFailed(stderr));
    }

    serde_json::from_slice::<TailscaleStatus>(&out.stdout)
        .map_err(|e| ArbitrationError::BadJson(e.to_string()))
}

/// True iff `machine_name` shows Online in a fresh `tailscale status`.
///
/// Deliberately NOT catch-all: a broken tailscale CLI locally says nothing
/// about whether the remote peer is alive. Swallowing that into "assume
/// offline" would cause this machine to self-elect host next to a healthy
/// real host — the worst kind of split-brain, caused by a local tooling
/// problem rather than a real peer failure. Surface, don't guess.
pub fn check_machine_online(machine_name: &str) -> Result<bool, ArbitrationError> {
    let status = fetch_tailscale_status()?;
    let target = machine_name.to_lowercase();

    for peer in status.peer.values() {
        let dns = peer.dns_name.trim_end_matches('.').to_lowercase();
        if dns.starts_with(&format!("{}.", target)) {
            return Ok(peer.online);
        }
    }
    Err(ArbitrationError::UnknownMachine(machine_name.to_string()))
}

#[derive(Debug, Clone)]
pub struct PeerInfo {
    pub name: String,
    pub online: bool,
}

#[derive(Debug, Clone, Default)]
pub struct PeerList {
    pub peers: Vec<PeerInfo>,
    pub hidden_count: u32,
}

/// Everything Tailscale reports as an actual member device. Peers with no
/// DNSName are Tailscale-operated infrastructure (Funnel ingress relay,
/// confirmed empirically as HostName="funnel-ingress-node", DNSName="") and
/// are counted separately, never silently dropped — same "+N infrastructure
/// peers hidden" note the Python code produces.
pub fn list_tailnet_peers() -> Result<PeerList, ArbitrationError> {
    let status = fetch_tailscale_status()?;
    let suffix = format!(".{}", TAILNET_SUFFIX);
    let mut peers = Vec::new();
    let mut hidden_count = 0u32;

    for peer in status.peer.values() {
        let dns = peer.dns_name.trim_end_matches('.').to_string();
        if dns.is_empty() {
            hidden_count += 1;
            continue;
        }
        let name = if dns.to_lowercase().ends_with(&suffix.to_lowercase()) {
            dns[..dns.len() - suffix.len()].to_string()
        } else {
            dns
        };
        peers.push(PeerInfo {
            name,
            online: peer.online,
        });
        // silence unused-field warning
        let _ = &peer.host_name;
    }

    Ok(PeerList {
        peers,
        hidden_count,
    })
}

/// True iff HTTP GET to `machine_name`'s /health returns 200. Any failure
/// (connection refused, timeout, DNS, non-200) is False — from the caller's
/// point of view all failure modes mean the same thing: "don't trust this
/// claim, no live server backs it."
#[allow(dead_code)] // used by the arbitration protocol (main.py), not the Manager's read path
pub fn health_check(machine_name: &str) -> bool {
    let url = format!(
        "{}://{}.{}:{}/health",
        SCHEME, machine_name, TAILNET_SUFFIX, APP_PORT
    );
    match ureq::get(&url).timeout(HEALTH_CHECK_TIMEOUT).call() {
        Ok(resp) => resp.status() == 200,
        Err(_) => false,
    }
}
