//! Remembering answers and the ones the user says were wrong.
//!
//! Every answer is kept in $LXASK_HOME/last.json. `lxask --bad [what was wrong]` appends
//! it, with the note, to $LXASK_HOME/feedback.jsonl, which scripts/feedback_cases.py
//! turns into draft regression cases. Nothing leaves the machine by itself: `lxask
//! --share [N]` prints the last N reports and a link to a GitHub issue prefilled with
//! them, which the user may open and submit.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use lxask_core::answer::Step;

use crate::config;

pub const LAST: &str = "last.json";
pub const FEEDBACK: &str = "feedback.jsonl";
/// where `lxask --share` links to
pub const NEW_ISSUE: &str = "https://github.com/arxhr007/lxask/issues/new";
/// at most this many reports in one link
pub const SHARE_MAX: usize = 10;
/// GitHub refuses longer URLs; reports past this are left out
const URL_MAX: usize = 7500;

/// `time.strftime("%Y-%m-%dT%H:%M:%S")`: local time.
pub fn local_time() -> String {
    // SAFETY: time and localtime_r only write to the struct passed in.
    unsafe {
        let now = libc::time(std::ptr::null_mut());
        let mut tm: libc::tm = std::mem::zeroed();
        libc::localtime_r(&now, &mut tm);
        format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
            tm.tm_year + 1900,
            tm.tm_mon + 1,
            tm.tm_mday,
            tm.tm_hour,
            tm.tm_min,
            tm.tm_sec
        )
    }
}

fn file_name(model: &Path) -> String {
    let resolved = fs::canonicalize(model).unwrap_or_else(|_| model.to_path_buf());
    resolved.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// Keep the answer just given, so `lxask --bad` can report it. Best effort: an
/// unwritable home costs the feature, never the answer.
pub fn remember(question: &str, steps: &[Step], model: &Path, home: Option<&Path>) {
    let record = json!({
        "time": local_time(),
        "question": question,
        "steps": steps.iter().map(|s| json!({"task": s.task, "command": s.command})).collect::<Vec<_>>(),
        "model": file_name(model),
        "version": env!("CARGO_PKG_VERSION"),
    });
    let path = home.map(Path::to_path_buf).unwrap_or_else(config::data_home).join(LAST);
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let _ = fs::write(&path, format!("{record}\n"));
}

/// Append the last answer, with `note`, to the feedback file; the message to print.
pub fn report(note: &str, home: Option<&Path>) -> String {
    let home: PathBuf = home.map(Path::to_path_buf).unwrap_or_else(config::data_home);
    let Ok(text) = fs::read_to_string(home.join(LAST)) else {
        return "lxask: there is no answer to report yet; ask something first".to_string();
    };
    let Ok(Value::Object(mut record)) = serde_json::from_str::<Value>(&text) else {
        return format!("lxask: {} cannot be read", home.join(LAST).display());
    };
    record.insert("note".into(), Value::from(note));
    record.insert("reported".into(), Value::from(local_time()));
    let path = home.join(FEEDBACK);
    let written = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .and_then(|mut out| writeln!(out, "{}", Value::Object(record.clone())));
    if let Err(error) = written {
        return format!("lxask: cannot write {}: {error}", path.display());
    }
    let count = fs::read_to_string(&path).map(|t| t.lines().filter(|l| !l.trim().is_empty()).count()).unwrap_or(0);
    let commands: Vec<String> = record
        .get("steps")
        .and_then(Value::as_array)
        .map(|steps| {
            steps
                .iter()
                .filter_map(|s| s.get("command").and_then(Value::as_str))
                .filter(|c| !c.is_empty())
                .map(String::from)
                .collect()
        })
        .unwrap_or_default();
    let commands = if commands.is_empty() { "(no command)".to_string() } else { commands.join("; ") };
    let question = record.get("question").and_then(Value::as_str).unwrap_or("");
    format!(
        "saved as a bad answer ({count} so far, in {}):\n  {question}\n  $ {commands}\n\
         to send it to lxask's developers: lxask --share",
        path.display()
    )
}

fn text<'a>(record: &'a Value, key: &str) -> &'a str {
    record.get(key).and_then(Value::as_str).unwrap_or("")
}

/// One report as it goes into an issue: the question, each command, the note.
fn markdown(record: &Value) -> String {
    let mut out = format!("**Question:** {}\n\n", text(record, "question").trim());
    let steps = record.get("steps").and_then(Value::as_array).cloned().unwrap_or_default();
    out.push_str("**lxask answered:**\n\n```\n");
    for step in &steps {
        let command = text(step, "command");
        if steps.len() > 1 {
            out.push_str(&format!("# {}\n", text(step, "task")));
        }
        out.push_str(&format!("{}\n", if command.is_empty() { "(no command)" } else { command }));
    }
    out.push_str("```\n\n");
    let note = text(record, "note").trim();
    out.push_str(&format!("**What was wrong:** {}\n\n", if note.is_empty() { "(no note)" } else { note }));
    out.push_str(&format!("<sub>lxask {}, {}</sub>\n", text(record, "version"), text(record, "model")));
    out
}

