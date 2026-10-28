//! Whose program listens on the panel's port. The panel token outlives Rafiq (panel.token is kept between
//! starts), so while Rafiq is stopped another program could take the port and collect the token from a
//! widget that keeps reconnecting. The token is only sent to a listener of this user's own (#199).

/// The user id of the program listening on `port` on loopback or every address, from the text of
/// /proc/net/tcp (or tcp6). None when nothing listens there.
pub fn listener_uid(table: &str, port: u16) -> Option<u32> {
    let port = format!("{port:04X}");
    table.lines().skip(1).find_map(|line| {
        let fields: Vec<&str> = line.split_whitespace().collect();
        let (address, at) = fields.get(1)?.split_once(':')?;
        let listening = fields.get(3) == Some(&"0A");
        let here = matches!(address, "0100007F" | "00000000" | "00000000000000000000000001000000")
            || address.chars().all(|c| c == '0');
        (listening && here && at == port).then(|| fields.get(7)?.parse().ok()).flatten()
    })
}

/// Ok when this user's own program listens on the port; a problem code otherwise.
#[cfg(target_os = "linux")]
pub fn check(port: u16, ipv6: bool) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;

    let me = std::fs::metadata("/proc/self").map_err(|_| "io".to_string())?.uid();
    let table = std::fs::read_to_string(if ipv6 { "/proc/net/tcp6" } else { "/proc/net/tcp" })
        .map_err(|_| "io".to_string())?;
    match listener_uid(&table, port) {
        Some(uid) if uid == me => Ok(()),
        Some(_) => Err("not_ours".to_string()),
        None => Err("not_running".to_string()),
    }
}

/// Elsewhere there is no /proc to read; the widget ships for Linux first (#192, #193 come later).
#[cfg(not(target_os = "linux"))]
pub fn check(_port: u16, _ipv6: bool) -> Result<(), String> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const HEADER: &str = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n";

    fn table(rows: &[&str]) -> String {
        format!("{HEADER}{}\n", rows.join("\n"))
    }

    #[test]
    fn finds_who_listens_on_the_port_on_loopback() {
        let t = table(&["   0: 0100007F:223D 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1"]);
        assert_eq!(listener_uid(&t, 8765), Some(1000));
    }

    #[test]
    fn a_connection_to_the_port_is_not_a_listener() {
        let t = table(&["   0: 0100007F:223D 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000        0 1 1"]);
        assert_eq!(listener_uid(&t, 8765), None);
    }

    #[test]
    fn another_port_or_another_address_does_not_count() {
        let t = table(&[
            "   0: 0100007F:2231 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1",
            "   1: 1401A8C0:223D 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1",
        ]);
        assert_eq!(listener_uid(&t, 8765), None);
    }

    #[test]
    fn another_user_listening_is_found_with_their_id() {
        let t = table(&["   0: 00000000:223D 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 1 1"]);
        assert_eq!(listener_uid(&t, 8765), Some(1001));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_listener_this_user_opened_is_ours_and_a_closed_port_is_nobody_s() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert_eq!(check(port, false), Ok(()));
        drop(listener);
        assert_eq!(check(port, false), Err("not_running".to_string()));
    }

    #[test]
    fn reads_ipv6_loopback_too() {
        let t = table(&["   0: 00000000000000000000000001000000:223D 00000000000000000000000000000000:0000 0A \
                         00000000:00000000 00:00000000 00000000  1000        0 1 1"]);
        assert_eq!(listener_uid(&t, 8765), Some(1000));
    }
}
