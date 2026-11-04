//! Whose socket the widget is about to talk to. The panel token outlives Rafiq (panel.token is kept between
//! starts), so while Rafiq is stopped another account could put its own program where Rafiq listens and collect
//! the token from a widget that keeps reconnecting (#199, #234). Rafiq's programs therefore talk over a socket
//! in `<settings>/run`, and the token is sent only when that folder is this user's alone (not a link, mode 0700)
//! and the socket in it is this user's: then no one else can have made it.

#[cfg(unix)]
use std::path::Path;

/// Ok when `socket` is a socket of `uid`'s in a folder only `uid` can open; a problem code otherwise.
#[cfg(unix)]
pub fn ours(socket: &Path, uid: u32) -> Result<(), String> {
    use std::os::unix::fs::{FileTypeExt, MetadataExt};

    let folder = socket.parent().ok_or("not_ours".to_string())?;
    let folder = std::fs::symlink_metadata(folder).map_err(|_| "not_running".to_string())?;
    if !folder.is_dir() || folder.uid() != uid || folder.mode() & 0o077 != 0 {
        return Err("not_ours".to_string());
    }
    let file = std::fs::symlink_metadata(socket).map_err(|_| "not_running".to_string())?;
    if !file.file_type().is_socket() || file.uid() != uid {
        return Err("not_ours".to_string());
    }
    Ok(())
}

/// Is the panel's socket this user's?
#[cfg(unix)]
pub fn check(panel: &crate::panel::Panel) -> Result<(), String> {
    // SAFETY: getuid has no preconditions and cannot fail.
    ours(&panel.socket, unsafe { libc::getuid() })
}

/// Whose program is at the other end of a connection, as the kernel says; None when it can't be told.
#[cfg(unix)]
pub fn peer_uid(conn: &std::os::unix::net::UnixStream) -> Option<u32> {
    use std::os::fd::AsRawFd;

    let fd = conn.as_raw_fd();
    #[cfg(target_os = "linux")]
    {
        // SAFETY: getsockopt writes at most `len` bytes into a ucred we own, on a descriptor `conn` keeps open.
        let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
        let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
        let got = unsafe {
            libc::getsockopt(fd, libc::SOL_SOCKET, libc::SO_PEERCRED, &mut cred as *mut _ as *mut libc::c_void, &mut len)
        };
        (got == 0 && len as usize == std::mem::size_of::<libc::ucred>()).then_some(cred.uid)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let (mut uid, mut gid) = (0, 0);
        // SAFETY: getpeereid writes two ids we own, on a descriptor `conn` keeps open.
        (unsafe { libc::getpeereid(fd, &mut uid, &mut gid) } == 0).then_some(uid)
    }
}

/// Ok only when the kernel says this user's program is at the other end: a check of the connection itself, so
/// a socket swapped in after `check` looked at the path still gets nothing.
#[cfg(unix)]
pub fn peer_ok(conn: &std::os::unix::net::UnixStream) -> Result<(), String> {
    // SAFETY: getuid has no preconditions and cannot fail.
    match peer_uid(conn) {
        Some(uid) if uid == unsafe { libc::getuid() } => Ok(()),
        _ => Err("not_ours".to_string()),
    }
}

/// On Windows (#193) the widget still talks over TCP, and nothing here can tell whose program listens.
#[cfg(not(unix))]
pub fn check(_panel: &crate::panel::Panel) -> Result<(), String> {
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn me() -> u32 {
        unsafe { libc::getuid() }
    }

    #[test]
    fn the_kernel_says_this_user_s_program_is_at_the_other_end() {
        let (a, _b) = std::os::unix::net::UnixStream::pair().unwrap();
        assert_eq!(peer_uid(&a), Some(me()));
        assert_eq!(peer_ok(&a), Ok(()));
    }

    #[test]
    fn a_socket_of_this_user_s_in_a_closed_folder_is_ours() {
        let (panel, _listener) = crate::testing::listener("ours");
        assert_eq!(ours(&panel.socket, me()), Ok(()));
        assert_eq!(check(&panel), Ok(()));
    }

    #[test]
    fn a_folder_others_can_open_is_not_ours() {
        let (panel, _listener) = crate::testing::listener("open");
        std::fs::set_permissions(panel.socket.parent().unwrap(), std::fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(ours(&panel.socket, me()), Err("not_ours".to_string()));
    }

    #[test]
    fn another_account_s_socket_is_not_ours() {
        let (panel, _listener) = crate::testing::listener("theirs");
        assert_eq!(ours(&panel.socket, me() + 1), Err("not_ours".to_string()));
    }

    #[test]
    fn a_plain_file_is_not_a_socket() {
        let (panel, listener) = crate::testing::listener("file");
        drop(listener);
        std::fs::remove_file(&panel.socket).unwrap();
        std::fs::write(&panel.socket, "").unwrap();
        assert_eq!(ours(&panel.socket, me()), Err("not_ours".to_string()));
    }

    #[test]
    fn a_linked_folder_is_not_ours() {
        let (panel, _listener) = crate::testing::listener("real");
        let link = std::env::temp_dir().join(format!("rafiq-widget-link-{}", std::process::id()));
        let _ = std::fs::remove_file(&link);
        std::os::unix::fs::symlink(panel.socket.parent().unwrap(), &link).unwrap();
        assert_eq!(ours(&link.join("panel.sock"), me()), Err("not_ours".to_string()));
    }

    #[test]
    fn no_socket_means_rafiq_is_not_running() {
        let (panel, listener) = crate::testing::listener("gone");
        drop(listener);
        std::fs::remove_file(&panel.socket).unwrap();
        assert_eq!(ours(&panel.socket, me()), Err("not_running".to_string()));
    }
}
