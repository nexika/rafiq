//! A fake panel socket for the tests: a folder only this user can open, with a listening socket in it.

use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::sync::atomic::{AtomicUsize, Ordering};

use crate::panel::Panel;

static NEXT: AtomicUsize = AtomicUsize::new(0);

/// A fresh folder each call, so tests running at once never share a socket.
pub fn listener(name: &str) -> (Panel, UnixListener) {
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let folder = std::env::temp_dir().join(format!("rafiq-widget-{name}-{}-{n}", std::process::id())).join("run");
    let _ = std::fs::remove_dir_all(&folder);
    std::fs::create_dir_all(&folder).unwrap();
    std::fs::set_permissions(&folder, std::fs::Permissions::from_mode(0o700)).unwrap();
    let socket = folder.join("panel.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    (Panel { port: 8765, token: "k".repeat(43), ipv6: false, socket }, listener)
}
