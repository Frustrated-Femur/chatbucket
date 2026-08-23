//! Unit tests for the pure logic in resources.rs — no HTTP, no filesystem
//! beyond a tempdir for the state-conflict scan.

use super::*;
use std::fs;

fn repo() -> PathBuf {
    PathBuf::from("/repo")
}

// ── resource mapping (task §30) ─────────────────────────────────────────

#[test]
fn resource_mapping_is_exact_and_complete() {
    assert_eq!(RESOURCES.len(), 7);
    let expect = [
        ("messages", ID_MESSAGES),
        ("presence", ID_PRESENCE),
        ("state", ID_STATE),
        ("gifs", ID_GIFS),
        ("sfx", ID_SFX),
        ("stickers", ID_STICKERS),
        ("uploads", ID_UPLOADS),
    ];
    for (rel, id) in expect {
        let def = by_relative_path(rel).expect(rel);
        assert_eq!(def.folder_id, id);
        assert_eq!(def.relative_path, rel);
        // round trip the other way
        assert_eq!(by_folder_id(id).unwrap().relative_path, rel);
    }
}

#[test]
fn local_paths_derive_from_repo_root() {
    let def = by_relative_path("uploads").unwrap();
    assert_eq!(local_path(&repo(), def), PathBuf::from("/repo/uploads"));
    let m = by_relative_path("messages").unwrap();
    assert_eq!(local_path(&repo(), m), PathBuf::from("/repo/messages"));
}

#[test]
fn fixed_ids_are_recognized() {
    for id in ALL_FOLDER_IDS {
        assert!(is_chatbucket_folder_id(id), "{id}");
    }
    assert!(!is_chatbucket_folder_id("chatbucket-uploads2"));
    assert!(!is_chatbucket_folder_id("photos"));
    assert!(!is_chatbucket_folder_id(""));
    assert!(!is_chatbucket_folder_id("CHATBUCKET-UPLOADS")); // case-sensitive wire id
}

#[test]
fn only_state_is_arbitration_state() {
    for r in RESOURCES {
        assert_eq!(r.is_arbitration_state, r.relative_path == "state");
    }
}

#[test]
fn versioning_policy_matches_matrix() {
    // §20: versioning is the ONLY axis of variation.
    assert_eq!(
        by_relative_path("messages").unwrap().versioning,
        VersioningPolicy::None
    );
    assert_eq!(
        by_relative_path("presence").unwrap().versioning,
        VersioningPolicy::None
    );
    assert_eq!(
        by_relative_path("state").unwrap().versioning,
        VersioningPolicy::None
    );
    assert_eq!(
        by_relative_path("gifs").unwrap().versioning,
        VersioningPolicy::Bounded
    );
    assert_eq!(
        by_relative_path("sfx").unwrap().versioning,
        VersioningPolicy::Bounded
    );
    assert_eq!(
        by_relative_path("stickers").unwrap().versioning,
        VersioningPolicy::Bounded
    );
    assert_eq!(
        by_relative_path("uploads").unwrap().versioning,
        VersioningPolicy::Bounded
    );
}

// ── managed logic (task §30) ────────────────────────────────────────────

#[test]
fn managed_defaults_to_true_for_all() {
    let m = ManagedState::default();
    for r in RESOURCES {
        assert!(m.is_managed(r.folder_id), "{} default", r.folder_id);
    }
    assert_eq!(m.managed_resources().len(), 7);
}

#[test]
fn managed_flag_roundtrips() {
    let mut m = ManagedState::default();
    m.set_managed(ID_UPLOADS, false);
    assert!(!m.is_managed(ID_UPLOADS));
    assert_eq!(m.managed_resources().len(), 6);
    // false → true re-enables
    m.set_managed(ID_UPLOADS, true);
    assert!(m.is_managed(ID_UPLOADS));
    assert_eq!(m.managed_resources().len(), 7);
}

#[test]
fn managed_serde_missing_key_defaults_true() {
    // A config written before this resource existed must still read managed.
    let json = r#"{"map": {"chatbucket-messages": false}}"#;
    let m: ManagedState = serde_json::from_str(json).unwrap();
    assert!(!m.is_managed(ID_MESSAGES));
    assert!(m.is_managed(ID_UPLOADS)); // absent → default true
}

