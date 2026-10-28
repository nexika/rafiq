//! Rafiq's event stream (docs/events.md), read line by line: each `data:` line is one JSON event, and a
//! line starting with `:` keeps the connection open. Errors are problem codes and never contain the token.

use std::io::{BufRead, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

use crate::panel::Panel;

/// Longer than any line the panel sends (an explanation is at most 1500 characters).
pub const MAX_LINE: u64 = 64 * 1024;
const MAX_HEADERS: usize = 50;

/// The event on one line of the stream, or None for a keep-alive, a blank line or anything that isn't JSON.
pub fn event(line: &str) -> Option<serde_json::Value> {
    let data = line.trim_end_matches(['\r', '\n']).strip_prefix("data:")?;
    serde_json::from_str(data.trim_start()).ok().filter(|v: &serde_json::Value| v.is_object())
}

/// The request for the stream. The token goes in the query, as the panel page sends it.
pub fn request(panel: &Panel) -> String {
    format!(
        "GET /events?token={} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAccept: text/event-stream\r\n\r\n",
        panel.token, panel.port
    )
}

/// One line, at most MAX_LINE long: a peer that never ends a line can't make the widget grow without end.
fn read_line(reader: &mut impl BufRead, line: &mut String) -> Result<usize, String> {
    line.clear();
    let n = reader.by_ref().take(MAX_LINE).read_line(line).map_err(|_| "stopped".to_string())?;
    if n as u64 == MAX_LINE && !line.ends_with('\n') {
        return Err("too_long".to_string());
    }
    Ok(n)
}

/// A connection to the panel, on IPv6 when that is where it listens. Nothing is sent yet.
pub fn connect(panel: &Panel) -> Result<TcpStream, String> {
    let address: SocketAddr = if panel.ipv6 {
        (std::net::Ipv6Addr::LOCALHOST, panel.port).into()
    } else {
        (std::net::Ipv4Addr::LOCALHOST, panel.port).into()
    };
    TcpStream::connect_timeout(&address, Duration::from_secs(2)).map_err(|_| "not_running".to_string())
}

/// Connect to the panel and read the stream until it ends. `check` runs before anything is sent (whose
/// program listens there), `on_open` once the panel has accepted, and `on_event` for each event.
pub fn follow(
    panel: &Panel,
    check: impl FnOnce() -> Result<(), String>,
    on_open: impl FnOnce(),
    mut on_event: impl FnMut(serde_json::Value),
) -> Result<(), String> {
    let mut conn = connect(panel)?;
    check()?;
    // The panel sends a keep-alive every 15 seconds; 40 seconds of silence means it is gone.
    conn.set_read_timeout(Some(Duration::from_secs(40))).ok();
    conn.write_all(request(panel).as_bytes()).map_err(|_| "stopped".to_string())?;
    let mut reader = std::io::BufReader::new(conn);
    let mut line = String::new();
    read_line(&mut reader, &mut line)?;
    if line.split_whitespace().nth(1) != Some("200") {
        return Err("refused".to_string());
    }
    for _ in 0..MAX_HEADERS {
        if read_line(&mut reader, &mut line)? == 0 {
            return Err("stopped".to_string());
        }
        if line.trim().is_empty() {
            on_open();
            loop {
                if read_line(&mut reader, &mut line)? == 0 {
                    return Err("stopped".to_string());
                }
                if let Some(value) = event(&line) {
                    on_event(value);
                }
            }
        }
    }
    Err("refused".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn panel(port: u16, c: char) -> Panel {
        Panel { port, token: c.to_string().repeat(43), ipv6: false }
    }

    /// A fake panel that reads the request line, then writes `reply`.
    fn fake(reply: &'static [u8]) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            let (mut conn, _) = listener.accept().unwrap();
            let mut first = String::new();
            std::io::BufReader::new(conn.try_clone().unwrap()).read_line(&mut first).unwrap();
            assert!(first.starts_with("GET /events?token="));
            conn.write_all(reply).unwrap();
        });
        port
    }

    #[test]
    fn reads_a_data_line_as_its_event() {
        let value = event("data: {\"kind\": \"said\", \"text\": \"Noon.\"}\r\n").unwrap();
        assert_eq!(value["kind"], "said");
    }

    #[test]
    fn skips_keep_alives_blank_lines_and_what_is_not_an_event() {
        for line in [": keep-alive\n", "\n", "data: not json\n", "data: [1, 2]\n", "event: x\n"] {
            assert_eq!(event(line), None, "{line:?}");
        }
    }

    #[test]
    fn asks_for_the_stream_on_this_computer_with_the_token() {
        let request = request(&panel(8765, 't'));
        assert!(request.starts_with(&format!("GET /events?token={} HTTP/1.1\r\n", "t".repeat(43))));
        assert!(request.contains("\r\nHost: 127.0.0.1:8765\r\n") && request.ends_with("\r\n\r\n"));
    }

    #[test]
    fn follows_a_stream_from_a_fake_panel_and_says_when_it_opened() {
        let port = fake(b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\n\r\n\
            data: {\"kind\": \"state\", \"state\": \"idle\"}\n\n: keep-alive\n\n\
            data: {\"kind\": \"said\", \"text\": \"Noon.\"}\n\n");
        let (mut opened, mut kinds) = (false, vec![]);
        let end = follow(&panel(port, 't'), || Ok(()), || opened = true, |e| kinds.push(e["kind"].to_string()));
        assert_eq!(end, Err("stopped".to_string()));
        assert!(opened);
        assert_eq!(kinds, ["\"state\"", "\"said\""]);
    }

    #[test]
    fn a_refused_stream_never_opens() {
        let port = fake(b"HTTP/1.0 403 Forbidden\r\n\r\n");
        let mut opened = false;
        assert_eq!(follow(&panel(port, 's'), || Ok(()), || opened = true, |_| {}), Err("refused".to_string()));
        assert!(!opened);
    }

    #[test]
    fn nothing_is_sent_when_the_listener_is_not_ours() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let got = std::thread::spawn(move || {
            let (mut conn, _) = listener.accept().unwrap();
            let mut sent = vec![];
            conn.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            let _ = conn.read_to_end(&mut sent);
            sent
        });
        let end = follow(&panel(port, 'x'), || Err("not_ours".to_string()), || {}, |_| {});
        assert_eq!(end, Err("not_ours".to_string()));
        assert!(got.join().unwrap().is_empty());
    }

    #[test]
    fn a_line_with_no_end_is_cut_off() {
        let mut long = std::io::Cursor::new(vec![b'a'; MAX_LINE as usize + 10]);
        assert_eq!(read_line(&mut long, &mut String::new()), Err("too_long".to_string()));
    }

    #[test]
    fn nothing_listening_means_rafiq_is_not_running() {
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();  // dropped: closed
        assert_eq!(follow(&panel(port, 't'), || Ok(()), || {}, |_| {}), Err("not_running".to_string()));
    }
}
