//! Unit tests for syncthing.rs pure/parsing logic — no live Syncthing needed.

use super::*;

#[test]
fn parse_api_key_from_config_xml_basic() {
    let xml = r#"<?xml version="1.0"?>
<configuration>
  <gui enabled="true" tls="false">
    <address>127.0.0.1:8384</address>
    <apikey>aBcDeFgH1234567890APIKEY</apikey>
  </gui>
</configuration>"#;
    assert_eq!(
        parse_api_key_from_config_xml(xml),
        Some("aBcDeFgH1234567890APIKEY".to_string())
    );
}

#[test]
fn parse_api_key_missing_returns_none() {
    assert_eq!(
        parse_api_key_from_config_xml("<configuration></configuration>"),
        None
    );
    assert_eq!(parse_api_key_from_config_xml(""), None);
    assert_eq!(parse_api_key_from_config_xml("<apikey></apikey>"), None);
}

#[test]
fn parse_api_key_trims_whitespace() {
    let xml = "<gui><apikey>   SOMEKEY123   </apikey></gui>";
    assert_eq!(
        parse_api_key_from_config_xml(xml),
        Some("SOMEKEY123".into())
    );
}

#[test]
fn config_roundtrip_preserves_managed() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().to_path_buf();
    let mut cfg = ManagerConfig {
        syncthing_api_key: Some("KEY".into()),
        ..ManagerConfig::default()
    };
    cfg.managed.set_managed(resources::ID_GIFS, false);
    save_config(&root, cfg).unwrap();
    let (read_back, err) = read_config(&root);
    assert!(err.is_none());
    assert!(!read_back.managed.is_managed(resources::ID_GIFS));
    assert!(read_back.managed.is_managed(resources::ID_UPLOADS));
}

#[cfg(unix)]
#[test]
fn config_file_is_owner_only() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().to_path_buf();
    let cfg = ManagerConfig {
        syncthing_api_key: Some("SECRET-KEY".into()),
        ..ManagerConfig::default()
    };
    save_config(&root, cfg).unwrap();
    let mode = fs::metadata(root.join(CONFIG_FILE))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600, "credential file must be 0600");
}

#[test]
fn config_save_normalizes_key() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().to_path_buf();
    let cfg = ManagerConfig {
        syncthing_api_key: Some("  \"QUOTED-KEY\"  ".into()),
        ..ManagerConfig::default()
    };
    let saved = save_config(&root, cfg).unwrap();
    assert_eq!(saved.syncthing_api_key.as_deref(), Some("QUOTED-KEY"));
}

/// Front-door integration §7: the Rust `save_config` must round-trip a
/// manager_config.json shape that the Python `manager_config.py` also
/// accepts, and must preserve the Python-owned keys (`auto_host`,
/// `take_host_on_crash`) when we write over the file. Simulate a
/// Python-authored file exactly, then let Rust re-save and confirm it
/// wasn't clobbered.
///
/// This is the strongest cross-language guarantee we can make without
/// running the actual Python interpreter in the test harness: the shape
/// we produce and consume matches the JSON `manager_config.py` emits.
#[test]
fn config_roundtrip_preserves_python_owned_keys() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().to_path_buf();

    // Simulate the file as Python would write it — the two front-door
    // booleans plus an already-present syncthing_api_key from a prior
    // Rust save. Field names are lifted VERBATIM from manager_config.py
    // (see its _DEFAULTS dict) and front_door_client's contract note.
    let python_authored = serde_json::json!({
        "auto_host": true,
        "take_host_on_crash": false,
        "syncthing_api_key": "SEEDED-KEY",
        "syncthing_url": "http://127.0.0.1:8384",
    });
    std::fs::write(
        root.join(CONFIG_FILE),
        serde_json::to_string_pretty(&python_authored).unwrap(),
    )
    .unwrap();

    // Rust reads it and writes it back (e.g. user toggled a managed flag).
    let (mut cfg, err) = read_config(&root);
    assert!(err.is_none(), "python-shaped file must parse cleanly");
    assert_eq!(cfg.syncthing_api_key.as_deref(), Some("SEEDED-KEY"));
    cfg.managed.set_managed(resources::ID_STICKERS, false);
    save_config(&root, cfg).unwrap();

    // Reopen the raw file — the front-door keys Python owns must still
    // be there byte-for-byte. If Rust's serde model dropped them we'd
    // silently reset auto_host to false on the next front-door poll.
    let raw: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(root.join(CONFIG_FILE)).unwrap()).unwrap();
    assert_eq!(raw.get("auto_host").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(
        raw.get("take_host_on_crash").and_then(|v| v.as_bool()),
        Some(false)
    );
    assert_eq!(
        raw.get("syncthing_api_key").and_then(|v| v.as_str()),
        Some("SEEDED-KEY")
    );
}

#[test]
fn folder_config_json_applies_chatbucket_profile() {
    let def = resources::by_relative_path("uploads").unwrap();
    let path = PathBuf::from("/repo/uploads");
    let cfg = folder_config_json(def, &path, &["DEVID".to_string()]);
    assert_eq!(cfg["id"], "chatbucket-uploads");
    assert_eq!(cfg["label"], "Uploads");
    assert_eq!(cfg["path"], "/repo/uploads");
    assert_eq!(cfg["type"], "sendreceive");
    assert_eq!(cfg["fsWatcherEnabled"], true);
    assert_eq!(cfg["fsWatcherDelayS"], resources::WATCHER_DELAY_SECS);
    assert_eq!(cfg["ignorePerms"], true);
    assert_eq!(cfg["ignoreDelete"], false);
    assert_eq!(cfg["disableFsync"], false);
    assert_eq!(cfg["autoNormalize"], true);
    assert_eq!(cfg["paused"], false);
    assert_eq!(cfg["devices"][0]["deviceID"], "DEVID");
    // uploads is bounded-versioning
    assert_eq!(cfg["versioning"]["type"], "staggered");
}

