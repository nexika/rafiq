//! What the widget may ask of the running Rafiq: a typed request, the answer to a question, and stop. The
//! page chooses one of these, never a path, and the token is added here.

use std::io::{BufRead, Write};

use crate::panel::Panel;
use crate::sse;

/// As much as the panel takes for a typed request (rafiq/panel.py cuts at 2000).
pub const MAX_TEXT: usize = 2000;

#[derive(Debug, PartialEq)]
pub enum Action {
    Ask(String),
    Answer { yes: bool, always: bool },
    Stop,
}

impl Action {
    /// A typed request, trimmed; a problem code when there is nothing to send or too much.
    pub fn ask(text: &str) -> Result<Action, String> {
        let text = text.trim();
        if text.is_empty() {
            return Err("empty".to_string());
        }
        if text.chars().count() > MAX_TEXT {
            return Err("too_long".to_string());
        }
        Ok(Action::Ask(text.to_string()))
    }

    fn path(&self) -> &'static str {
        match self {
            Action::Ask(_) => "/api/text",
            Action::Answer { .. } => "/api/confirm",
            Action::Stop => "/api/stop",
        }
    }

    fn body(&self) -> serde_json::Value {
        match self {
            Action::Ask(text) => serde_json::json!({"text": text}),
            // "Always" is only a yes that is remembered; a no is never "always".
            Action::Answer { yes, always } => serde_json::json!({"yes": yes, "always": *yes && *always}),
            Action::Stop => serde_json::json!({}),
        }
    }
}

/// The request, with the panel's token and the Host it checks.
pub fn request(panel: &Panel, action: &Action) -> String {
    let body = action.body().to_string();
    format!(
        "POST {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nX-Rafiq-Token: {}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{}",
        action.path(),
        panel.port,
        panel.token,
        body.len(),
        body
    )
}

/// Send it. `check` runs before anything is sent (whose program listens there). Errors are problem codes.
pub fn send(panel: &Panel, action: &Action, check: impl FnOnce() -> Result<(), String>) -> Result<(), String> {
    let mut conn = sse::connect(panel)?;
    check()?;
    conn.set_read_timeout(Some(std::time::Duration::from_secs(5))).ok();
    conn.write_all(request(panel, action).as_bytes()).map_err(|_| "stopped".to_string())?;
    let mut status = String::new();
    std::io::BufReader::new(conn).read_line(&mut status).map_err(|_| "stopped".to_string())?;
    match status.split_whitespace().nth(1) {
        Some("200") => Ok(()),
        _ => Err("refused".to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;
    use std::net::TcpListener;

    fn panel(port: u16) -> Panel {
        Panel { port, token: "k".repeat(43), ipv6: false }
    }

    #[test]
    fn a_typed_request_goes_to_the_panel_with_its_token_and_host() {
        let request = request(&panel(8765), &Action::ask("  weather in Cairo ").unwrap());
        assert!(request.starts_with("POST /api/text HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n"));
        assert!(request.contains(&format!("\r\nX-Rafiq-Token: {}\r\n", "k".repeat(43))));
        assert!(request.ends_with("\r\n\r\n{\"text\":\"weather in Cairo\"}"));
        assert!(request.contains("\r\nContent-Length: 27\r\n"));
    }

    #[test]
    fn the_length_counts_bytes_not_letters() {
        let request = request(&panel(8765), &Action::ask("الجو").unwrap());
        let body = request.split("\r\n\r\n").nth(1).unwrap();
        assert!(request.contains(&format!("\r\nContent-Length: {}\r\n", body.len())));
    }

    #[test]
    fn an_empty_or_too_long_request_is_not_sent() {
        assert_eq!(Action::ask("   "), Err("empty".to_string()));
        assert_eq!(Action::ask(&"a".repeat(MAX_TEXT + 1)), Err("too_long".to_string()));
        assert!(Action::ask(&"ب".repeat(MAX_TEXT)).is_ok());
    }

    #[test]
    fn answers_and_stop_go_where_the_panel_takes_them() {
        let yes = request(&panel(1), &Action::Answer { yes: true, always: true });
        assert!(yes.starts_with("POST /api/confirm ") && yes.ends_with("{\"always\":true,\"yes\":true}"));
        let no = request(&panel(1), &Action::Answer { yes: false, always: true });
        assert!(no.ends_with("{\"always\":false,\"yes\":false}"));
        assert!(request(&panel(1), &Action::Stop).starts_with("POST /api/stop "));
    }

    fn fake(status: &'static [u8]) -> (u16, std::thread::JoinHandle<Vec<u8>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let got = std::thread::spawn(move || {
            let (mut conn, _) = listener.accept().unwrap();
            conn.set_read_timeout(Some(std::time::Duration::from_millis(300))).unwrap();
            let mut sent = vec![0; 4096];
            let n = conn.read(&mut sent).unwrap_or(0);
            sent.truncate(n);
            if n > 0 {
                let _ = conn.write_all(status);
            }
            sent
        });
        (port, got)
    }

    #[test]
    fn a_request_the_panel_accepts_is_sent() {
        let (port, got) = fake(b"HTTP/1.0 200 OK\r\n\r\n{}");
        assert_eq!(send(&panel(port), &Action::Stop, || Ok(())), Ok(()));
        assert!(got.join().unwrap().starts_with(b"POST /api/stop "));
    }

    #[test]
    fn a_request_the_panel_refuses_says_so() {
        let (port, _) = fake(b"HTTP/1.0 403 Forbidden\r\n\r\n");
        assert_eq!(send(&panel(port), &Action::Stop, || Ok(())), Err("refused".to_string()));
    }

    #[test]
    fn nothing_is_sent_to_a_listener_that_is_not_ours() {
        let (port, got) = fake(b"HTTP/1.0 200 OK\r\n\r\n");
        let sent = send(&panel(port), &Action::Stop, || Err("not_ours".to_string()));
        assert_eq!(sent, Err("not_ours".to_string()));
        assert!(got.join().unwrap().is_empty());
    }
}
