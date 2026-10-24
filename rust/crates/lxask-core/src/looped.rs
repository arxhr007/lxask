//! What the model's raw reply needs before it is used.

use std::collections::HashMap;

use crate::py::{self, LazyRe};
use crate::re;

/// Reasoning models wrap their scratch work in this; it is not part of the answer.
static THINKING: LazyRe = re!(r"<think>.*?(?:</think>|\Z)", "s");

pub fn strip_thinking(text: &str) -> String {
    py::strip(&THINKING.sub(text, "")).to_string()
}

/// Whether a reply is stuck repeating itself: the same line three times, or the same
/// four words in a row four times (`-S -X -S -X -S -X ...`).
pub fn looks_looped(text: &str) -> bool {
    let mut lines: HashMap<&str, usize> = HashMap::new();
    for line in py::splitlines(text) {
        let line = py::strip(line);
        if !line.is_empty() {
            *lines.entry(line).or_default() += 1;
        }
    }
    if lines.values().any(|&n| n >= 3) {
        return true;
    }
    let words = py::split_ws(text);
    let mut runs: HashMap<&[&str], usize> = HashMap::new();
    for window in words.windows(4) {
        *runs.entry(window).or_default() += 1;
    }
    runs.values().any(|&n| n >= 4)
}
