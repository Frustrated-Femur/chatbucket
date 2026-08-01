//! tray.rs — Best-effort system tray icon (feature = "tray").
//!
//! When the `tray` feature is disabled (e.g. headless build on a box without
//! GTK/gdk-pixbuf), `spawn_tray()` is a no-op returning None. The window
//! still opens normally in that case. Same "never a hard requirement"
//! contract as Python's `_build_tray_icon()`.

use crate::manager::ManagerContext;
use std::sync::Arc;

pub struct TrayHandle {
    #[cfg(feature = "tray")]
    stop: Arc<std::sync::atomic::AtomicBool>,
    #[cfg(feature = "tray")]
    _thread: Option<std::thread::JoinHandle<()>>,
    #[cfg(not(feature = "tray"))]
    _priv: (),
}

#[cfg(feature = "tray")]
impl Drop for TrayHandle {
    fn drop(&mut self) {
        self.stop
            .store(true, std::sync::atomic::Ordering::SeqCst);
    }
}

#[cfg(not(feature = "tray"))]
pub fn spawn_tray(_ctx: Arc<ManagerContext>) -> Option<TrayHandle> {
    log::info!("[tray] built without the `tray` feature — running window-only");
    None
}

#[cfg(feature = "tray")]
pub fn spawn_tray(ctx: Arc<ManagerContext>) -> Option<TrayHandle> {
    use std::time::Duration;
    const TRAY_POLL: Duration = Duration::from_secs(5);

    let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let stop_clone = stop.clone();
    let ctx_clone = ctx.clone();

    let thread = std::thread::Builder::new()
        .name("cb-tray".into())
        .spawn(move || {
            if let Err(e) = tray_loop(ctx_clone, stop_clone, TRAY_POLL) {
                log::info!("[tray] disabled: {e}");
            }
        })
        .ok()?;

    Some(TrayHandle {
        stop,
        _thread: Some(thread),
    })
}

#[cfg(feature = "tray")]
fn tray_loop(
    ctx: Arc<ManagerContext>,
    stop: Arc<std::sync::atomic::AtomicBool>,
    poll: std::time::Duration,
) -> Result<(), String> {
    use crate::manager::RoleState;
    use tray_icon::{
        menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem},
        TrayIconBuilder,
    };

    let menu = Menu::new();
    let mi_status = MenuItem::new("Role: —", false, None);
    let mi_start = MenuItem::new("Start ChatBucket", true, None);
    let mi_stop = MenuItem::new("Stop ChatBucket", true, None);
    let mi_quit = MenuItem::new("Quit Manager", true, None);
    menu.append(&mi_status).map_err(|e| e.to_string())?;
    menu.append(&PredefinedMenuItem::separator())
        .map_err(|e| e.to_string())?;
    menu.append(&mi_start).map_err(|e| e.to_string())?;
    menu.append(&mi_stop).map_err(|e| e.to_string())?;
    menu.append(&PredefinedMenuItem::separator())
        .map_err(|e| e.to_string())?;
    menu.append(&mi_quit).map_err(|e| e.to_string())?;

    let start_id = mi_start.id().clone();
    let stop_id = mi_stop.id().clone();
    let quit_id = mi_quit.id().clone();

    let initial = ctx.get_role_only();
    let icon = build_icon(initial.state.color_hex()).map_err(|e| format!("icon: {e}"))?;

    let tray = TrayIconBuilder::new()
        .with_menu(Box::new(menu))
        .with_tooltip(format!("ChatBucket Manager — {}", initial.state.label()))
        .with_icon(icon)
        .build()
        .map_err(|e| e.to_string())?;

    let menu_channel = MenuEvent::receiver();

    loop {
        if stop.load(std::sync::atomic::Ordering::SeqCst) {
            break;
        }
        while let Ok(ev) = menu_channel.try_recv() {
            let id = ev.id;
            if id == start_id {
                let c = ctx.clone();
                std::thread::spawn(move || {
                    let _ = c.start();
                });
            } else if id == stop_id {
                let c = ctx.clone();
                std::thread::spawn(move || {
                    let _ = c.stop();
                });
            } else if id == quit_id {
                stop.store(true, std::sync::atomic::Ordering::SeqCst);
                std::process::exit(0);
            }
        }

        let rs = ctx.get_role_only();
        if let Ok(icn) = build_icon(rs.state.color_hex()) {
            let _ = tray.set_icon(Some(icn));
        }
        let _ = tray.set_tooltip(Some(format!(
            "ChatBucket Manager — {}",
            rs.state.label()
        )));
        mi_status.set_text(format!("Role: {}", rs.state.label()));
        let running = !matches!(rs.state, RoleState::Idle | RoleState::Stale);
        mi_start.set_enabled(!running);
        mi_stop.set_enabled(running);

        std::thread::sleep(poll);
    }
    drop(tray);
    Ok(())
}

#[cfg(feature = "tray")]
fn build_icon(hex_color: &str) -> Result<tray_icon::Icon, String> {
    let size: u32 = 32;
    let (r, g, b) = parse_hex(hex_color).ok_or("bad hex")?;
    let mut rgba = vec![0u8; (size * size * 4) as usize];
    let cx = size as f32 / 2.0;
    let cy = size as f32 / 2.0;
    let rr = (size as f32 / 2.0) - 2.0;
    for y in 0..size {
        for x in 0..size {
            let dx = x as f32 + 0.5 - cx;
            let dy = y as f32 + 0.5 - cy;
            let d2 = dx * dx + dy * dy;
            let idx = ((y * size + x) * 4) as usize;
            if d2 <= rr * rr {
                rgba[idx] = r;
                rgba[idx + 1] = g;
                rgba[idx + 2] = b;
                rgba[idx + 3] = 255;
            } else {
                rgba[idx + 3] = 0;
            }
        }
    }
    tray_icon::Icon::from_rgba(rgba, size, size).map_err(|e| e.to_string())
}

#[cfg(feature = "tray")]
fn parse_hex(s: &str) -> Option<(u8, u8, u8)> {
    let s = s.trim_start_matches('#');
    if s.len() != 6 {
        return None;
    }
    let r = u8::from_str_radix(&s[0..2], 16).ok()?;
    let g = u8::from_str_radix(&s[2..4], 16).ok()?;
    let b = u8::from_str_radix(&s[4..6], 16).ok()?;
    Some((r, g, b))
}
