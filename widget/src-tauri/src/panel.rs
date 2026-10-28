//! Where the running Rafiq's panel is, and its token: read from the same files `rafiq ask` uses.
//! The token stays on this side; the web view never sees it. Problems are codes the page says in the
//! user's language (ui/phrases.js), and never contain the token.

use std::path::{Path, PathBuf};

pub const DEFAULT_PORT: u16 = 8765;

#[derive(Debug, PartialEq)]
pub struct Panel {
    pub port: u16,
    pub token: String,
    pub ipv6: bool, // `[panel] host = "::1"`: the panel then listens on IPv6 only
}

/// `RAFIQ_HOME`, or `~/.config/rafiq`, as in rafiq/config.py.
pub fn home() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("RAFIQ_HOME").filter(|d| !d.is_empty()) {
        return Some(PathBuf::from(dir));
    }
    std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config").join("rafiq"))
}

/// The panel of the Rafiq whose settings are in `home`.
pub fn find(home: &Path) -> Result<Panel, String> {
    let token = std::fs::read_to_string(home.join("panel.token"))
        .map_err(|_| "no_token".to_string())?
        .trim()
        .to_string();
    // Rafiq writes 43 URL-safe characters (secrets.token_urlsafe(32)); anything else isn't its token.
    if token.len() < 32 || !token.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        return Err("bad_token".to_string());
    }
    let (port, ipv6) = settings(home)?;
    Ok(Panel { port, token, ipv6 })
}

/// `[panel] port` and `host` from config.toml. The panel only ever listens on this computer, so another
/// host is refused.
fn settings(home: &Path) -> Result<(u16, bool), String> {
    let Ok(text) = std::fs::read_to_string(home.join("config.toml")) else {
        return Ok((DEFAULT_PORT, false));
    };
    let config: toml::Table = text.parse().map_err(|_| "bad_config".to_string())?;
    let Some(panel) = config.get("panel").and_then(|p| p.as_table()) else {
        return Ok((DEFAULT_PORT, false));
    };
    let ipv6 = match panel.get("host").and_then(|h| h.as_str()) {
        None | Some("127.0.0.1" | "localhost") => false,
        Some("::1") => true,
        Some(_) => return Err("other_host".to_string()),
    };
    let port = match panel.get("port") {
        None => DEFAULT_PORT,
        Some(p) => p.as_integer().and_then(|n| u16::try_from(n).ok()).ok_or("bad_config".to_string())?,
    };
    Ok((port, ipv6))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn folder(name: &str, files: &[(&str, &str)]) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("rafiq-widget-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        for (file, text) in files {
            std::fs::write(dir.join(file), text).unwrap();
        }
        dir
    }

    const TOKEN: &str = "Zr8f2Qm4Tq9W1xKcV7bN3pLs6Hd0Jy5Ea_E-u2RtMoI";

    #[test]
    fn finds_the_token_and_the_default_port() {
        let home = folder("default", &[("panel.token", &format!("{TOKEN}\n"))]);
        assert_eq!(find(&home), Ok(Panel { port: DEFAULT_PORT, token: TOKEN.to_string(), ipv6: false }));
    }

    #[test]
    fn reads_the_port_from_the_settings() {
        let home = folder("port", &[("panel.token", TOKEN), ("config.toml", "[panel]\nport = 9000\n")]);
        assert_eq!(find(&home).unwrap().port, 9000);
    }

    #[test]
    fn connects_over_ipv6_when_the_panel_listens_there() {
        let home = folder("ipv6", &[("panel.token", TOKEN), ("config.toml", "[panel]\nhost = \"::1\"\n")]);
        assert!(find(&home).unwrap().ipv6);
    }

    #[test]
    fn says_rafiq_has_not_started_yet_when_there_is_no_token() {
        assert_eq!(find(&folder("none", &[])), Err("no_token".to_string()));
    }

    #[test]
    fn refuses_a_token_rafiq_did_not_write() {
        assert_eq!(find(&folder("odd", &[("panel.token", "short")])), Err("bad_token".to_string()));
        let home = folder("spaces", &[("panel.token", "a token with spaces and more than 32 characters")]);
        assert_eq!(find(&home), Err("bad_token".to_string()));
    }

    #[test]
    fn refuses_a_panel_on_another_host() {
        let home = folder("host", &[("panel.token", TOKEN), ("config.toml", "[panel]\nhost = \"0.0.0.0\"\n")]);
        assert_eq!(find(&home), Err("other_host".to_string()));
    }

    #[test]
    fn refuses_a_port_that_is_not_one() {
        let home = folder("badport", &[("panel.token", TOKEN), ("config.toml", "[panel]\nport = 70000\n")]);
        assert_eq!(find(&home), Err("bad_config".to_string()));
    }
}
