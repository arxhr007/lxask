//! A second look at an answer the checks found nothing wrong with. The checks can
//! say that a program and its options exist in the documentation; they cannot say
//! the command does what was asked. The same model is asked that, with the
//! documentation the answer was built from as the evidence: the retrieved notes and
//! what each option used is documented to do.
//!
//! A "wrong" is one more objection: the answer is sampled again with the objection
//! named, and the new one is looked at too. An answer still doubted is marked
//! `unverified:` — the look judges from the same notes, it does not prove.

use crate::explain::Described;
use crate::prompt::{format_context, Doc, Message, MAX_CONTEXT_CHARS};
use crate::py;

/// A doubted answer gets this many new tries, each looked at in turn.
pub const RETRIES: usize = 1;

/// The documented option meanings get this much of the look's context.
const MAX_DESCRIBED_CHARS: usize = 1200;

/// What a look ends in.
#[derive(Clone, Debug, PartialEq)]
pub enum Verdict {
    /// "ok": the command does what the request asks, as far as the notes show
    Ok,
    /// "wrong: ...": what it objects to ("" when it gives no reason)
    Wrong(String),
    /// the reply was neither, and a look that cannot say is no objection
    Unknown,
}

pub const VERIFY_SYSTEM: &str = "You check whether a Linux command line does what the request asks, \
judging only from the request, the command, and the reference notes from the documentation. \
Names the request did not give may appear as placeholders such as path/to/file or \
example.com, and a request that rules a program out (\"without grep\") is answered right by \
a different program that does the job. Say 'wrong' only when the command clearly cannot do \
what the request asks or clearly does something else; when in doubt, say 'ok'. \
Reply with exactly 'ok' or 'wrong: <what it does wrong or misses>'.";

/// What the command's parts are documented to be and do, as `- program ...: ...` lines.
fn described_lines(described: &[Described], max_chars: usize) -> String {
    let mut lines = Vec::new();
    let mut used = 0;
    'outer: for entry in described {
        let summary = if entry.summary.is_empty() { None } else { Some(format!("- {}: {}", entry.program, entry.summary)) };
        for line in summary.into_iter().chain(entry.options.iter().map(|(o, t)| format!("- {} {}: {}", entry.program, o, t))) {
            let length = py::char_len(&line);
            if used + length > max_chars {
                break 'outer;
            }
            used += length + 1;
            lines.push(line);
        }
    }
    lines.join("\n")
}

/// The look's prompt: the evidence first — what the options used do, then the
/// retrieved notes — then the request, what it rules out, and the answer under it.
pub fn messages(task: &str, command: &str, notes: &[Doc], described: &[Described], ruled_out: &[String]) -> Vec<Message> {
    let mut evidence = described_lines(described, MAX_DESCRIBED_CHARS);
    let context = format_context(notes, MAX_CONTEXT_CHARS);
    if !context.is_empty() {
        if !evidence.is_empty() {
            evidence.push('\n');
        }
        evidence.push_str(&context);
    }
    let mut user = String::new();
    if !evidence.is_empty() {
        user = format!("Reference notes:\n{evidence}\n\n");
    }
    user = format!("{user}Request: {}", py::strip(task));
    if !ruled_out.is_empty() {
        user = format!("{user}\nThe request rules out these programs: {}; the command must not run them.", ruled_out.join(", "));
    }
    user = format!("{user}\nCommand: {}", py::strip(command));
    vec![
        Message { role: "system", content: VERIFY_SYSTEM.to_string() },
        Message { role: "user", content: user },
    ]
}

/// The generation messages for the try a doubt triggers: the same, with the doubted
/// answer and the objection named, so the resample does not repeat it.
pub fn retry_messages(base: &[Message], command: &str, doubt: &str) -> Vec<Message> {
    let mut retry = base.to_vec();
    if let Some(user) = retry.iter_mut().rev().find(|m| m.role == "user") {
        let note = if doubt.is_empty() { String::new() } else { format!(": {doubt}") };
        user.content = format!("{}\nThe answer `{command}` was objected to{note}. Try a different one.", user.content);
    }
    retry
}

