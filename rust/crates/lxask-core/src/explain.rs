//! Describe a command from the documentation.
//!
//! No model is involved: every line comes from a tldr summary or a man page, and where
//! the documentation says nothing, nothing is printed. So an explanation can be
//! incomplete, but it cannot be made up.

use crate::py::{self, LazyRe};
use crate::re;
use crate::validate::{uses, Catalog};

pub const MAX_CHARS: usize = 110;
// A full stop followed by a space ends the sentence, except after the abbreviations man
// pages are fond of. ("1.5" and "e.g" have no space after the stop.)
static SENTENCE_END: LazyRe = re!(r"(?<!e\.g)(?<!i\.e)(?<!\betc)(?<!\bvs)(?<!\bcf)\.(?=\s|$)");

#[derive(Clone, Debug, PartialEq)]
pub struct Described {
    pub program: String,
    pub summary: String,
    /// (option, what the manual says it does)
    pub options: Vec<(String, String)>,
}

/// The first sentence, without its full stop, cut at a word if it runs long. A sentence
/// that leads into a list (`File is of type c:`) loses its colon.
pub fn brief(text: &str, limit: usize) -> String {
    let mut text = py::split_ws(text).join(" ");
    if let Some(caps) = SENTENCE_END.search(&text) {
        let end = caps.get(0).unwrap().start();
        text.truncate(end);
    }
    let mut text = py::rstrip(text.trim_end_matches(':')).to_string();
    if py::char_len(&text) > limit {
        let cut = py::prefix(&text, limit);
        let cut = cut.rsplit_once(' ').map_or(cut, |(head, _)| head);
        text = format!("{} ...", py::rstrip_chars(cut, ",;:"));
    }
    text
}

/// What each program in `command` is, and what each documented option it is given does.
pub fn explain(command: &str, catalog: &Catalog) -> Vec<Described> {
    let Ok(found) = uses(command, catalog) else {
        return Vec::new(); // unbalanced quotes: nothing to describe
    };
    let mut described: Vec<Described> = Vec::new();
    for use_ in found {
        if use_.status != "known" {
            continue;
        }
        let arguments = &use_.arguments;
        let mut options: Vec<(String, String)> = Vec::new();
        for (_, flags) in &use_.options {
            for flag in flags.iter().flatten() {
                if options.iter().any(|(known, _)| known == flag) {
                    continue;
                }
                let text = catalog.text_for(&use_.program, arguments, flag);
                if !text.is_empty() {
                    options.push((flag.clone(), brief(&text, MAX_CHARS)));
                }
            }
        }
        // `git commit`, not `git`, when the summary shown is the subcommand's.
        let name = match catalog.subcommand(&use_.program, arguments) {
            Some(sub) if !sub.is_empty() && catalog.summaries.contains_key(&format!("{} {sub}", use_.program)) => {
                format!("{} {sub}", use_.program)
            }
            _ => use_.program.clone(),
        };
        let entry = Described { program: name, summary: brief(&catalog.summary_for(&use_.program, arguments), MAX_CHARS), options };
        if (!entry.summary.is_empty() || !entry.options.is_empty()) && !described.contains(&entry) {
            described.push(entry);
        }
    }
    described
}

/// Lines for the terminal: each program with its summary, its options beneath it, in
/// columns wide enough for the longest name.
pub fn render(described: &[Described]) -> Vec<String> {
    if described.is_empty() {
        return Vec::new();
    }
    let name_width = described.iter().map(|e| py::char_len(&e.program)).max().unwrap() + 2;
    let option_width =
        described.iter().flat_map(|e| e.options.iter().map(|(o, _)| py::char_len(o))).max().unwrap_or(0) + 2;
    let mut lines = Vec::new();
    for entry in described {
        lines.push(py::rstrip(&format!("{:<name_width$}{}", entry.program, entry.summary)).to_string());
        for (option, text) in &entry.options {
            lines.push(format!("  {option:<option_width$}{text}"));
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn brief_keeps_the_first_sentence() {
        assert_eq!(brief("Select all processes. Identical to -A.", MAX_CHARS), "Select all processes");
        assert_eq!(brief("Use e.g. this. Or that.", MAX_CHARS), "Use e.g. this");
        assert_eq!(brief("File is of type c:", MAX_CHARS), "File is of type c");
        let cut = brief(&"word ".repeat(60), MAX_CHARS);
        assert!(cut.ends_with(" ...") && cut.len() <= 115);
    }
}
