//! arbitration.rs — Tailscale liveness + peer discovery + health-check.
//!
//! Port of the tailscale-status section of Python `arbitration.py`. The
//! Manager never invokes the full decision-tree arbitration itself — that
//! lives in `front_door.py` and runs from the control thread. The Manager
//! only needs the read-only helpers: is a named machine online, list all
//! real tailnet peers (excluding Tailscale-operated infrastructure without
//! DNSName), including each peer's IPv4 address (front-door integration
//! §3), and the /health primitive (kept as protocol surface even though
//! the Manager's read path doesn't call it).
//!
//! Single fetch/parse path (`fetch_tailscale_status()`) shared by every
//! caller — same "one place that knows how to invoke the CLI and parse
//! its output" boundary the Python code establishes.

use serde::Deserialize;
use std::process::Command;
use std::time::Duration;

pub const TAILNET_SUFFIX: &str = "tail888cf2.ts.net";
// The next three constants back `health_check()`, which the Manager's
// read-only path does not call (arbitration itself lives in the Python
// front door). They are kept as part of the protocol surface so the
// constants stay in one place.
#[allow(dead_code)]
pub const APP_PORT: u16 = 5000;
#[allow(dead_code)]
pub const SCHEME: &str = "http"; // see arbitration.py — flip only once server.py terminates TLS
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

// ── Raw wire types (only the fields we consume) ─────────────────────────

#[derive(Debug, Deserialize)]
struct TailscalePeer {
    #[serde(rename = "DNSName", default)]
    dns_name: String,
    #[serde(rename = "HostName", default)]
    host_name: String,
    #[serde(rename = "Online", default)]
    online: bool,
    /// `tailscale status --json` returns EVERY IP the peer advertises here
    /// — normally one IPv4 (100.x.y.z) and one IPv6 (fd7a:…) — in the
    /// order Tailscale saw fit to list them. We must NOT assume [0] is
    /// IPv4: on some peers the array comes back v6-first. Distinguish by
    /// content, not position. §3 of the integration design is explicit
    /// about this being the failure mode of the naive `[0]` approach.
    #[serde(rename = "TailscaleIPs", default)]
    tailscale_ips: Vec<String>,
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

/// One tailnet peer, in ChatBucket terms.
#[derive(Debug, Clone)]
pub struct PeerInfo {
    pub name: String,
    pub online: bool,
    /// First IPv4 the peer advertises in `TailscaleIPs`. None only if the
    /// peer has NO IPv4 at all (extremely rare — IPv6-only Tailscale
    /// nodes). Distinguished from IPv6 by content, not position — see
    /// `extract_ipv4()` below.
    pub ipv4: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct PeerList {
    pub peers: Vec<PeerInfo>,
    pub hidden_count: u32,
}

/// Everything Tailscale reports as an actual member device. Peers with no
/// DNSName are Tailscale-operated infrastructure (Funnel ingress relay,
/// confirmed empirically as HostName="funnel-ingress-node", DNSName="")
/// and are counted separately, never silently dropped — same "+N
/// infrastructure peers hidden" note the Python code produces.
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
            ipv4: extract_ipv4(&peer.tailscale_ips),
        });
        // silence unused-field warning — HostName is part of the parsed
        // shape for potential future use (Funnel node classification).
        let _ = &peer.host_name;
    }

    Ok(PeerList {
        peers,
        hidden_count,
    })
}

/// Pull the first IPv4 out of a `TailscaleIPs` array. Distinguishes by
/// CONTENT (colonless = IPv4), not by position — Tailscale sometimes
/// orders the array v6-first, and a plain `[0]` would then hand back
/// "fd7a:…" as this peer's "IPv4". Returns None only if the peer has no
/// IPv4 at all.
///
/// Public for testability — the .md's §7 asks for this exact function
/// to be covered against both array orders and the missing-field case.
pub fn extract_ipv4(ips: &[String]) -> Option<String> {
    for ip in ips {
        let trimmed = ip.trim();
        if trimmed.is_empty() {
            continue;
        }
        // IPv6 addresses contain colons; IPv4 addresses never do. Cheaper
        // and more permissive than a full parse — we don't need to
        // reject malformed strings, just prefer the v4-shaped ones.
        if !trimmed.contains(':') {
            return Some(trimmed.to_string());
        }
    }
    None
}

/// True iff HTTP GET to `machine_name`'s /health returns 200. Any failure
/// (connection refused, timeout, DNS, non-200) is False — from the caller's
/// point of view all failure modes mean the same thing: "don't trust this
/// claim, no live server backs it."
#[allow(dead_code)] // used by the arbitration protocol (front_door.py), not the Manager
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

// ── Tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extract_ipv4_v4_first_order() {
        // The "natural" Tailscale ordering: IPv4 then IPv6.
        let ips = vec!["100.101.102.103".to_string(), "fd7a:115c::1".to_string()];
        assert_eq!(extract_ipv4(&ips), Some("100.101.102.103".to_string()));
    }

    #[test]
    fn extract_ipv4_v6_first_order() {
        // Regression test for the naive `[0]` bug: some peers come back
        // v6-first, and picking [0] would hand v6 back as "IPv4". This
        // test is the design doc's §3 stated regression case.
        let ips = vec!["fd7a:115c::1".to_string(), "100.101.102.103".to_string()];
        assert_eq!(extract_ipv4(&ips), Some("100.101.102.103".to_string()));
    }

    #[test]
    fn extract_ipv4_missing_returns_none() {
        // No IPv4 at all — IPv6-only Tailscale peer (rare but valid).
        let ips = vec!["fd7a:115c::1".to_string()];
        assert_eq!(extract_ipv4(&ips), None);
        // Empty array.
        let ips: Vec<String> = vec![];
        assert_eq!(extract_ipv4(&ips), None);
        // Empty strings are skipped rather than treated as valid.
        let ips = vec!["".to_string(), "  ".to_string()];
        assert_eq!(extract_ipv4(&ips), None);
    }

    #[test]
    fn extract_ipv4_trims_whitespace() {
        let ips = vec!["  100.64.0.1  ".to_string()];
        assert_eq!(extract_ipv4(&ips), Some("100.64.0.1".to_string()));
    }

    #[test]
    fn peer_deserialize_includes_tailscale_ips() {
        // Belt-and-braces: confirm the serde rename picks the field up.
        let json = r#"{
            "DNSName":"archlinux.tail888cf2.ts.net.",
            "HostName":"archlinux",
            "Online":true,
            "TailscaleIPs":["100.64.1.2","fd7a:115c::2"]
        }"#;
        let p: TailscalePeer = serde_json::from_str(json).unwrap();
        assert_eq!(p.tailscale_ips, vec!["100.64.1.2", "fd7a:115c::2"]);
        assert_eq!(
            extract_ipv4(&p.tailscale_ips),
            Some("100.64.1.2".to_string())
        );
    }

    #[test]
    fn peer_deserialize_missing_tailscale_ips_defaults_to_empty() {
        let json = r#"{
            "DNSName":"archlinux.tail888cf2.ts.net.",
            "HostName":"archlinux",
            "Online":true
        }"#;
        let p: TailscalePeer = serde_json::from_str(json).unwrap();
        assert!(p.tailscale_ips.is_empty());
        assert_eq!(extract_ipv4(&p.tailscale_ips), None);
    }
}
