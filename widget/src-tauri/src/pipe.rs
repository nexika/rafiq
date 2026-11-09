//! On Windows (#242) the widget reaches Rafiq through its named pipe, `\\.\pipe\rafiq-panel-<SID>`, as Rafiq's own
//! programs do (rafiq/winpipe.py). The token is sent only once the pipe's own security descriptor says Rafiq made
//! it (descriptor.rs): another account can't make an object owned by this user's SID, and a Low-integrity
//! program's gets a Low label. The pipe is opened at the identification level, so Rafiq can't act as the widget.
//! Nothing falls back to the panel's TCP port, where another account's program could be listening.

use std::ffi::c_void;
use std::fs::File;
use std::io::{Read, Write};
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::AsRawHandle;
use std::time::{Duration, Instant};

use crate::descriptor;
use crate::panel::Panel;

type Handle = *mut c_void;

const SECURITY_IDENTIFICATION: u32 = 0x0001_0000; // with SECURITY_SQOS_PRESENT, which std adds
const SE_KERNEL_OBJECT: i32 = 6;
const OWNER_DACL_LABEL: u32 = 0x1 | 0x4 | 0x10;
const TOKEN_QUERY: u32 = 0x8;
const TOKEN_USER: i32 = 1;
const ERROR_BROKEN_PIPE: i32 = 109;
const ERROR_PIPE_BUSY: i32 = 231;
const ERROR_NO_DATA: i32 = 232;
const ERROR_PIPE_NOT_CONNECTED: i32 = 233;
const POLL: Duration = Duration::from_millis(10);
const MAX_POLL: Duration = Duration::from_millis(50);

#[link(name = "kernel32")]
extern "system" {
    fn PeekNamedPipe(pipe: Handle, buf: *mut c_void, size: u32, read: *mut u32, avail: *mut u32, left: *mut u32) -> i32;
    fn LocalFree(mem: *mut c_void) -> *mut c_void;
    fn GetCurrentProcess() -> Handle;
    fn CloseHandle(handle: Handle) -> i32;
}

#[link(name = "advapi32")]
extern "system" {
    fn GetSecurityInfo(
        handle: Handle,
        kind: i32,
        info: u32,
        owner: *mut *mut c_void,
        group: *mut *mut c_void,
        dacl: *mut *mut c_void,
        sacl: *mut *mut c_void,
        descriptor: *mut *mut c_void,
    ) -> u32;
    fn ConvertSecurityDescriptorToStringSecurityDescriptorW(
        descriptor: *mut c_void,
        revision: u32,
        info: u32,
        text: *mut *mut u16,
        len: *mut u32,
    ) -> i32;
    fn ConvertStringSidToSidW(text: *const u16, sid: *mut *mut c_void) -> i32;
    fn ConvertSidToStringSidW(sid: *mut c_void, text: *mut *mut u16) -> i32;
    fn OpenProcessToken(process: Handle, access: u32, token: *mut Handle) -> i32;
    fn GetTokenInformation(token: Handle, class: i32, info: *mut c_void, len: u32, needed: *mut u32) -> i32;
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

/// A string Windows allocated with LocalAlloc, read and freed.
///
/// SAFETY: `text` is null or a NUL-terminated UTF-16 string from LocalAlloc that nothing else frees.
unsafe fn take_wide(text: *mut u16) -> Option<String> {
    if text.is_null() {
        return None;
    }
    let mut len = 0;
    while *text.add(len) != 0 {
        len += 1;
    }
    let value = String::from_utf16(std::slice::from_raw_parts(text, len)).ok();
    LocalFree(text as *mut c_void);
    value
}

/// This user's SID, from this process's own token.
pub fn my_sid() -> Option<String> {
    // SAFETY: each call gets buffers and out-pointers we own; the token handle is closed before returning.
    unsafe {
        let mut token: Handle = std::ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
            return None;
        }
        let mut needed = 0u32;
        GetTokenInformation(token, TOKEN_USER, std::ptr::null_mut(), 0, &mut needed);
        // u64s, so the buffer is aligned for the pointer TOKEN_USER starts with.
        let mut buffer = vec![0u64; (needed as usize).div_ceil(8).max(1)];
        let got = GetTokenInformation(token, TOKEN_USER, buffer.as_mut_ptr() as *mut c_void, needed, &mut needed);
        CloseHandle(token);
        if got == 0 {
            return None;
        }
        let sid = *(buffer.as_ptr() as *const *mut c_void); // TOKEN_USER begins with the SID's address
        let mut text: *mut u16 = std::ptr::null_mut();
        if ConvertSidToStringSidW(sid, &mut text) == 0 {
            return None;
        }
        take_wide(text)
    }
}

