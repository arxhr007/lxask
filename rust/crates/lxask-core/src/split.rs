//! Turn a request into the separate tasks in it.
//!
//! A long request retrieves badly as a whole ("server is slow, find X, check Y, stop Z"
//! brings back notes about disk usage) and each part retrieves well on its own, so a
//! multi-part request is answered part by part. The model does the splitting; short
//! single-clause questions skip that call.

use crate::prompt::{build_split_messages, parse_tasks, Message, MAX_TASKS};
use crate::py::{self, LazyRe};
use crate::re;

/// Below this many words, with nothing joining clauses, a question is one task.
pub const SINGLE_TASK_WORDS: usize = 12;
static JOINS: LazyRe = re!(r"[,;]|\.\s+\S|\b(and|then|also|after that|afterwards|plus)\b", "i");

pub fn needs_split(question: &str) -> bool {
    py::split_ws(question).len() > SINGLE_TASK_WORDS || JOINS.is_match(question)
}

/// The tasks in `question`, in order. Falls back to the question itself whenever the
/// split is skipped or comes back empty, so there is always something to answer.
pub fn tasks_for(question: &str, chat: &mut dyn FnMut(&[Message]) -> Option<String>) -> Vec<String> {
    let question = py::strip(question);
    if !needs_split(question) {
        return vec![question.to_string()];
    }
    let reply = chat(&build_split_messages(question)).unwrap_or_default();
    let tasks = parse_tasks(&reply, MAX_TASKS);
    if tasks.is_empty() {
        vec![question.to_string()]
    } else {
        tasks
    }
}