/// `encodeURIComponent`: everything but letters, digits and `-_.!~*'()` as %XX of UTF-8.
fn url_encode(text: &str) -> String {
    let mut out = String::with_capacity(text.len() * 3);
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'!' | b'~' | b'*' | b'\'' | b'(' | b')' => {
                out.push(byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

fn issue_url(title: &str, body: &str) -> String {
    format!("{NEW_ISSUE}?template=bad-answer.md&title={}&body={}", url_encode(title), url_encode(body))
}

/// The last `n` reports and a link to a new GitHub issue holding them, for the user to
/// read and, if they choose, open and submit. Nothing is sent from here.
pub fn share(n: usize, home: Option<&Path>) -> String {
    let home: PathBuf = home.map(Path::to_path_buf).unwrap_or_else(config::data_home);
    let path = home.join(FEEDBACK);
    let records: Vec<Value> = fs::read_to_string(&path)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect();
    if records.is_empty() {
        return "lxask: no bad answers reported yet; after a wrong answer, run lxask --bad [what was wrong]".to_string();
    }
    let wanted = n.clamp(1, SHARE_MAX).min(records.len());
    // newest last, as reported; the oldest of the wanted ones go first if the link is too long
    let mut chosen: Vec<&Value> = records[records.len() - wanted..].iter().collect();
    let (title, body) = loop {
        let title = match chosen.as_slice() {
            [one] => format!("Bad answer: {}", py_prefix(text(one, "question").trim(), 70)),
            many => format!("{} bad answers", many.len()),
        };
        let body = chosen.iter().map(|r| markdown(r)).collect::<Vec<_>>().join("\n---\n\n");
        if issue_url(&title, &body).len() <= URL_MAX || chosen.len() == 1 {
            break (title, body);
        }
        chosen.remove(0);
    };
    let mut url = issue_url(&title, &body);
    let mut body = body;
    if url.len() > URL_MAX {
        // a single report too long for a link: shorten its text, the rest stays readable here
        let cut: String = body.chars().take(URL_MAX / 4).collect();
        body = format!("{cut}\n\n(cut short to fit in a link)");
        url = issue_url(&title, &body);
    }
    let left_out = wanted - chosen.len();
    let mut out = String::from(
        "This is what the link below would send. Nothing is sent unless you open it and submit\n\
         the issue on GitHub (a GitHub account is needed). Remove anything private first: the\n\
         issue is public.\n\n",
    );
    out.push_str(&format!("Title: {title}\n\n"));
    out.extend(body.lines().map(|line| format!("  {line}\n")));
    if left_out > 0 {
        out.push_str(&format!("\n({left_out} older report(s) left out to keep the link short: share them with a smaller N)\n"));
    }
    out.push_str(&format!("\nOpen to report:\n{url}\n"));
    out
}

/// At most `n` characters, with `...` if cut.
fn py_prefix(text: &str, n: usize) -> String {
    if text.chars().count() <= n {
        text.to_string()
    } else {
        format!("{}...", text.chars().take(n).collect::<String>())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn home() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("lxask-share-{}-{}", std::process::id(), rand_suffix()));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn rand_suffix() -> u128 {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    }

    fn reported(home: &Path, question: &str, command: &str, note: &str) {
        let step = Step { task: question.into(), command: command.into(), ..Step::default() };
        remember(question, &[step], Path::new("lxask-round4-Q4_K_M.gguf"), Some(home));
        report(note, Some(home));
    }

    #[test]
    fn encodes_like_encode_uri_component() {
        assert_eq!(url_encode("a b&c=d/é"), "a%20b%26c%3Dd%2F%C3%A9");
        assert_eq!(url_encode("ls -d */"), "ls%20-d%20*%2F");
    }

    #[test]
    fn nothing_to_share_says_how_to_report() {
        assert!(share(1, Some(&home())).contains("lxask --bad"));
    }

    #[test]
    fn the_link_holds_the_last_report() {
        let home = home();
        reported(&home, "list directories only", "ls -l", "should be ls -d */");
        let out = share(1, Some(&home));
        assert!(out.contains("Title: Bad answer: list directories only"));
        assert!(out.contains("ls -l") && out.contains("should be ls -d */"));
        let url = out.lines().last().unwrap();
        assert!(url.starts_with("https://github.com/arxhr007/lxask/issues/new?template=bad-answer.md&title=Bad%20answer"));
        assert!(url.contains("should%20be%20ls%20-d%20*%2F"));
        let _ = fs::remove_dir_all(home);
    }

    #[test]
    fn several_reports_fit_one_link_or_the_oldest_are_left_out() {
        let home = home();
        for i in 0..3 {
            reported(&home, &format!("question {i}"), &format!("cmd{i}"), "");
        }
        let out = share(2, Some(&home));
        assert!(out.contains("Title: 2 bad answers") && out.contains("question 2") && !out.contains("question 0"));
        let long = "x".repeat(3000);
        for i in 0..4 {
            reported(&home, &format!("long {i} {long}"), "cmd", "");
        }
        let out = share(4, Some(&home));
        assert!(out.lines().last().unwrap().len() <= URL_MAX);
        assert!(out.contains("left out to keep the link short"));
        let _ = fs::remove_dir_all(home);
    }
}