/// The first meaningful line of the look's reply decides: "ok", or "wrong" with
/// what it objects to. Anything else is no objection — a look that cannot say so
/// cannot flag an answer either.
pub fn verdict(reply: &str) -> Verdict {
    for line in py::splitlines(reply) {
        let line = py::strip(line).trim_matches(|c: char| "\"'`".contains(c));
        if line.is_empty() {
            continue;
        }
        let mut parts = line.splitn(2, |c: char| c.is_whitespace() || c == ':');
        let head = parts.next().unwrap_or("").trim_end_matches(['.', ',', '!', ';']).to_lowercase();
        let reason = parts.next().map(|r| py::strip(r).to_string()).unwrap_or_default();
        return match head.as_str() {
            "ok" | "okay" => Verdict::Ok,
            "wrong" => Verdict::Wrong(reason),
            _ => Verdict::Unknown,
        };
    }
    Verdict::Unknown
}

// A "wrong" reason that is only these words says nothing about the answer.
const REASON_FILLER: &[&str] = &[
    "a", "an", "and", "are", "as", "but", "by", "command", "do", "does", "for", "from", "in",
    "is", "it", "its", "line", "not", "of", "on", "only", "or", "request", "that", "the",
    "this", "to", "use", "uses", "using", "with", "without",
];

fn words(text: &str) -> std::collections::HashSet<String> {
    text.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '-'))
        .filter(|w| !w.is_empty())
        .map(|w| w.to_lowercase())
        .collect()
}

/// Whether `reason` says something the command and the request do not: a "wrong"
/// whose reason only echoes them ("wrong: id -un") is the look pattern-matching,
/// not judging, and counts as no objection. Boilerplate words never count as novel.
pub fn meaningful(reason: &str, command: &str, task: &str) -> bool {
    let known: std::collections::HashSet<String> = words(command)
        .into_iter()
        .chain(words(task))
        .chain(REASON_FILLER.iter().map(|w| w.to_string()))
        .collect();
    words(reason).iter().any(|w| !known.contains(w))
}

/// Whether a command earns the extra generation. The look's value scales with what
/// can go wrong: pipes, several programs, substitutions and several options are where
/// semantic slips live; a one-program, one-option line it mostly doubts for free, and
/// the doubt is usually wrong. A request that rules a program out is the exception —
/// the look catches the ruled-out program sneaking back in under another name.
pub fn worth_a_look(described: &[Described], ruled_out: &[String]) -> bool {
    !ruled_out.is_empty()
        || described.len() > 1
        || described.first().map_or(0, |d| d.options.len()) > 1
}

