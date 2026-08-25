//! front_door_client.rs — Loopback client for the Python front door's
//! status/control endpoint.
//!
//! This is the ONE place the Rust Manager talks HTTP to the front door on
//! 127.0.0.1:5050. Every caller (manager.rs, tray, app.rs) goes through
//! `FrontDoorClient` — nobody constructs URLs or parses JSON here anywhere
//! else. Same "one place that knows the wire format" boundary the Python
//! side draws for tailscale in arbitration.py.
//!
//! ── Verified contract (against front_door.py at the time of writing) ─────
//!
//! The .md's proposal spoke of `pointer`/`transient`/`child_pid`; the actual
//! shipped Python contract is different. This module implements the REAL
//! contract, captured here so the two languages cannot silently disagree:
//!
//!   GET  http://127.0.0.1:5050/status  ->  200 application/json
//!     {
//!       "routing":            "local"                 // hosting locally
//!                          | "unavailable"            // no host known
//!                          | "redirect:<machine>",   // client of <machine>
//!       "machine":            "<this-machine-name>",
//!       "child_running":      bool,
//!       "child_pid":          int | null,
//!       "starting":           bool,           // pointer transitioning
//!       "last_error":         string | null,  // last transient error, if any
//!       "auto_host":          bool,
//!       "take_host_on_crash": bool
//!     }
//!
//!   POST http://127.0.0.1:5050/control  ->  202 application/json
//!     Request body:
//!       {"action": "start"}                          // force claim attempt
//!       {"action": "stop"}                           // release hosting
//!       {"action": "rearbitrate"}                    // re-run without force
//!       {"action": "set_config",
//!        "config": {"auto_host": bool,
//!                   "take_host_on_crash": bool}}     // either or both
//!
//! Notes on parsing:
//!   * `routing` is a STRING (not an enum object). "redirect:" is a prefix
//!     followed by the target machine name; the machine can theoretically
//!     be empty, in which case we treat it as Unavailable (front_door.py
//!     already defensively degrades to that in `_handle_connection`).
//!   * `child_running` is not redundant with `Routing::Local`: it stays
//!     false during the brief window between claim-decided and child-bound.
//!     `starting` covers the same window; both are exposed as-is.
//!   * `last_error` is a diagnostic string; it is never machine-readable.
//!
//! ── manager_config.json (the file itself) ────────────────────────────────
//!
//! The Python side (manager_config.py) reads/writes this file with
//! `auto_host: bool` / `take_host_on_crash: bool` as the ONLY recognized
//! keys — but it preserves any other keys already present (including this
//! Manager's own `syncthing_api_key`, `syncthing_url`, `api_key_source`,
//! and `managed`). We mirror the same round-trip discipline on the Rust
//! side (`syncthing::save_config` already merges unknown keys via serde's
//! `flatten`+extra), so pushing config either via this endpoint or by
//! writing the file directly is safe from both directions.

use serde::{Deserialize, Serialize};
use std::time::Duration;

pub const STATUS_URL: &str = "http://127.0.0.1:5050/status";
pub const CONTROL_URL: &str = "http://127.0.0.1:5050/control";

/// Short by design: the front door lives on loopback and answers instantly
/// under normal conditions. A hung endpoint should never freeze the GUI.
const HTTP_TIMEOUT: Duration = Duration::from_millis(1500);

// ── Routing enum (parsed from the "routing" string) ─────────────────────

/// The routing decision the front door is currently applying to accepted
/// connections. Matches the doc's Local / Redirect(m) / Unavailable trio.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Routing {
    Local,
    Redirect(String),
    Unavailable,
}

impl Routing {
    pub fn parse(s: &str) -> Self {
        // The Python side emits exactly three shapes:
        //   "local", "unavailable", "redirect:<machine>"
        // "redirect:" with an empty machine is defensively downgraded to
        // Unavailable — same posture front_door.py takes in the accept
        // path when a Redirect pointer is missing its target.
        if s == "local" {
            Routing::Local
        } else if let Some(rest) = s.strip_prefix("redirect:") {
            let m = rest.trim().to_string();
            if m.is_empty() {
                Routing::Unavailable
            } else {
                Routing::Redirect(m)
            }
        } else {
            // "unavailable" and any unknown value: safe degradation to
            // Unavailable. An unknown routing string is either a Python
            // bug or a version skew, and neither is a reason to guess
            // Local (worst case: pretend we're hosting when we're not).
            Routing::Unavailable
        }
    }
}