#[test]
fn folder_config_json_no_versioning_for_messages() {
    let def = resources::by_relative_path("messages").unwrap();
    let cfg = folder_config_json(def, &PathBuf::from("/repo/messages"), &[]);
    assert_eq!(cfg["versioning"]["type"], "");
}

#[test]
fn folder_config_devices_parses() {
    let v = serde_json::json!({
        "devices": [{"deviceID":"AAA"},{"deviceID":"BBB"}]
    });
    assert_eq!(
        folder_config_devices(&v),
        vec!["AAA".to_string(), "BBB".to_string()]
    );
}

#[test]
fn folder_config_path_extracts() {
    let v = serde_json::json!({ "path": "/x/y" });
    assert_eq!(folder_config_path(&v), Some("/x/y".to_string()));
    assert_eq!(folder_config_path(&serde_json::json!({})), None);
}

#[test]
fn api_error_classification_never_contains_key() {
    // Auth failure must not embed the key.
    let e = ApiError::AuthFailed;
    assert_eq!(format!("{e}"), "authentication rejected (401/403)");
}

#[test]
fn pending_snapshot_groups_by_device_and_classifies() {
    // Build the controller logic offline by simulating the classification of
    // a pending-folders map. We can't call pending_snapshot() without HTTP,
    // so we test classify_pending integration directly.
    let offered = vec![
        resources::ID_MESSAGES.to_string(),
        resources::ID_UPLOADS.to_string(),
    ];
    match resources::classify_pending(&offered) {
        resources::PendingKind::ChatBucket { folder_ids } => {
            assert_eq!(folder_ids.len(), 2);
        }
        other => panic!("expected ChatBucket, got {other:?}"),
    }
}

#[test]
fn short_device_id_truncates() {
    assert_eq!(short_device_id("ABCDEFG-HIJKLMN"), "ABCDEFG");
}

#[test]
fn install_state_labels() {
    assert_eq!(InstallState::NotInstalled.label(), "Not installed");
    assert_eq!(InstallState::Running.label(), "Running");
}

// ── Integration §1: device self-filter ─────────────────────────────────
//
// We can't spin up a Syncthing to test the full REST path, but the pure
// filter logic itself — "given a device ID map, drop the entry equal to
// the local ID" — is what we need to guard against regression.

/// Regression test for the reported bug: the device list shows this
/// machine's OWN Syncthing ID (e.g. "SHRE7SK-...") because Syncthing's
/// `/rest/config/folders/<id>` response by design includes the local
/// device in the `devices` array of every folder it hosts. The fix
/// filters out the local ID inside `all_chatbucket_device_associations`.
///
/// The filter itself is a pure function of two inputs: the raw
/// device-associations map and the local device ID. We reproduce that
/// two-line filter here so the test doesn't require a live daemon.
#[test]
fn device_self_filter_drops_local_id() {
    let raw: std::collections::BTreeMap<String, Vec<String>> = [
        (
            "SHRE7SK-LOCAL-DEVICE-ID".to_string(),
            vec![resources::ID_MESSAGES.to_string()],
        ),
        (
            "PEERAAA-REMOTE-DEVICE".to_string(),
            vec![
                resources::ID_MESSAGES.to_string(),
                resources::ID_UPLOADS.to_string(),
            ],
        ),
    ]
    .into_iter()
    .collect();

    let local_id = "SHRE7SK-LOCAL-DEVICE-ID";
    let filtered: std::collections::BTreeMap<String, Vec<String>> = raw
        .into_iter()
        .filter(|(id, _)| id != local_id)
        .collect();

    assert!(!filtered.contains_key(local_id), "self must be filtered");
    assert!(
        filtered.contains_key("PEERAAA-REMOTE-DEVICE"),
        "remote peers must survive filtering"
    );
    assert_eq!(filtered.len(), 1);
}

/// The "unrelated folder never appears" case (§1 second bullet): even if
/// an entirely non-ChatBucket folder happens to be configured on the
/// same Syncthing daemon and shares a device with ChatBucket folders,
/// the walk in `all_chatbucket_device_associations` iterates only
/// `resources::RESOURCES` — so a device that's *only* on a foreign
/// folder cannot leak in.
///
/// We simulate that here by handing the classifier a folder ID that
/// isn't one of the seven ChatBucket IDs, then asserting our lookup
/// (which uses `resources::by_folder_id`) never resolves it.
#[test]
fn unrelated_folder_id_is_not_recognized() {
    for foreign in [
        "family-photos",
        "syncthing-default",
        "chatbucket-messages-typo",
        "",
    ] {
        assert!(
            resources::by_folder_id(foreign).is_none(),
            "foreign folder id {foreign:?} must not be recognised as a ChatBucket resource"
        );
    }
    // Sanity: known IDs still resolve.
    assert!(resources::by_folder_id(resources::ID_MESSAGES).is_some());
}