/// The objection a look's reply amounts to: the reason a "wrong" gives, when it is a
/// reason at all. Ok, unclear and echo answers are no objection.
pub fn objection(reply: &str, command: &str, task: &str) -> Option<String> {
    match verdict(reply) {
        Verdict::Wrong(reason) if reason.is_empty() || meaningful(&reason, command, task) => Some(reason),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::prompt::format_answer;

    #[test]
    fn a_verdict_is_ok_wrong_or_unreadable() {
        assert_eq!(verdict("ok"), Verdict::Ok);
        assert_eq!(verdict("  OK."), Verdict::Ok);
        assert_eq!(verdict("\n\nok, looks right"), Verdict::Ok);
        assert_eq!(verdict("wrong: -s sorts stable, not by size"), Verdict::Wrong("-s sorts stable, not by size".into()));
        assert_eq!(verdict("wrong"), Verdict::Wrong(String::new()));
        assert_eq!(verdict("WRONG: it deletes them"), Verdict::Wrong("it deletes them".into()));
        // an answer-shape reply, a ramble, or nothing: not an objection
        assert_eq!(verdict(&format_answer("ls -l")), Verdict::Unknown);
        assert_eq!(verdict("the command sorts by size"), Verdict::Unknown);
        assert_eq!(verdict(""), Verdict::Unknown);
        assert_eq!(verdict("yes"), Verdict::Unknown);
    }

    #[test]
    fn the_retry_names_the_doubted_answer_and_why() {
        let base = crate::prompt::build_messages("sort by size", &[]);
        let retry = retry_messages(&base, "sort -s f", "-s is a stable sort");
        assert_eq!(retry.len(), base.len());
        assert!(retry[1].content.contains("`sort -s f`"));
        assert!(retry[1].content.contains("-s is a stable sort"));
        assert!(retry[1].content.starts_with(&base[1].content[..20]));
        // no user message to amend: the base comes back unchanged
        let bare = vec![Message { role: "system", content: "x".into() }];
        assert_eq!(retry_messages(&bare, "c", "d"), bare);
    }

    #[test]
    fn the_evidence_lists_documented_meanings_then_notes() {
        let described = vec![
            Described { program: "sort".into(), summary: "Sort lines.".into(),
                        options: vec![("-s".into(), "Stable sort.".into()), ("-h".into(), "Compare human readable numbers.".into())] },
            Described { program: "du".into(), summary: String::new(), options: vec![] },
        ];
        let notes = vec![Doc { cmd: "sort".into(), title: "sort file sizes".into(), body: "sort -h".into(), source: "tldr".into(), score: 1.0 }];
        let built = messages("sort by size", "sort -s f", &notes, &described, &[]);
        let user = &built[1].content;
        let options = user.find("- sort -s: Stable sort.").unwrap();
        let note = user.find("- sort: sort file sizes: `sort -h`").unwrap();
        assert!(options < note && user.contains("Request: sort by size\nCommand: sort -s f"));
        // an entry with no documented options leaves only its summary, and one with
        // neither leaves nothing
        assert!(user.contains("- sort -h: Compare human readable numbers.") && !user.contains("du"));
        // no evidence at all: no notes header, just the request and the command
        let bare = messages("t", "c", &[], &[], &[]);
        assert_eq!(bare[1].content, "Request: t\nCommand: c");
        // a ruled-out program is named for the look, between request and command
        let ruled = messages("count lines without wc", "awk 'END {print NR}' f", &[], &[], &["wc".to_string()]);
        assert_eq!(
            ruled[1].content,
            "Request: count lines without wc\nThe request rules out these programs: wc; the command must not run them.\nCommand: awk 'END {print NR}' f"
        );
    }

    #[test]
    fn only_commands_with_something_to_check_earn_a_look() {
        let one_option = vec![Described {
            program: "find".into(),
            summary: String::new(),
            options: vec![("-name".into(), "match name".into())],
        }];
        let two_options = vec![Described {
            program: "find".into(),
            summary: String::new(),
            options: vec![("-name".into(), "match name".into()), ("-type".into(), "match type".into())],
        }];
        let two_programs = vec![
            Described { program: "du".into(), summary: String::new(), options: vec![] },
            Described { program: "sort".into(), summary: String::new(), options: vec![] },
        ];
        assert!(!worth_a_look(&one_option, &[]));
        assert!(!worth_a_look(&[], &[]));
        assert!(worth_a_look(&two_options, &[]));
        assert!(worth_a_look(&two_programs, &[]));
        // a request that rules a program out keeps its look, however plain the answer
        assert!(worth_a_look(&one_option, &["wc".to_string()]));
    }

    #[test]
    fn an_objection_needs_a_reason_the_request_and_command_do_not_hold() {
        // echoes and restatements count as no objection; a reason that names what
        // is off counts
        assert_eq!(objection("wrong: id -un", "id -un", "user name without whoami"), None);
        assert_eq!(objection("wrong: awk 'END {print NR}' access.log", "awk 'END {print NR}' access.log", "count lines without wc"), None);
        assert_eq!(
            objection("wrong: shows only listening UDP sockets", "ss -u -l", "list open TCP ports").as_deref(),
            Some("shows only listening UDP sockets")
        );
        assert_eq!(objection("wrong: the command does not sort by size", "sort -s f", "sort by size"), None);
        assert_eq!(
            objection("wrong: -s is a stable sort, not by size", "sort -s f", "sort by size").as_deref(),
            Some("-s is a stable sort, not by size")
        );
        // a bare "wrong" is still a doubt to mark; ok and ramble are none
        assert_eq!(objection("wrong", "id -un", "whoami").as_deref(), Some(""));
        assert_eq!(objection("ok", "id -un", "whoami"), None);
        assert_eq!(objection("the command prints the user", "id -un", "whoami"), None);
    }
}