// ── collision detection (task §30) ──────────────────────────────────────

#[test]
fn collision_clear_when_absent() {
    let def = by_relative_path("uploads").unwrap();
    assert_eq!(check_collision(def, None, &repo()), CollisionCheck::Clear);
}

#[test]
fn collision_same_install_allowed() {
    let def = by_relative_path("uploads").unwrap();
    // Path exists on disk? paths_equal falls back to non-canonical compare
    // for nonexistent paths, and "/repo/uploads" == "/repo/uploads" textually.
    assert_eq!(
        check_collision(def, Some("/repo/uploads"), &repo()),
        CollisionCheck::SameInstall
    );
}

#[test]
fn collision_different_path_is_conflict() {
    let def = by_relative_path("uploads").unwrap();
    let chk = check_collision(def, Some("/other/cb/uploads"), &repo());
    match chk {
        CollisionCheck::Conflict { existing_path } => {
            assert_eq!(existing_path, "/other/cb/uploads");
        }
        other => panic!("expected conflict, got {other:?}"),
    }
}

#[test]
fn conflict_message_names_both_paths() {
    let def = by_relative_path("uploads").unwrap();
    let msg = conflict_message(def, "/other/uploads", &repo());
    assert!(msg.contains("chatbucket-uploads"));
    assert!(msg.contains("/repo/uploads"));
    assert!(msg.contains("/other/uploads"));
    assert!(msg.contains("will not repoint"));
}

// ── pending-request recognition (task §30) ──────────────────────────────

fn ids(v: &[&str]) -> Vec<String> {
    v.iter().map(|s| s.to_string()).collect()
}

#[test]
fn pending_empty_is_none() {
    assert_eq!(classify_pending(&[]), PendingKind::None);
}

#[test]
fn pending_full_known_set_is_chatbucket() {
    let all: Vec<String> = ALL_FOLDER_IDS.iter().map(|s| s.to_string()).collect();
    match classify_pending(&all) {
        PendingKind::ChatBucket { folder_ids } => assert_eq!(folder_ids.len(), 7),
        other => panic!("expected ChatBucket, got {other:?}"),
    }
}

#[test]
fn pending_subset_is_chatbucket() {
    // §17 fixed rule: subset is approvable (two machines, different managed sets).
    let sub = ids(&[ID_MESSAGES, ID_PRESENCE, ID_STATE, ID_UPLOADS]);
    match classify_pending(&sub) {
        PendingKind::ChatBucket { folder_ids } => assert_eq!(folder_ids.len(), 4),
        other => panic!("expected ChatBucket subset, got {other:?}"),
    }
}

#[test]
fn pending_single_known_id_is_chatbucket() {
    let one = ids(&[ID_UPLOADS]);
    match classify_pending(&one) {
        PendingKind::ChatBucket { folder_ids } => {
            assert_eq!(folder_ids, vec![ID_UPLOADS.to_string()])
        }
        other => panic!("expected ChatBucket single, got {other:?}"),
    }
}

#[test]
fn pending_with_unknown_id_is_not_chatbucket() {
    let mixed = ids(&[ID_MESSAGES, "some-random-folder"]);
    match classify_pending(&mixed) {
        PendingKind::Mixed { unknown_ids } => {
            assert_eq!(unknown_ids, vec!["some-random-folder".to_string()])
        }
        other => panic!("expected Mixed, got {other:?}"),
    }
}

#[test]
fn resources_for_ids_preserves_display_order() {
    let got = resources_for_ids(&ids(&[ID_UPLOADS, ID_MESSAGES, ID_STATE]));
    let rels: Vec<&str> = got.iter().map(|r| r.relative_path).collect();
    assert_eq!(rels, vec!["messages", "state", "uploads"]); // RESOURCES order
}

// ── device ID validation (task §30) ─────────────────────────────────────

#[test]
fn device_id_valid_undashed() {
    // 52 base32 chars (A-Z, 2-7)
    let raw = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567ABCDEFGHIJKLMNOPQRST"; // exactly 52
    assert_eq!(raw.len(), 52);
    assert!(validate_device_id(raw).is_ok());
}