// ── Wire types ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
struct RawStatus {
    #[serde(default)]
    routing: String,
    #[serde(default)]
    machine: String,
    #[serde(default)]
    child_running: bool,
    #[serde(default)]
    child_pid: Option<u32>,
    #[serde(default)]
    starting: bool,
    #[serde(default)]
    last_error: Option<String>,
    #[serde(default)]
    auto_host: bool,
    #[serde(default)]
    take_host_on_crash: bool,
}

/// The structured, parsed front-door status. This is what every caller
/// consumes — nobody outside this module should ever see the raw string.
#[derive(Debug, Clone)]
pub struct FrontDoorStatus {
    pub routing: Routing,
    pub machine: String,
    pub child_running: bool,
    pub child_pid: Option<u32>,
    pub starting: bool,
    pub last_error: Option<String>,
    pub auto_host: bool,
    pub take_host_on_crash: bool,
}

impl FrontDoorStatus {
    /// True when the front-door process itself is up (i.e. we got any
    /// valid response). Callers use this to decide whether to fall back to
    /// process_scan for role derivation.
    pub fn is_serving_role_source(&self) -> bool {
        // If we managed to construct a FrontDoorStatus at all, /status
        // answered — that IS the "up" signal.
        true
    }
}

#[derive(Debug, thiserror::Error, Clone)]
pub enum FrontDoorError {
    /// Front door isn't accepting on 127.0.0.1:5050 (connection refused,
    /// timeout, or DNS-y transport error). This is a NORMAL condition
    /// pre-launch; callers should degrade to process_scan and NOT surface
    /// it as an error banner.
    #[error("front door is not responding: {0}")]
    Unreachable(String),
    /// Front door answered but the response wasn't the JSON we expected.
    /// Diagnostic, not "the front door is down" — surface loudly.
    #[error("front door returned malformed status: {0}")]
    BadResponse(String),
    /// Non-2xx HTTP response with a decodable JSON error body.
    #[error("front door rejected the request ({status}): {message}")]
    HttpError { status: u16, message: String },
}

// ── Control-endpoint request bodies (Serialize into JSON) ───────────────

#[derive(Debug, Serialize)]
struct ControlActionBody<'a> {
    action: &'a str,
}

#[derive(Debug, Serialize)]
struct ControlSetConfigBody {
    action: &'static str,
    config: ConfigPatch,
}

/// Config patch payload — matches the Python endpoint's `config` object.
/// Both fields are optional so a caller can update just one without
/// re-asserting the other's value.
#[derive(Debug, Clone, Default, Serialize)]
pub struct ConfigPatch {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auto_host: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub take_host_on_crash: Option<bool>,
}

// ── Client ───────────────────────────────────────────────────────────────

/// Loopback client. Cheap to construct; holds no state. Every method is a
/// single HTTP round-trip with a short timeout.
#[derive(Debug, Clone, Default)]
pub struct FrontDoorClient;

impl FrontDoorClient {
    pub fn new() -> Self {
        Self
    }

    /// GET /status. Returns Ok on 2xx-with-valid-JSON, Unreachable on any
    /// transport error, BadResponse on 2xx-with-broken-JSON, HttpError on
    /// non-2xx (should not happen for /status but plumbed for symmetry).
    pub fn status(&self) -> Result<FrontDoorStatus, FrontDoorError> {
        let resp = ureq::get(STATUS_URL)
            .timeout(HTTP_TIMEOUT)
            .call()
            .map_err(map_transport)?;
        let raw: RawStatus = resp
            .into_json()
            .map_err(|e| FrontDoorError::BadResponse(e.to_string()))?;
        Ok(FrontDoorStatus {
            routing: Routing::parse(&raw.routing),
            machine: raw.machine,
            child_running: raw.child_running,
            child_pid: raw.child_pid,
            starting: raw.starting,
            last_error: raw.last_error,
            auto_host: raw.auto_host,
            take_host_on_crash: raw.take_host_on_crash,
        })
    }

