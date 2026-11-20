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
//!
//! With `--listen HOST:PORT` it also answers over TCP, so other machines and containers
//! can ask it with only the program installed (LXASK_SERVER=host:port). Every request
//! that comes over the network must carry the server's token ($LXASK_HOME/token, made
//! on first use); a network client can ask and explain, not stop the server.

use std::fs;
use std::io::{ErrorKind, Read, Write};
use std::net::{TcpListener, TcpStream, ToSocketAddrs};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
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

fn send(stream: &mut impl Write, message: &Value) -> std::io::Result<()> {
    let mut line = serde_json::to_vec(message).map_err(std::io::Error::other)?;
    line.push(b'\n');
    stream.write_all(&line)
}

fn receive(stream: &mut impl Read, limit: Option<usize>) -> std::io::Result<Value> {
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

/// The reply of the server listening at `address` (host:port) to `message`, sent with
/// `token`; Err says why there is none, for the user.
pub fn request_remote(address: &str, token: &str, message: Value, timeout: Duration) -> Result<Value, String> {
    let targets = address.to_socket_addrs().map_err(|e| format!("cannot resolve {address}: {e}"))?;
    let mut last = format!("{address} resolves to no address");
    let mut stream = None;
    for target in targets {
        match TcpStream::connect_timeout(&target, CONNECT_TIMEOUT) {
            Ok(connected) => {
                stream = Some(connected);
                break;
            }
            Err(e) => last = format!("cannot connect to {address}: {e}"),
        }
    }
    let mut stream = stream.ok_or(last)?;
    let _ = stream.set_write_timeout(Some(CONNECT_TIMEOUT));
    let _ = stream.set_read_timeout(Some(timeout));
    let mut message = match message {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    message.insert("version".into(), Value::from(VERSION));
    message.insert("token".into(), Value::from(token));
    send(&mut stream, &Value::Object(message)).map_err(|e| format!("cannot send to {address}: {e}"))?;
    receive(&mut stream, None).map_err(|e| format!("no answer from {address}: {e}"))
}

/// The token for `--listen` and where it is: the configured one, else a new random one
/// saved to $LXASK_HOME/token (readable by this user only). Err when neither can be had.
pub fn ensure_token() -> Result<(String, String), String> {
    if let Some(found) = config::token_with_source() {
        return Ok(found);
    }
    let mut bytes = [0u8; 24];
    fs::File::open("/dev/urandom")
        .and_then(|mut random| random.read_exact(&mut bytes))
        .map_err(|e| format!("cannot read /dev/urandom: {e}"))?;
    let token: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    let path = config::token_path();
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    }
    fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(&path)
        .and_then(|mut file| file.write_all(format!("{token}\n").as_bytes()))
        .map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    Ok((token, path.display().to_string()))
}

/// Equal without saying, through the time it takes, how much of `given` was right.
fn same_secret(given: &str, token: &str) -> bool {
    given.len() == token.len() && given.bytes().zip(token.bytes()).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0
}

/// The network side of a server: where it listens, and the token every request needs.
pub struct Remote {
    pub listener: TcpListener,
    pub token: String,
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

/// Which listener has a connection waiting.
enum Ready {
    Local,
    Network,
}

/// Wait for a connection on the socket, or on the network listener if there is one;
/// None on the idle timeout or a termination signal.
fn wait_for_connection(listener: &UnixListener, network: Option<&TcpListener>, idle_ms: i32) -> Option<Ready> {
    let mut fds = [
        libc::pollfd { fd: listener.as_raw_fd(), events: libc::POLLIN, revents: 0 },
        libc::pollfd { fd: network.map_or(-1, |n| n.as_raw_fd()), events: libc::POLLIN, revents: 0 },
    ];
    let count: libc::nfds_t = if network.is_some() { 2 } else { 1 };
    loop {
        if TERMINATED.load(Ordering::SeqCst) {
            return None;
        }
        // SAFETY: `count` valid pollfds.
        let ready = unsafe { libc::poll(fds.as_mut_ptr(), count, idle_ms) };
        match ready {
            0 => return None,
            n if n > 0 => {
                return Some(if fds[0].revents & libc::POLLIN != 0 { Ready::Local } else { Ready::Network });
            }
            _ if std::io::Error::last_os_error().kind() == ErrorKind::Interrupted => continue,
            _ => return None,
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
/// request or SIGTERM. `respond` turns an "ask" or "explain" request into {stdout,
/// stderr, code}. With `remote`, requests that come over the network are answered too.
pub fn serve(
    respond: &mut dyn FnMut(&Value) -> Value,
    path: Option<&Path>,
    remote: Option<&Remote>,
    idle: f64,
    ready: &dyn Fn(&Path),
) -> i32 {
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
    while let Some(ready) = wait_for_connection(&listener, remote.map(|r| &r.listener), idle_ms) {
        let keep_going = match (ready, remote) {
            (Ready::Network, Some(remote)) => match remote.listener.accept() {
                Ok((mut stream, _)) => {
                    let _ = stream.set_read_timeout(Some(REQUEST_TIMEOUT));
                    let _ = stream.set_write_timeout(Some(ANSWER_TIMEOUT));
                    handle(&mut stream, respond, Some(&remote.token))
                }
                Err(_) => true,
            },
            _ => match listener.accept() {
                Ok((mut stream, _)) if same_user(&stream) => {
                    let _ = stream.set_read_timeout(Some(REQUEST_TIMEOUT));
                    let _ = stream.set_write_timeout(Some(ANSWER_TIMEOUT));
                    handle(&mut stream, respond, None)
                }
                _ => true,
            },
        };
        if !keep_going {
            break;
        }
    }
    0
}

/// how long a client has to send its request once connected
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);

fn error_reply(text: String, code: i64) -> Value {
    json!({"stdout": "", "stderr": text, "code": code})
}

/// Answer one connection; false when it asked the server to stop. `token` is set for a
/// connection from the network: the request must carry it, and cannot stop the server.
fn handle(stream: &mut (impl Read + Write), respond: &mut dyn FnMut(&Value) -> Value, token: Option<&str>) -> bool {
    let mut message = match receive(stream, Some(MAX_REQUEST)) {
        Ok(message) => message,
        Err(_) => {
            let _ = send(stream, &error_reply("lxask: bad request".into(), 2));
            return true;
        }
    };
    if let Some(token) = token {
        let given = message.as_object_mut().and_then(|m| m.remove("token"));
        if !given.as_ref().and_then(Value::as_str).is_some_and(|given| same_secret(given, token)) {
            let _ = send(stream, &error_reply("lxask: the server refused the token (LXASK_TOKEN)".into(), 2));
            return true;
        }
    }
    let kind = message.get("type").and_then(Value::as_str).unwrap_or("");
    let (reply, keep_going) = match kind {
        "stop" if token.is_some() => (error_reply("lxask: the server cannot be stopped over the network".into(), 2), true),
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
            serve(&mut fake_respond, Some(&serve_path), None, idle, &|p: &Path| tx.send(p.to_path_buf()).unwrap())
        });
        let ready = rx.recv_timeout(Duration::from_secs(10)).expect("server ready");
        (ready, handle)
    }

    /// A server that also listens on a free local TCP port; returns the socket path,
    /// the host:port and the token.
    fn start_remote() -> (PathBuf, String, String, std::thread::JoinHandle<i32>) {
        let dir = std::env::temp_dir().join(format!("lxask-test-net-{}-{}", std::process::id(), rand_suffix()));
        let path = dir.join(SOCKET);
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap().to_string();
        let token = "0123456789abcdef".to_string();
        let remote = Remote { listener, token: token.clone() };
        let (tx, rx) = mpsc::channel();
        let serve_path = path.clone();
        let handle = std::thread::spawn(move || {
            serve(&mut fake_respond, Some(&serve_path), Some(&remote), 0.0, &|p: &Path| tx.send(p.to_path_buf()).unwrap())
        });
        let ready = rx.recv_timeout(Duration::from_secs(10)).expect("server ready");
        (ready, address, token, handle)
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
    fn over_the_network_only_with_the_token_and_never_stopped() {
        let _serial = serial();
        let (path, address, token, handle) = start_remote();
        let wait = Duration::from_secs(10);
        let ask = |question: &str| json!({"type": "ask", "question": question});
        // the token answers, over the network
        assert_eq!(request_remote(&address, &token, ask("hi"), wait).unwrap()["stdout"], "$ echo hi");
        // a wrong token, or none, is refused and the server goes on
        let refused = request_remote(&address, "0123456789abcdeX", ask("hi"), wait).unwrap();
        assert_eq!((refused["code"].as_i64(), refused["stdout"].as_str()), (Some(2), Some("")));
        assert!(refused["stderr"].as_str().unwrap().contains("refused the token"));
        let mut bare = TcpStream::connect(&address).unwrap();
        send(&mut bare, &json!({"type": "ask", "question": "hi", "version": VERSION})).unwrap();
        assert_eq!(receive(&mut bare, None).unwrap()["code"], 2);
        // a network client cannot stop it
        let stop = request_remote(&address, &token, json!({"type": "stop"}), wait).unwrap();
        assert_eq!(stop["code"], 2);
        assert_eq!(request_remote(&address, &token, ask("still"), wait).unwrap()["stdout"], "$ echo still");
        // the local socket still answers, and stops it
        assert_eq!(request(json!({"type": "stop"}), Some(&path), wait).unwrap()["stopped"], true);
        assert_eq!(handle.join().unwrap(), 0);
        // and then there is nobody to ask
        assert!(request_remote(&address, &token, ask("gone"), Duration::from_secs(2)).is_err());
    }

    #[test]
    fn secrets_compare_whole() {
        assert!(same_secret("abc", "abc"));
        assert!(!same_secret("abd", "abc"));
        assert!(!same_secret("ab", "abc"));
        assert!(!same_secret("", "abc"));
    }

    #[test]
    fn stops_when_idle() {
        let _serial = serial();
        let (path, handle) = start(0.002); // about 120 ms
        assert_eq!(handle.join().unwrap(), 0);
        assert!(!path.exists());
    }
}
