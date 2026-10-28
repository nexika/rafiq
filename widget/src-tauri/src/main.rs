//! Rafiq's widget: a small window that shows what Rafiq is doing, from the running Rafiq's event stream.
//! This side holds the panel token and the connection; the web view only gets the events.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod owner;
mod panel;
mod post;
mod sse;

use std::time::Duration;

use tauri::{Emitter, LogicalSize, Manager, WebviewWindow};

const SMALL: (f64, f64) = (340.0, 104.0);
const LARGE: (f64, f64) = (420.0, 600.0);
const FIRST_RETRY: Duration = Duration::from_secs(2);
const LAST_RETRY: Duration = Duration::from_secs(30);

/// The small view (what Rafiq is doing now) or the large one (the whole request, step by step).
#[tauri::command]
fn set_large(window: WebviewWindow, large: bool) -> Result<(), String> {
    let (w, h) = if large { LARGE } else { SMALL };
    window.set_size(LogicalSize::new(w, h)).map_err(|e| e.to_string())
}

/// Send one of the three things the widget may ask, away from the window's thread. Errors are problem codes.
async fn send(action: Result<post::Action, String>) -> Result<(), String> {
    let action = action?;
    tauri::async_runtime::spawn_blocking(move || {
        let found = panel::home().ok_or("no_token".to_string()).and_then(|h| panel::find(&h))?;
        post::send(&found, &action, || owner::check(found.port, found.ipv6))
    })
    .await
    .map_err(|_| "stopped".to_string())?
}

/// A typed request, answered like one typed in the panel.
#[tauri::command]
async fn ask(text: String) -> Result<(), String> {
    send(post::Action::ask(&text)).await
}

/// Yes, no, or yes and don't ask again for this tool, to the question Rafiq is asking.
#[tauri::command]
async fn answer(yes: bool, always: bool) -> Result<(), String> {
    send(Ok(post::Action::Answer { yes, always })).await
}

/// Stop speaking, and run no further step of the request being answered.
#[tauri::command]
async fn stop() -> Result<(), String> {
    send(Ok(post::Action::Stop)).await
}

/// Follow the running Rafiq for as long as the widget is open, connecting again when it restarts: soon at
/// first, then every 30 seconds while it stays away.
fn follow(app: tauri::AppHandle) {
    let mut wait = FIRST_RETRY;
    loop {
        let mut opened = false;
        let problem = match panel::home().ok_or("no_token".to_string()).and_then(|h| panel::find(&h)) {
            Ok(found) => sse::follow(
                &found,
                || owner::check(found.port, found.ipv6),
                || {
                    opened = true;
                    let _ = app.emit("rafiq-connection", serde_json::json!({"connected": true}));
                },
                |event| {
                    let _ = app.emit("rafiq-event", event);
                },
            )
            .err()
            .unwrap_or_else(|| "stopped".to_string()),
            Err(problem) => problem,
        };
        let _ = app.emit("rafiq-connection", serde_json::json!({"connected": false, "problem": problem}));
        wait = if opened { FIRST_RETRY } else { (wait * 2).min(LAST_RETRY) };
        std::thread::sleep(wait);
    }
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![set_large, ask, answer, stop])
        .setup(|app| {
            let handle = app.handle().clone();
            std::thread::spawn(move || follow(handle));
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_size(LogicalSize::new(SMALL.0, SMALL.1));
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("the widget could not start");
}