    /// POST /control {"action": "start"}
    pub fn start(&self) -> Result<(), FrontDoorError> {
        self.post_action("start")
    }

    /// POST /control {"action": "stop"}
    pub fn stop(&self) -> Result<(), FrontDoorError> {
        self.post_action("stop")
    }

    /// POST /control {"action": "rearbitrate"}
    #[allow(dead_code)] // exposed for future "re-arbitrate now" button
    pub fn rearbitrate(&self) -> Result<(), FrontDoorError> {
        self.post_action("rearbitrate")
    }

    /// POST /control {"action": "set_config", "config": {...}}
    ///
    /// Idempotent on the Python side: if the requested values match
    /// what's already on disk, the server accepts the write but does NOT
    /// fire CONFIG_CHANGED (the truth-table transitions fire on genuine
    /// flips only). No special handling required here.
    pub fn set_config(&self, patch: ConfigPatch) -> Result<(), FrontDoorError> {
        let body = ControlSetConfigBody {
            action: "set_config",
            config: patch,
        };
        let resp = ureq::post(CONTROL_URL)
            .set("Content-Type", "application/json")
            .timeout(HTTP_TIMEOUT)
            .send_json(serde_json::to_value(&body).unwrap());
        finalize_control(resp)
    }

    fn post_action(&self, action: &str) -> Result<(), FrontDoorError> {
        let body = ControlActionBody { action };
        let resp = ureq::post(CONTROL_URL)
            .set("Content-Type", "application/json")
            .timeout(HTTP_TIMEOUT)
            .send_json(serde_json::to_value(&body).unwrap());
        finalize_control(resp)
    }
}

fn finalize_control(
    resp: Result<ureq::Response, ureq::Error>,
) -> Result<(), FrontDoorError> {
    match resp {
        Ok(_) => Ok(()),
        Err(ureq::Error::Status(code, r)) => {
            // Try to pull {"error": "..."} out of the body for a good message.
            let text = r.into_string().unwrap_or_default();
            let msg = serde_json::from_str::<serde_json::Value>(&text)
                .ok()
                .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(String::from))
                .unwrap_or_else(|| {
                    if text.is_empty() {
                        format!("HTTP {code}")
                    } else {
                        text
                    }
                });
            Err(FrontDoorError::HttpError {
                status: code,
                message: msg,
            })
        }
        Err(ureq::Error::Transport(t)) => Err(FrontDoorError::Unreachable(t.to_string())),
    }
}

fn map_transport(e: ureq::Error) -> FrontDoorError {
    match e {
        ureq::Error::Status(code, r) => FrontDoorError::HttpError {
            status: code,
            message: r.into_string().unwrap_or_default(),
        },
        ureq::Error::Transport(t) => FrontDoorError::Unreachable(t.to_string()),
    }
}

// ── Tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routing_parses_local() {
        assert_eq!(Routing::parse("local"), Routing::Local);
    }

    #[test]
    fn routing_parses_unavailable() {
        assert_eq!(Routing::parse("unavailable"), Routing::Unavailable);
    }

    #[test]
    fn routing_parses_redirect_with_machine() {
        assert_eq!(
            Routing::parse("redirect:archlinux"),
            Routing::Redirect("archlinux".into())
        );
    }

    #[test]
    fn routing_empty_redirect_downgrades_to_unavailable() {
        // Matches the front door's own defensive posture in
        // _handle_connection when a Redirect pointer has no machine.
        assert_eq!(Routing::parse("redirect:"), Routing::Unavailable);
        assert_eq!(Routing::parse("redirect:   "), Routing::Unavailable);
    }

    #[test]
    fn routing_unknown_string_downgrades_to_unavailable() {
        // Version-skew safety: never guess Local from an unknown value.
        assert_eq!(Routing::parse("hosting"), Routing::Unavailable);
        assert_eq!(Routing::parse(""), Routing::Unavailable);
    }

    #[test]
    fn status_urls_are_loopback_only() {
        // Belt-and-braces: if this ever got a 0.0.0.0/tailnet URL by accident
        // we'd expose the control plane to the whole tailnet. Assert the
        // loopback bind in the URL itself.
        assert!(STATUS_URL.starts_with("http://127.0.0.1:5050/"));
        assert!(CONTROL_URL.starts_with("http://127.0.0.1:5050/"));
    }

    #[test]
    fn config_patch_omits_none_fields() {
        // Confirm serde skips None so a partial update stays partial on
        // the wire — Python's `set_config` only re-evaluates keys the
        // client actually included.
        let just_auto = ConfigPatch {
            auto_host: Some(true),
            take_host_on_crash: None,
        };
        let j = serde_json::to_string(&just_auto).unwrap();
        assert!(j.contains("auto_host"));
        assert!(!j.contains("take_host_on_crash"));

        let just_takeover = ConfigPatch {
            auto_host: None,
            take_host_on_crash: Some(false),
        };
        let j = serde_json::to_string(&just_takeover).unwrap();
        assert!(!j.contains("auto_host"));
        assert!(j.contains("take_host_on_crash"));
    }

    // ── Raw JSON round-trip against samples front_door.py actually emits ──

    #[test]
    fn parses_local_status_sample() {
        let raw = r#"{
            "routing":"local",
            "machine":"archlinux",
            "child_running":true,
            "child_pid":12345,
            "starting":false,
            "last_error":null,
            "auto_host":true,
            "take_host_on_crash":false
        }"#;
        let parsed: RawStatus = serde_json::from_str(raw).unwrap();
        let s = FrontDoorStatus {
            routing: Routing::parse(&parsed.routing),
            machine: parsed.machine,
            child_running: parsed.child_running,
            child_pid: parsed.child_pid,
            starting: parsed.starting,
            last_error: parsed.last_error,
            auto_host: parsed.auto_host,
            take_host_on_crash: parsed.take_host_on_crash,
        };
        assert_eq!(s.routing, Routing::Local);
        assert_eq!(s.machine, "archlinux");
        assert!(s.child_running);
        assert_eq!(s.child_pid, Some(12345));
        assert!(!s.starting);
        assert!(s.auto_host);
        assert!(!s.take_host_on_crash);
    }

    #[test]
    fn parses_redirect_status_sample() {
        let raw = r#"{
            "routing":"redirect:archlinux",
            "machine":"win1",
            "child_running":false,
            "child_pid":null,
            "starting":false,
            "last_error":null,
            "auto_host":false,
            "take_host_on_crash":false
        }"#;
        let parsed: RawStatus = serde_json::from_str(raw).unwrap();
        assert_eq!(
            Routing::parse(&parsed.routing),
            Routing::Redirect("archlinux".into())
        );
        assert!(!parsed.child_running);
        assert!(parsed.child_pid.is_none());
    }

    #[test]
    fn parses_unavailable_status_sample() {
        // "starting" true + child_pid null covers the bootstrap window.
        let raw = r#"{
            "routing":"unavailable",
            "machine":"archlinux",
            "child_running":false,
            "child_pid":null,
            "starting":true,
            "last_error":"local server exited; respawning",
            "auto_host":true,
            "take_host_on_crash":true
        }"#;
        let parsed: RawStatus = serde_json::from_str(raw).unwrap();
        assert_eq!(Routing::parse(&parsed.routing), Routing::Unavailable);
        assert!(parsed.starting);
        assert_eq!(
            parsed.last_error.as_deref(),
            Some("local server exited; respawning")
        );
    }

    #[test]
    fn missing_fields_default_gracefully() {
        // If the Python side ever forgets a field, we should default it
        // rather than fail parsing — status is a diagnostic surface, not
        // a wire protocol.
        let raw = r#"{"routing":"local"}"#;
        let parsed: RawStatus = serde_json::from_str(raw).unwrap();
        assert_eq!(Routing::parse(&parsed.routing), Routing::Local);
        assert_eq!(parsed.machine, "");
        assert!(!parsed.child_running);
        assert!(parsed.child_pid.is_none());
    }
}