#[test]
fn device_id_valid_dashed_normalizes() {
    // 8 dashed groups of 6 + final 4 = 52 chars total
    let undashed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567ABCDEFGHIJKLMNOPQRST";
    assert_eq!(undashed.len(), 52);
    // Insert dashes every 7 chars for the dashed presentation form.
    let mut dashed = String::new();
    for (i, c) in undashed.chars().enumerate() {
        if i > 0 && i % 7 == 0 {
            dashed.push('-');
        }
        dashed.push(c);
    }
    let got = validate_device_id(&dashed).unwrap();
    assert_eq!(got, undashed);
}

#[test]
fn device_id_rejects_wrong_length() {
    assert!(validate_device_id("ABCDEFG").is_err());
    assert!(validate_device_id("").is_err());
    let long = "A".repeat(53);
    assert!(validate_device_id(&long).is_err());
}

#[test]
fn device_id_rejects_bad_charset() {
    // '0','1','8','9' are not in base32
    let bad = format!("{}0", "A".repeat(51));
    assert!(validate_device_id(&bad).is_err());
}

#[test]
fn device_id_lowercase_accepted_and_uppercased() {
    let lower = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrst"; // exactly 52
    assert_eq!(lower.len(), 52);
    let got = validate_device_id(lower).unwrap();
    assert_eq!(got, lower.to_ascii_uppercase());
}

// ── sync-conflict detection (task §30) ──────────────────────────────────

#[test]
fn conflict_filename_matching() {
    assert!(is_sync_conflict_json(
        "host-state.sync-conflict-20260101-120000-ABCDEFG.json"
    ));
    assert!(!is_sync_conflict_json("host-state.json"));
    assert!(!is_sync_conflict_json("sync-conflict-notes.txt"));
    assert!(!is_sync_conflict_json(
        "host-state.sync-conflict-20260101.bak"
    ));
}

#[test]
fn find_state_conflicts_scans_state_dir() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().to_path_buf();
    let state = root.join("state");
    fs::create_dir_all(&state).unwrap();
    fs::write(state.join("host-state.json"), "{}").unwrap();
    fs::write(
        state.join("host-state.sync-conflict-20260101-120000-AAAAAAA.json"),
        "{}",
    )
    .unwrap();
    fs::write(state.join("unrelated.txt"), "x").unwrap();

    let found = find_state_conflicts(&root);
    assert_eq!(found.len(), 1);
    assert!(found[0]
        .file_name()
        .unwrap()
        .to_string_lossy()
        .contains(".sync-conflict-"));

    // Missing state dir → empty, not an error.
    let empty = tempfile::tempdir().unwrap();
    assert!(find_state_conflicts(empty.path()).is_empty());
}

// ── status classification (task §30) ────────────────────────────────────

#[test]
fn classify_disabled_wins_first() {
    assert_eq!(
        classify_folder(false, true, "idle", 0, 0, 0, false),
        FolderHealth::Disabled
    );
}

#[test]
fn classify_conflict_outranks_missing() {
    // managed, has conflict files, folder configured
    assert_eq!(
        classify_folder(true, true, "idle", 0, 0, 0, true),
        FolderHealth::Conflict
    );
}

#[test]
fn classify_missing_when_managed_but_absent() {
    assert_eq!(
        classify_folder(true, false, "", 0, 0, 0, false),
        FolderHealth::Missing
    );
}

#[test]
fn classify_in_sync() {
    assert_eq!(
        classify_folder(true, true, "idle", 0, 0, 0, false),
        FolderHealth::InSync
    );
}

#[test]
fn classify_syncing_with_need() {
    assert_eq!(
        classify_folder(true, true, "idle", 12, 183_000_000, 0, false),
        FolderHealth::Syncing {
            need_files: 12,
            need_bytes: 183_000_000
        }
    );
    assert_eq!(
        classify_folder(true, true, "scanning", 0, 0, 0, false),
        FolderHealth::Syncing {
            need_files: 0,
            need_bytes: 0
        }
    );
}

#[test]
fn classify_error_states() {
    assert_eq!(
        classify_folder(true, true, "error", 0, 0, 0, false),
        FolderHealth::SyncError
    );
    assert_eq!(
        classify_folder(true, true, "idle", 0, 0, 2, false),
        FolderHealth::SyncError
    );
}

#[test]
fn classify_unknown_when_no_state() {
    assert_eq!(
        classify_folder(true, true, "", 0, 0, 0, false),
        FolderHealth::Unknown
    );
}