/// An SDDL account abbreviation (LA: this computer's Administrator) as the full SID Windows means by it.
fn full_sid(alias: &str) -> Option<String> {
    let name = wide(alias);
    // SAFETY: `name` is NUL-terminated and outlives the call; the SID Windows allocates is freed here.
    unsafe {
        let mut sid: *mut c_void = std::ptr::null_mut();
        if ConvertStringSidToSidW(name.as_ptr(), &mut sid) == 0 {
            return None;
        }
        let mut text: *mut u16 = std::ptr::null_mut();
        let converted = ConvertSidToStringSidW(sid, &mut text);
        LocalFree(sid);
        if converted == 0 {
            return None;
        }
        take_wide(text)
    }
}

/// The pipe's owner, DACL and integrity label in SDDL, from the pipe object behind this handle.
fn security(pipe: &File) -> Option<String> {
    let handle = pipe.as_raw_handle() as Handle;
    // SAFETY: `pipe` keeps the handle open for the call; the descriptor and string Windows allocates are freed.
    unsafe {
        let mut found: *mut c_void = std::ptr::null_mut();
        let none = std::ptr::null_mut();
        if GetSecurityInfo(handle, SE_KERNEL_OBJECT, OWNER_DACL_LABEL, none, none, none, none, &mut found) != 0 {
            return None;
        }
        let mut text: *mut u16 = std::ptr::null_mut();
        let converted = ConvertSecurityDescriptorToStringSecurityDescriptorW(
            found,
            1,
            OWNER_DACL_LABEL,
            &mut text,
            std::ptr::null_mut(),
        );
        LocalFree(found);
        if converted == 0 {
            return None;
        }
        take_wide(text)
    }
}

pub fn pipe_name(sid: &str) -> String {
    format!(r"\\.\pipe\rafiq-panel-{sid}")
}

/// One end of Rafiq's pipe: reads wait for data no longer than the read timeout, as a socket's would.
pub struct Conn {
    file: File,
    timeout: Option<Duration>,
}

impl Conn {
    pub fn set_read_timeout(&mut self, timeout: Option<Duration>) -> std::io::Result<()> {
        self.timeout = timeout;
        Ok(())
    }

    /// Bytes waiting to be read; None once Rafiq closed its end.
    fn waiting(&self) -> std::io::Result<Option<u32>> {
        let mut avail = 0u32;
        let handle = self.file.as_raw_handle() as Handle;
        let null = std::ptr::null_mut();
        // SAFETY: `file` keeps the handle open; only the count is written, into a u32 we own.
        if unsafe { PeekNamedPipe(handle, null, 0, null as *mut u32, &mut avail, null as *mut u32) } == 0 {
            let error = std::io::Error::last_os_error();
            return match error.raw_os_error() {
                Some(ERROR_BROKEN_PIPE | ERROR_NO_DATA | ERROR_PIPE_NOT_CONNECTED) => Ok(None),
                _ => Err(error),
            };
        }
        Ok(Some(avail))
    }
}

impl Read for Conn {
    /// A pipe opened for plain (not overlapped) reads can't stop a ReadFile, so it is called only once data is
    /// there.
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let deadline = self.timeout.map(|t| Instant::now() + t);
        let mut pause = POLL;
        loop {
            match self.waiting()? {
                None => return Ok(0),
                Some(0) => {}
                Some(n) => {
                    let n = buf.len().min(n as usize);
                    return self.file.read(&mut buf[..n]);
                }
            }
            if deadline.is_some_and(|d| Instant::now() >= d) {
                return Err(std::io::Error::new(std::io::ErrorKind::TimedOut, "no answer in time"));
            }
            std::thread::sleep(pause);
            pause = (pause * 2).min(MAX_POLL);
        }
    }
}

