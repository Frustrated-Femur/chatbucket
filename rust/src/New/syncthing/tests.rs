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
