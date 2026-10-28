//! The screenshot a `screen` event names (#201): fetched from the panel with the token, which the page never
//! sees, and handed to the page as a data: URL. Only a PNG of a sane size is accepted.

use std::io::{BufRead, Read, Write};

use crate::panel::Panel;
use crate::sse;

/// Larger than any screenshot Rafiq sends (the long side is at most 1568 pixels).
pub const MAX_IMAGE: usize = 20 * 1024 * 1024;
const PNG: &[u8] = b"\x89PNG\r\n\x1a\n";

/// An id as Rafiq makes them (12 hex characters); anything else is never put in a request.
pub fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 32 && id.bytes().all(|b| b.is_ascii_hexdigit())
}

/// The whole fetch, however slowly a peer sends: a screenshot from this computer takes well under a second.
const DEADLINE: std::time::Duration = std::time::Duration::from_secs(30);

pub fn request(panel: &Panel, id: &str, screen_token: &str) -> String {
    format!(
        "GET /api/screen?id={id} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nX-Rafiq-Token: {}\r\n\
         X-Rafiq-Screen-Token: {screen_token}\r\nConnection: close\r\n\r\n",
        panel.port, panel.token
    )
}

/// One line of the reply, at most 8 KB, before the deadline.
fn line_of(reader: &mut impl BufRead, line: &mut String, started: std::time::Instant) -> Result<usize, String> {
    if started.elapsed() > DEADLINE {
        return Err("stopped".to_string());
    }
    line.clear();
    reader.by_ref().take(8192).read_line(line).map_err(|_| "stopped".to_string())
}

/// The PNG, or a problem code. `check` runs before anything is sent (whose program listens there).
pub fn fetch(
    panel: &Panel,
    screen_token: Option<&str>,
    id: &str,
    check: impl FnOnce() -> Result<(), String>,
) -> Result<Vec<u8>, String> {
    let screen_token = screen_token.ok_or("no_token".to_string())?;
    if !valid_id(id) {
        return Err("refused".to_string());
    }
    let started = std::time::Instant::now();
    let mut conn = sse::connect(panel)?;
    check()?;
    conn.set_read_timeout(Some(std::time::Duration::from_secs(10))).ok();
    conn.write_all(request(panel, id, screen_token).as_bytes()).map_err(|_| "stopped".to_string())?;
    let mut reader = std::io::BufReader::new(conn);
    let mut line = String::new();
    line_of(&mut reader, &mut line, started)?;
    if line.split_whitespace().nth(1) != Some("200") || !line.ends_with('\n') {
        return Err("refused".to_string());
    }
    let mut length = None;
    for _ in 0..50 {
        if line_of(&mut reader, &mut line, started)? == 0 {
            return Err("stopped".to_string());
        }
        let header = line.trim();
        if header.is_empty() {
            break;
        }
        if let Some((name, value)) = header.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                length = value.trim().parse::<usize>().ok();
            }
        }
    }
    let length = length.filter(|n| *n <= MAX_IMAGE).ok_or("too_long".to_string())?;
    let mut body = Vec::with_capacity(length);
    while body.len() < length {           // in pieces, so the deadline holds against a trickle
        if started.elapsed() > DEADLINE {
            return Err("stopped".to_string());
        }
        let mut piece = vec![0; (length - body.len()).min(64 * 1024)];
        let got = reader.read(&mut piece).map_err(|_| "stopped".to_string())?;
        if got == 0 {
            return Err("stopped".to_string());
        }
        body.extend_from_slice(&piece[..got]);
    }
    if !body.starts_with(PNG) {
        return Err("refused".to_string());
    }
    Ok(body)
}

/// The picture as a data: URL the page can show.
pub fn data_url(png: &[u8]) -> String {
    format!("data:image/png;base64,{}", base64(png))
}