impl Write for Conn {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.file.write(buf)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.file.flush()
    }
}

/// Open `name` within 2 seconds (a Rafiq that is stuck must not hang the widget), at the identification level.
fn open(name: &str) -> Result<File, String> {
    let started = Instant::now();
    loop {
        match std::fs::OpenOptions::new().read(true).write(true).security_qos_flags(SECURITY_IDENTIFICATION).open(name) {
            Ok(file) => return Ok(file),
            Err(e) if e.raw_os_error() == Some(ERROR_PIPE_BUSY) && started.elapsed() < Duration::from_secs(2) => {
                std::thread::sleep(POLL);
            }
            Err(_) => return Err("not_running".to_string()), // no pipe: no Rafiq running
        }
    }
}

/// A connection to `name`, only when the pipe is one this user's Rafiq made. Nothing is sent yet.
pub fn connect_to(name: &str, me: &str) -> Result<Conn, String> {
    let file = open(name)?;
    let text = security(&file).map(|t| descriptor::expand_sids(&t, full_sid)).unwrap_or_default();
    if !descriptor::ok(&text, me) {
        return Err("not_ours".to_string()); // dropping `file` closes it, with nothing sent
    }
    Ok(Conn { file, timeout: None })
}

/// The running Rafiq's pipe, for this user.
pub fn connect(_panel: &Panel) -> Result<Conn, String> {
    let me = my_sid().ok_or("not_ours".to_string())?;
    connect_to(&pipe_name(&me), &me)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::os::windows::io::FromRawHandle;
    use std::sync::atomic::{AtomicUsize, Ordering};

    const PIPE_ACCESS_DUPLEX: u32 = 0x3;
    const FILE_FLAG_FIRST_PIPE_INSTANCE: u32 = 0x0008_0000;
    const PIPE_REJECT_REMOTE_CLIENTS: u32 = 0x8;
    const ERROR_PIPE_CONNECTED: i32 = 535;

    #[repr(C)]
    struct SecurityAttributes {
        length: u32,
        descriptor: *mut c_void,
        inherit: i32,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateNamedPipeW(
            name: *const u16,
            open_mode: u32,
            pipe_mode: u32,
            instances: u32,
            out_size: u32,
            in_size: u32,
            timeout: u32,
            security: *const SecurityAttributes,
        ) -> Handle;
        fn ConnectNamedPipe(pipe: Handle, overlapped: *mut c_void) -> i32;
    }

    #[link(name = "advapi32")]
    extern "system" {
        fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
            text: *const u16,
            revision: u32,
            descriptor: *mut *mut c_void,
            len: *mut u32,
        ) -> i32;
    }

    static NEXT: AtomicUsize = AtomicUsize::new(0);

    fn name() -> String {
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        format!(r"\\.\pipe\rafiq-widget-test-{}-{n}", std::process::id())
    }

    /// A pipe made with `sddl`, as Rafiq (or a program that took its name) would make it.
    fn make(name: &str, sddl: &str) -> File {
        let text = wide(sddl);
        let pipe = wide(name);
        // SAFETY: the strings outlive the calls; the descriptor is freed once the pipe is made.
        unsafe {
            let mut descriptor: *mut c_void = std::ptr::null_mut();
            let made = ConvertStringSecurityDescriptorToSecurityDescriptorW(
                text.as_ptr(),
                1,
                &mut descriptor,
                std::ptr::null_mut(),
            );
            assert_ne!(made, 0, "the test's descriptor: {}", std::io::Error::last_os_error());
            let attributes = SecurityAttributes {
                length: std::mem::size_of::<SecurityAttributes>() as u32,
                descriptor,
                inherit: 0,
            };
            let handle = CreateNamedPipeW(
                pipe.as_ptr(),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_REJECT_REMOTE_CLIENTS,
                1,
                65536,
                65536,
                0,
                &attributes,
            );
            LocalFree(descriptor);
            assert!(!handle.is_null() && handle as isize != -1, "CreateNamedPipeW: {}", std::io::Error::last_os_error());
            File::from_raw_handle(handle as _)
        }
    }

    /// Serve one request on `pipe`: what was sent, after answering `reply`.
    fn serve(pipe: File, reply: &'static [u8]) -> std::thread::JoinHandle<Vec<u8>> {
        std::thread::spawn(move || {
            let handle = pipe.as_raw_handle() as Handle;
            // SAFETY: `pipe` keeps the handle open for the call.
            let connected = unsafe { ConnectNamedPipe(handle, std::ptr::null_mut()) } != 0
                || std::io::Error::last_os_error().raw_os_error() == Some(ERROR_PIPE_CONNECTED);
            if !connected {
                return Vec::new();
            }
            let mut reader = BufReader::new(&pipe);
            let mut sent = Vec::new();
            let mut line = String::new();
            while reader.read_line(&mut line).unwrap_or(0) > 0 {
                sent.extend_from_slice(line.as_bytes());
                if line == "\r\n" {
                    break;
                }
                line.clear();
            }
            let _ = (&pipe).write_all(reply);
            sent
        })
    }

    fn rafiqs(me: &str) -> String {
        format!("O:{me}D:P(A;;GA;;;{me})S:(ML;;NW;;;ME)") // rafiq/winpipe.py's sddl()
    }

    #[test]
    fn this_user_s_sid_comes_from_the_process_token() {
        assert!(my_sid().is_some_and(|sid| sid.starts_with("S-1-5-")));
    }

    #[test]
    fn rafiq_s_own_pipe_is_let_through_and_carries_the_request() {
        let me = my_sid().unwrap();
        let name = name();
        let got = serve(make(&name, &rafiqs(&me)), b"HTTP/1.0 200 OK\r\n\r\nhello");
        let mut conn = connect_to(&name, &me).expect("Rafiq's own pipe");
        conn.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        conn.write_all(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n").unwrap();
        let mut answer = String::new();
        conn.read_to_string(&mut answer).unwrap();
        assert_eq!(answer, "HTTP/1.0 200 OK\r\n\r\nhello");
        assert!(got.join().unwrap().starts_with(b"GET /events "));
    }

    #[test]
    fn a_pipe_open_to_everyone_gets_nothing() {
        // Another program took the name while Rafiq was down, open to all.
        let me = my_sid().unwrap();
        let name = name();
        let pipe = make(&name, &format!("O:{me}D:P(A;;GA;;;WD)"));
        let got = serve(pipe, b"HTTP/1.0 200 OK\r\n\r\n");
        assert_eq!(connect_to(&name, &me).err(), Some("not_ours".to_string()));
        assert!(got.join().unwrap().is_empty());
    }

    #[test]
    fn a_pipe_labelled_low_gets_nothing() {
        // A sandboxed program of this user's (Low integrity) made it.
        let me = my_sid().unwrap();
        let name = name();
        let got = serve(make(&name, &format!("O:{me}D:P(A;;GA;;;{me})S:(ML;;NW;;;LW)")), b"HTTP/1.0 200 OK\r\n\r\n");
        assert_eq!(connect_to(&name, &me).err(), Some("not_ours".to_string()));
        assert!(got.join().unwrap().is_empty());
    }

    #[test]
    fn a_pipe_of_someone_else_s_gets_nothing() {
        // The pipe is this test's own; the widget is told this user is someone else, as when another account owns it.
        let me = my_sid().unwrap();
        let name = name();
        let got = serve(make(&name, &rafiqs(&me)), b"HTTP/1.0 200 OK\r\n\r\n");
        assert_eq!(connect_to(&name, "S-1-5-21-1-2-3-1002").err(), Some("not_ours".to_string()));
        assert!(got.join().unwrap().is_empty());
    }

    #[test]
    fn no_pipe_means_rafiq_is_not_running() {
        let me = my_sid().unwrap();
        assert_eq!(connect_to(&name(), &me).err(), Some("not_running".to_string()));
    }

    #[test]
    fn a_read_with_nothing_coming_times_out() {
        let me = my_sid().unwrap();
        let name = name();
        let pipe = make(&name, &rafiqs(&me));
        let mut conn = connect_to(&name, &me).unwrap();
        conn.set_read_timeout(Some(Duration::from_millis(200))).unwrap();
        let mut buf = [0u8; 16];
        let error = conn.read(&mut buf).unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::TimedOut);
        drop(pipe);
    }
}
