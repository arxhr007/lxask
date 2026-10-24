//! `lxask --serve`: keep the model loaded and answer over a Unix socket.
//!
//! Loading the model, the encoder and the index is most of the time an answer takes on
//! a CPU. With a server running, `lxask "..."` sends the question to it and prints the
//! reply, so only the answering is left. A server holds the model's memory (about
//! 1.3 GB) until it has been idle for `idle` minutes or is stopped with `lxask --stop`.
//! With `serve = auto` in the settings (install.sh asks), a question that finds no
//! server starts one in the background and asks it (`start_and_wait`); otherwise
//! nothing starts a server on its own.
//!
//! The socket is in a directory only the user can open, and the server answers only
//! connections from its own user. Like the rest of lxask it prints commands and never
//! runs them.

use std::fs;
use std::io::{ErrorKind, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::config;

pub const SOCKET: &str = "lxask.sock";
/// a question and four flags
pub const MAX_REQUEST: usize = 8192;
pub const IDLE_MINUTES: f64 = 30.0;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
/// a long multi-part answer on a slow CPU
pub const ANSWER_TIMEOUT: Duration = Duration::from_secs(600);
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

pub fn socket_path() -> PathBuf {
    match std::env::var("XDG_RUNTIME_DIR").ok().filter(|v| !v.is_empty()) {
        Some(runtime) => PathBuf::from(runtime).join("lxask").join(SOCKET),
        None => config::data_home().join("run").join(SOCKET),
    }
}

fn send(stream: &mut UnixStream, message: &Value) -> std::io::Result<()> {
    let mut line = serde_json::to_vec(message).map_err(std::io::Error::other)?;
    line.push(b'\n');
    stream.write_all(&line)
}

fn receive(stream: &mut UnixStream, limit: Option<usize>) -> std::io::Result<Value> {
    let mut data = Vec::new();
    let mut chunk = [0u8; 65536];
    while !data.ends_with(b"\n") {
        let n = stream.read(&mut chunk)?;
        if n == 0 {
            break;
        }
        data.extend_from_slice(&chunk[..n]);
        if limit.is_some_and(|limit| data.len() > limit) {
            return Err(std::io::Error::new(ErrorKind::InvalidData, "request too long"));
        }
    }
    serde_json::from_slice(&data).map_err(|e| std::io::Error::new(ErrorKind::InvalidData, e))
}

/// The server's reply to `message`, or None when no server is running (a socket left
/// behind by one that died is removed).
pub fn request(message: Value, path: Option<&Path>, timeout: Duration) -> Option<Value> {
    let path = path.map(Path::to_path_buf).unwrap_or_else(socket_path);
    if !path.exists() {
        return None;
    }
    let mut stream = match UnixStream::connect(&path) {
        Ok(stream) => stream,
        Err(error) if error.kind() == ErrorKind::ConnectionRefused => {
            let _ = fs::remove_file(&path);
            return None;
        }
        Err(_) => return None,
    };
    let _ = stream.set_write_timeout(Some(CONNECT_TIMEOUT));
    let _ = stream.set_read_timeout(Some(timeout));
    let mut message = match message {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    message.insert("version".into(), Value::from(VERSION));
    send(&mut stream, &Value::Object(message)).ok()?;
    receive(&mut stream, None).ok()
}

/// next to the socket: held by the one caller that is starting a server
const STARTING_LOCK: &str = "starting.lock";
/// loading the model on a slow CPU
pub const START_TIMEOUT: Duration = Duration::from_secs(120);

fn running(path: &Path) -> bool {
    request(json!({"type": "ping"}), Some(path), CONNECT_TIMEOUT).is_some()
}

/// Make sure a server is answering on the socket, starting `lxask --serve` in the
/// background if none is: true once one answers, false if none came up within `wait`.
/// Only one caller starts it (a lock next to the socket); a caller that finds the lock
/// taken waits for that server instead of loading a second model. The server is
/// detached from this terminal and stops on its own when idle.
pub fn start_and_wait(wait: Duration) -> bool {
    let path = socket_path();
    let Some(dir) = path.parent() else { return false };
    if fs::create_dir_all(dir).and_then(|_| fs::set_permissions(dir, fs::Permissions::from_mode(0o700))).is_err() {
        return false;
    }
    let Ok(lock) = fs::OpenOptions::new().create(true).truncate(false).write(true).open(dir.join(STARTING_LOCK)) else {
        return false;
    };
    // SAFETY: flock on a file this process holds open; released when `lock` is dropped.
    let try_lock = || unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0;
    let mut child = None;
    if try_lock() {
        if running(&path) {
            return true; // came up between the caller's look and the lock
        }
        let Ok(exe) = std::env::current_exe() else { return false };
        let spawned = std::process::Command::new(exe)
            .arg("--serve")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .process_group(0) // Ctrl+C on this question must not stop the server
            .spawn();
        match spawned {
            Ok(started) => child = Some(started),
            Err(_) => return false,
        }
    }
    let deadline = std::time::Instant::now() + wait;
    while std::time::Instant::now() < deadline {
        if running(&path) {
            return true;
        }
        let gave_up = match child.as_mut() {
            // the server we started has exited: it will not come up
            Some(started) => matches!(started.try_wait(), Ok(Some(_))),
            // another caller was starting one and has given up
            None => try_lock() && !running(&path),
        };
        if gave_up {
            return false;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

fn same_user(stream: &UnixStream) -> bool {
    let mut cred = libc::ucred { pid: 0, uid: 0, gid: 0 };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: getsockopt writes at most `len` bytes into `cred`.
    let ok = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut cred as *mut libc::ucred).cast(),
            &mut len,
        )
    } == 0;
    // SAFETY: getuid cannot fail.
    ok && cred.uid == unsafe { libc::getuid() }
}

static TERMINATED: AtomicBool = AtomicBool::new(false);

extern "C" fn on_term(_: libc::c_int) {
    TERMINATED.store(true, Ordering::SeqCst);
}

fn install_term_handler() {
    // SAFETY: the handler only stores to an atomic. No SA_RESTART, so poll() returns
    // EINTR and the loop below notices.
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = on_term as extern "C" fn(libc::c_int) as *const () as usize;
        libc::sigemptyset(&mut action.sa_mask);
        libc::sigaction(libc::SIGTERM, &action, std::ptr::null_mut());
        libc::sigaction(libc::SIGINT, &action, std::ptr::null_mut());
    }
}

/// Wait for a connection; false on the idle timeout or a termination signal.
fn wait_for_connection(listener: &UnixListener, idle_ms: i32) -> bool {
    let mut fds = libc::pollfd { fd: listener.as_raw_fd(), events: libc::POLLIN, revents: 0 };
    loop {
        if TERMINATED.load(Ordering::SeqCst) {
            return false;
        }
        // SAFETY: one valid pollfd.
        let ready = unsafe { libc::poll(&mut fds, 1, idle_ms) };
        match ready {
            0 => return false,
            n if n > 0 => return true,
            _ if std::io::Error::last_os_error().kind() == ErrorKind::Interrupted => continue,
            _ => return false,
        }
    }
}

/// Removes the socket however the server ends.
struct Cleanup(PathBuf);

impl Drop for Cleanup {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

/// Answer requests one at a time until idle for `idle` minutes (0: never), a stop
/// request or SIGTERM. `respond` turns an "ask" request into {stdout, stderr, code}.
pub fn serve(respond: &mut dyn FnMut(&Value) -> Value, path: Option<&Path>, idle: f64, ready: &dyn Fn(&Path)) -> i32 {
    let path = path.map(Path::to_path_buf).unwrap_or_else(socket_path);
    if request(json!({"type": "ping"}), Some(&path), CONNECT_TIMEOUT).is_some() {
        eprintln!("lxask: a server is already running ({})", path.display());
        return 1;
    }
    let parent = path.parent().unwrap_or(Path::new("/"));
    if let Err(error) = fs::create_dir_all(parent).and_then(|_| fs::set_permissions(parent, fs::Permissions::from_mode(0o700))) {
        eprintln!("lxask: cannot prepare {}: {error}", parent.display());
        return 1;
    }
    let _ = fs::remove_file(&path);
    // SAFETY: umask only changes this process's file creation mask.
    let old_mask = unsafe { libc::umask(0o177) };
    let bound = UnixListener::bind(&path);
    unsafe { libc::umask(old_mask) };
    let listener = match bound {
        Ok(listener) => listener,
        Err(error) => {
            eprintln!("lxask: cannot listen on {}: {error}", path.display());
            return 1;
        }
    };
    let _cleanup = Cleanup(path.clone());
    install_term_handler();
    ready(&path);
    let idle_ms = if idle > 0.0 { (idle * 60_000.0).min(i32::MAX as f64) as i32 } else { -1 };
    while wait_for_connection(&listener, idle_ms) {
        let Ok((mut stream, _)) = listener.accept() else { continue };
        if !handle(&mut stream, respond) {
            break;
        }
    }
    0
}

fn error_reply(text: String, code: i64) -> Value {
    json!({"stdout": "", "stderr": text, "code": code})
}

/// Answer one connection; false when it asked the server to stop.
fn handle(stream: &mut UnixStream, respond: &mut dyn FnMut(&Value) -> Value) -> bool {
    if !same_user(stream) {
        return true;
    }
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    let message = match receive(stream, Some(MAX_REQUEST)) {
        Ok(message) => message,
        Err(_) => {
            let _ = send(stream, &error_reply("lxask: bad request".into(), 2));
            return true;
        }
    };
    let kind = message.get("type").and_then(Value::as_str).unwrap_or("");
    let (reply, keep_going) = match kind {
        "stop" => (json!({"stopped": true}), false),
        "ping" => (json!({"version": VERSION}), true),
        _ if message.get("version").and_then(Value::as_str) != Some(VERSION) => (json!({"stale": VERSION}), true),
        _ => {
            // one bad question must not take the server down
            let reply = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| respond(&message)))
                .unwrap_or_else(|_| error_reply("lxask: the server failed on this question".into(), 1));
            (reply, true)
        }
    };
    let _ = stream.set_write_timeout(Some(ANSWER_TIMEOUT));
    let _ = send(stream, &reply);
    keep_going
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::FileTypeExt;
    use std::sync::mpsc;

    fn fake_respond(message: &Value) -> Value {
        let question = message["question"].as_str().unwrap_or("");
        if question == "fail" {
            panic!("boom");
        }
        json!({"stdout": format!("$ echo {question}"), "stderr": "", "code": 0})
    }

    /// A server on a fresh socket in a thread; returns its path once it is ready.
    fn start(idle: f64) -> (PathBuf, std::thread::JoinHandle<i32>) {
        let dir = std::env::temp_dir().join(format!("lxask-test-{}-{}", std::process::id(), rand_suffix()));
        let path = dir.join("sub").join(SOCKET);
        let (tx, rx) = mpsc::channel();
        let serve_path = path.clone();
        let handle = std::thread::spawn(move || {
            serve(&mut fake_respond, Some(&serve_path), idle, &|p: &Path| tx.send(p.to_path_buf()).unwrap())
        });
        let ready = rx.recv_timeout(Duration::from_secs(10)).expect("server ready");
        (ready, handle)
    }

    /// serve() sets the process umask while it binds; tests run in threads of one
    /// process, so one that creates files meanwhile would get them without the x bit.
    fn serial() -> std::sync::MutexGuard<'static, ()> {
        static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn rand_suffix() -> u128 {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    }

    fn ask(path: &Path, question: &str) -> Option<Value> {
        request(json!({"type": "ask", "question": question}), Some(path), Duration::from_secs(10))
    }

    #[test]
    fn round_trip_with_private_permissions() {
        let _serial = serial();
        let (path, handle) = start(0.0);
        assert_eq!(ask(&path, "hi").unwrap()["stdout"], "$ echo hi");
        let socket_mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        let dir_mode = fs::metadata(path.parent().unwrap()).unwrap().permissions().mode() & 0o777;
        assert!(fs::metadata(&path).unwrap().file_type().is_socket());
        assert_eq!((socket_mode, dir_mode), (0o600, 0o700));
        // a failing question does not stop the server
        assert_eq!(ask(&path, "fail").unwrap()["code"], 1);
        assert_eq!(ask(&path, "again").unwrap()["stdout"], "$ echo again");
        assert_eq!(request(json!({"type": "stop"}), Some(&path), Duration::from_secs(10)).unwrap()["stopped"], true);
        assert_eq!(handle.join().unwrap(), 0);
        assert!(!path.exists());
    }

    #[test]
    fn oversized_and_stale_requests_are_refused() {
        let _serial = serial();
        let (path, handle) = start(0.0);
        let reply = ask(&path, &"x".repeat(MAX_REQUEST + 10)).unwrap();
        assert_eq!(reply["stderr"], "lxask: bad request");
        let mut stream = UnixStream::connect(&path).unwrap();
        send(&mut stream, &json!({"type": "ask", "question": "q", "version": "0.0.1"})).unwrap();
        assert_eq!(receive(&mut stream, None).unwrap()["stale"], VERSION);
        request(json!({"type": "stop"}), Some(&path), Duration::from_secs(10));
        handle.join().unwrap();
    }

    #[test]
    fn no_server_and_a_stale_socket() {
        let _serial = serial();
        let dir = std::env::temp_dir().join(format!("lxask-test-stale-{}", rand_suffix()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join(SOCKET);
        assert!(ask(&path, "q").is_none());
        drop(UnixListener::bind(&path).unwrap()); // left behind, nobody listening
        assert!(ask(&path, "q").is_none());
        assert!(!path.exists());
    }

    #[test]
    fn stops_when_idle() {
        let _serial = serial();
        let (path, handle) = start(0.002); // about 120 ms
        assert_eq!(handle.join().unwrap(), 0);
        assert!(!path.exists());
    }
}