fn base64(data: &[u8]) -> String {
    const ABC: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(ABC[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn panel(port: u16) -> Panel {
        Panel { port, token: "k".repeat(43), ipv6: false }
    }

    const SCREEN: Option<&str> = Some("sssssssssssssssssssssssssssssssssssssssssss");

    fn fake(reply: Vec<u8>) -> (u16, std::thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let got = std::thread::spawn(move || {
            let (mut conn, _) = listener.accept().unwrap();
            conn.set_read_timeout(Some(std::time::Duration::from_millis(300))).unwrap();
            let mut first = vec![0; 4096];
            let n = conn.read(&mut first).unwrap_or(0);
            if n > 0 {
                let _ = conn.write_all(&reply);
            }
            String::from_utf8_lossy(&first[..n]).to_string()
        });
        (port, got)
    }

    fn reply(status: &str, body: &[u8]) -> Vec<u8> {
        let mut out = format!("HTTP/1.0 {status}\r\nContent-Type: image/png\r\nContent-Length: {}\r\n\r\n", body.len())
            .into_bytes();
        out.extend_from_slice(body);
        out
    }

    #[test]
    fn base64_matches_the_standard() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"foobar"), "Zm9vYmFy");
        assert_eq!(base64(&[0xff, 0xfe, 0x00]), "//4A");
    }

    #[test]
    fn only_an_id_rafiq_could_have_made_is_asked_for() {
        assert!(valid_id("a1b2c3d4e5f6"));
        for id in ["", "../events", "abc def", "g123", &"a".repeat(33)] {
            assert!(!valid_id(id), "{id:?}");
        }
    }

    #[test]
    fn fetches_the_png_with_the_token_and_host() {
        let picture = [PNG, b"rest of the picture"].concat();
        let (port, got) = fake(reply("200 OK", &picture));
        assert_eq!(fetch(&panel(port), SCREEN, "a1b2c3", || Ok(())), Ok(picture));
        let sent = got.join().unwrap();
        assert!(sent.starts_with("GET /api/screen?id=a1b2c3 HTTP/1.1\r\n"));
        assert!(sent.contains(&format!("\r\nX-Rafiq-Token: {}\r\n", "k".repeat(43))));
        assert!(sent.contains(&format!("\r\nHost: 127.0.0.1:{port}\r\n")));
        assert!(sent.contains(&format!("\r\nX-Rafiq-Screen-Token: {}\r\n", SCREEN.unwrap())));
    }

    #[test]
    fn without_the_screen_token_nothing_is_asked_for() {
        assert_eq!(fetch(&panel(1), None, "a1", || Ok(())), Err("no_token".to_string()));
    }

    #[test]
    fn a_status_line_with_no_end_is_refused() {
        let (port, _) = fake(vec![b'H'; 100_000]);
        assert_eq!(fetch(&panel(port), SCREEN, "a1", || Ok(())), Err("refused".to_string()));
    }

    #[test]
    fn refuses_what_is_not_a_png_a_refusal_or_too_large() {
        let (port, _) = fake(reply("200 OK", b"<html>not a picture</html>"));
        assert_eq!(fetch(&panel(port), SCREEN, "a1", || Ok(())), Err("refused".to_string()));
        let (port, _) = fake(reply("404 Not Found", b""));
        assert_eq!(fetch(&panel(port), SCREEN, "a1", || Ok(())), Err("refused".to_string()));
        let (port, _) = fake(format!("HTTP/1.0 200 OK\r\nContent-Length: {}\r\n\r\n", MAX_IMAGE + 1).into_bytes());
        assert_eq!(fetch(&panel(port), SCREEN, "a1", || Ok(())), Err("too_long".to_string()));
    }

    #[test]
    fn nothing_is_sent_for_a_bad_id_or_to_a_listener_that_is_not_ours() {
        assert_eq!(fetch(&panel(1), SCREEN, "../x", || Ok(())), Err("refused".to_string()));
        let (port, got) = fake(reply("200 OK", PNG));
        assert_eq!(fetch(&panel(port), SCREEN, "a1", || Err("not_ours".to_string())), Err("not_ours".to_string()));
        assert!(got.join().unwrap().is_empty());
    }

    #[test]
    fn the_data_url_is_a_png_one() {
        assert_eq!(data_url(b"foo"), "data:image/png;base64,Zm9v");
    }
}
