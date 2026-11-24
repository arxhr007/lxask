//! A newer release, said once a day — in the background, never in the answer's way.
//!
//! Answers stay offline: a check is one curl on a spare thread or a detached copy of
//! this program (`lxask --update-check`), whose result the next run reads from
//! `$LXASK_HOME/latest`. No network, a failed check or an old binary only means the
//! note does not appear; `update_check = off` (or LXASK_UPDATE_CHECK=off) stops it
//! entirely. `lxask --upgrade` is what the note suggests: it runs the newest
//! release's install.sh, which keeps every file a checksum says is unchanged.

use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;

use crate::config;
use crate::server;

/// Seconds between checks: at most one a day however often lxask runs.
const CHECK_EVERY_SECS: u64 = 24 * 3600;
/// the file a check's outcome is kept in, under $LXASK_HOME
const LATEST_FILENAME: &str = "latest";
/// asked for the newest release's tag (a JSON object; `.tag_name` is "v1.5.6")
const RELEASES_URL: &str = "https://api.github.com/repos/arxhr007/lxask/releases/latest";
/// the newest release's installer, which --upgrade downloads and runs
const INSTALL_URL: &str = "https://github.com/arxhr007/lxask/releases/latest/download/install.sh";

fn cache_path() -> PathBuf {
    config::data_home().join(LATEST_FILENAME)
}

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// The last check's outcome: the newest tag it saw ("" when it could not look) and
/// when it ran.
fn cache() -> Option<(String, u64)> {
    let text = std::fs::read_to_string(cache_path()).ok()?;
    let json: Value = serde_json::from_str(&text).ok()?;
    let tag = json.get("tag").and_then(Value::as_str).unwrap_or("").to_string();
    let checked_at = json.get("checked_at").and_then(Value::as_u64).unwrap_or(0);
    Some((tag, checked_at))
}

/// A check ran long enough ago (or never) to run another.
fn stale() -> bool {
    cache().map_or(true, |(_, at)| now().saturating_sub(at) >= CHECK_EVERY_SECS)
}

/// "v1.5.7" is ahead of the running "1.5.6"? Compared piecewise ("1.6.0" > "1.5.9"),
/// extra or missing parts counting as 0.
pub fn newer(latest: &str, current: &str) -> bool {
    let parts = |v: &str| -> Vec<u64> {
        v.split(|c: char| !c.is_ascii_digit()).filter(|s| !s.is_empty()).filter_map(|s| s.parse().ok()).collect()
    };
    let (latest, current) = (parts(latest), parts(current));
    let len = latest.len().max(current.len());
    let padded = |v: &[u64]| (0..len).map(|i| v.get(i).copied().unwrap_or(0)).collect::<Vec<u64>>();
    !latest.is_empty() && padded(&latest) > padded(&current)
}

/// The one-line note for after the answer, when a check has seen a newer release.
pub fn notice() -> Option<String> {
    if !config::update_check() {
        return None;
    }
    let (tag, _) = cache()?;
    newer(&tag, server::VERSION)
        .then(|| format!("lxask: a newer release is out: {tag} (installed: {}); 'lxask --upgrade' installs it", server::VERSION))
}

/// Check once: ask GitHub for the latest release's tag and write it down. The time
/// is stamped however it went — offline or a GitHub hiccup tries again tomorrow,
/// not on the next question.
pub fn run_and_write() {
    let tag = latest_tag().unwrap_or_default();
    if std::fs::create_dir_all(config::data_home()).is_err() {
        return;
    }
    let tmp = cache_path().with_extension("tmp");
    if std::fs::write(&tmp, serde_json::json!({"tag": tag, "checked_at": now()}).to_string()).is_ok() {
        let _ = std::fs::rename(&tmp, cache_path());
    }
}

/// The newest release's tag ("v1.5.6"), asked with the curl install.sh already needs;
/// None when it cannot be had in time.
fn latest_tag() -> Option<String> {
    let out = Command::new("curl")
        .args(["-fsSL", "--max-time", "8", RELEASES_URL])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let json: Value = serde_json::from_slice(&out.stdout).ok()?;
    json.get("tag_name").and_then(Value::as_str).map(String::from)
}

/// A question run calls this when a check is due: a detached copy of this program
/// runs it (`lxask --update-check`), so the answer never waits on the network — the
/// note, if any, appears on the next run.
pub fn maybe_spawn_detached() {
    if !config::update_check() || !stale() {
        return;
    }
    let Ok(exe) = std::env::current_exe() else { return };
    let _ = Command::new(exe)
        .arg("--update-check")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
}

/// The server calls this instead: it stays around, so a thread does the check in
/// place of a detached program (its child would else linger as a zombie for days).
pub fn maybe_spawn_thread() {
    if !config::update_check() || !stale() {
        return;
    }
    std::thread::spawn(run_and_write);
}

/// `lxask --upgrade`: the newest release's install.sh, downloaded and run in the
/// open — it picks the binary for this machine, checks every file against
/// SHA256SUMS, keeps unchanged ones and stops a server the old version started.
/// Its exit status becomes this program's.
pub fn upgrade() -> u8 {
    let script = std::env::temp_dir().join(format!("lxask-install-{}.sh", std::process::id()));
    eprintln!("lxask: downloading the newest release's install.sh");
    match Command::new("curl")
        .args(["-fsSL", "--max-time", "30", "-o"])
        .arg(&script)
        .arg(INSTALL_URL)
        .stdin(Stdio::null())
        .status()
    {
        Err(error) => {
            eprintln!("lxask: cannot run curl: {error}");
            return 1;
        }
        Ok(status) if !status.success() => {
            let _ = std::fs::remove_file(&script);
            eprintln!("lxask: could not download install.sh ({status})");
            return 1;
        }
        _ => {}
    }
    let ran = Command::new("sh").arg(&script).status();
    let _ = std::fs::remove_file(&script);
    match ran {
        Ok(status) => status.code().unwrap_or(1) as u8,
        Err(error) => {
            eprintln!("lxask: cannot run sh: {error}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_compare_piecewise() {
        assert!(newer("v1.5.7", "1.5.6"));
        assert!(newer("1.6.0", "1.5.9"));
        assert!(newer("v2.0.0", "1.9.9"));
        assert!(!newer("v1.5.6", "1.5.6"));
        assert!(!newer("v1.5.5", "1.5.6"));
        assert!(!newer("", "1.5.6"));
        // a tag that is not a number does not count as newer
        assert!(!newer("latest", "1.5.6"));
        // a longer tag counts its extra parts
        assert!(newer("v1.5.6.1", "1.5.6"));
        assert!(!newer("v1.5", "1.5.6"));
    }
}
