//! The prompt formats, shared by inference, evaluation and fine-tuning.
//!
//! The model does two jobs, told apart by the system prompt: SPLIT turns a request
//! into the separate tasks in it, one per line; ANSWER turns one task plus reference
//! notes into one command line in a bash block.
//!
//! It writes no prose. What a command does is described by `explain`, from the
//! documentation.

use crate::py::{self, char_len};
use crate::re;
use crate::py::LazyRe;

pub const ANSWER_SYSTEM: &str = "You are a Linux command-line assistant. Reply with one command line that does what \
the user asks, inside a ```bash code block, and nothing else. The reference notes \
come from the documentation: use them when they are relevant, ignore them when \
they are not. Use the file names, paths and values the user gave; where none are \
given, use a plain placeholder such as path/to/file.";

pub const SPLIT_SYSTEM: &str = "You split a request for help on Linux into the separate things the user wants done. \
Write one task per line, starting each line with \"- \". Each task is a short \
instruction that makes sense on its own, without referring to the other tasks, in \
the order they should be done. Leave out anything a command cannot do. If the \
request asks for one thing, write one line.";

pub const MAX_CONTEXT_CHARS: usize = 1800;
pub const MAX_TASKS: usize = 6;

/// A documentation entry found for a question.
#[derive(Clone, Debug, PartialEq)]
pub struct Doc {
    pub cmd: String,
    pub title: String,
    pub body: String,
    pub source: String,
    /// lower is a better match (bm25, or negated cosine similarity)
    pub score: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Message {
    pub role: &'static str,
    pub content: String,
}

pub fn format_doc(doc: &Doc) -> String {
    if doc.source == "tldr" {
        format!("- {}: {}: `{}`", doc.cmd, doc.title, doc.body)
    } else {
        format!("- {}: {}", doc.cmd, doc.title)
    }
}

pub fn format_context(docs: &[Doc], max_chars: usize) -> String {
    let mut lines = Vec::new();
    let mut used = 0;
    for doc in docs {
        let line = format_doc(doc);
        let length = char_len(&line);
        if used + length > max_chars {
            break;
        }
        used += length + 1;
        lines.push(line);
    }
    lines.join("\n")
}

/// The ANSWER prompt for one task.
pub fn build_messages(task: &str, docs: &[Doc]) -> Vec<Message> {
    let context = format_context(docs, MAX_CONTEXT_CHARS);
    let mut user = format!("Question: {}", py::strip(task));
    if !context.is_empty() {
        user = format!("Reference notes:\n{context}\n\n{user}");
    }
    vec![
        Message { role: "system", content: ANSWER_SYSTEM.to_string() },
        Message { role: "user", content: user },
    ]
}

/// The reply the model is trained to give; `parse_answer` reads it back.
pub fn format_answer(command: &str) -> String {
    format!("```bash\n{}\n```", py::strip(command))
}

static FENCE: LazyRe = re!(r"```[\w-]*[ \t]*\n(.*?)(?:```|\Z)", "s");

/// The command in a model reply, or "" if it gave none. A block left open (cut off by
/// the token limit) still counts.
pub fn parse_answer(text: &str) -> String {
    match FENCE.search(text) {
        Some(caps) => py::strip_chars(py::group_or_empty(&caps, 1), "\n").to_string(),
        None => String::new(),
    }
}

/// The SPLIT prompt for a request.
pub fn build_split_messages(request: &str) -> Vec<Message> {
    vec![
        Message { role: "system", content: SPLIT_SYSTEM.to_string() },
        Message { role: "user", content: py::strip(request).to_string() },
    ]
}

/// The reply the model is trained to give; `parse_tasks` reads it back.
pub fn format_tasks<S: AsRef<str>>(tasks: &[S]) -> String {
    tasks.iter().map(|task| format!("- {}", py::strip(task.as_ref()))).collect::<Vec<_>>().join("\n")
}

static BULLET: LazyRe = re!(r"^\s*(?:[-*]|\d+[.)])\s+");

/// The tasks in a model reply: one per bulleted or numbered line, duplicates dropped.
pub fn parse_tasks(text: &str, limit: usize) -> Vec<String> {
    let mut tasks: Vec<String> = Vec::new();
    for line in py::splitlines(text) {
        if BULLET.match_(line).is_none() {
            continue;
        }
        let task = py::strip(&BULLET.sub(line, "")).to_string();
        if !task.is_empty() && !tasks.contains(&task) {
            tasks.push(task);
        }
    }
    tasks.truncate(limit);
    tasks
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn answers_round_trip() {
        assert_eq!(parse_answer(&format_answer(" ls -la ")), "ls -la");
        assert_eq!(parse_answer("```bash\nfind . -name x"), "find . -name x");
        assert_eq!(parse_answer("no block"), "");
        assert_eq!(parse_answer("```\nls\n```"), "ls");
    }

    #[test]
    fn tasks_are_parsed_from_bullets() {
        assert_eq!(parse_tasks("- a\n* b\n1. c\n2) d\nprose\n- a", MAX_TASKS), vec!["a", "b", "c", "d"]);
        assert_eq!(parse_tasks("- a\n- b\n- c", 2), vec!["a", "b"]);
    }
}
