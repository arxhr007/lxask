//! From a question to checked, explained steps. No I/O here: the model, the search and
//! the catalogue are passed in, so the whole flow can be tested without any of them.
//!
//! ```text
//! question -> split into tasks -> per task: retrieve -> command -> validate and check
//!          against the task (another try if either objects) -> explain -> safety
//! ```

use crate::constraints;
use crate::explain::{self, explain, Described};
use crate::prompt::{build_messages, parse_answer, Doc, Message};
use crate::py::{self, LazyRe};
use crate::re;
use crate::safety::find_warnings;
use crate::split::tasks_for;
use crate::validate::{check, Catalog};
use crate::verify;
use crate::Error;

pub const RETRIES: usize = 2;

/// The model, as the flow sees it: messages in, reply text out (None if nothing usable).
pub type Chat<'a> = dyn FnMut(&[Message]) -> Result<Option<String>, Error> + 'a;
/// Another answer to the same messages; the number is the attempt, so each try differs.
pub type Resample<'a> = dyn FnMut(&[Message], usize) -> Result<Option<String>, Error> + 'a;
pub type Search<'a> = dyn FnMut(&str) -> Result<Vec<Doc>, Error> + 'a;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Step {
    pub task: String,
    /// empty when the model produced nothing usable
    pub command: String,
    /// what the validator could not confirm
    pub issues: Vec<String>,
    /// what the task asks for and the command lacks
    pub misses: Vec<String>,
    /// a documented example to fall back on, shown when there are issues
    pub closest: String,
    pub described: Vec<Described>,
    /// destructive-command warnings
    pub warnings: Vec<String>,
    /// what retrieval gave the model
    pub notes: Vec<Doc>,
    /// answers asked for before this one was kept
    pub tries: usize,
}

impl Step {
    pub fn new(task: &str) -> Step {
        Step { task: task.to_string(), tries: 1, ..Step::default() }
    }
}

type Checked = (String, Vec<String>, Vec<String>);

fn checked(task: &str, reply: Option<String>, catalog: Option<&Catalog>, request: &str) -> Checked {
    let command = constraints::repair(&parse_answer(&reply.unwrap_or_default()), task);
    // the split may have dropped "no sudo" from the task; the request still says it
    let command = constraints::ruled_out_repair(&command, request);
    if command.is_empty() {
        return (String::new(), Vec::new(), Vec::new());
    }
    let issues = match catalog {
        Some(catalog) => check(&command, catalog, false).into_iter().map(|i| i.detail).collect(),
        None => Vec::new(),
    };
    let misses = constraints::check(task, &command, request).into_iter().map(|m| m.detail).collect();
    (command, issues, misses)
}

// The literals a split must keep: they decide which files a step touches. Tasks are
// written in words, and the training data's tasks often name a path or a count in words
// too ("the nginx access log", "first six lines"), so for other kinds a task list that
// lacks a literal is not evidence of a bad split (a third of good ones do).
const SPLIT_KEEPS: &[&str] = &["extension", "size", "duration"];

/// The request's literals that `text` (tasks, or commands) does not keep.
fn lost(question: &str, text: &str, kinds: Option<&[&str]>) -> Vec<String> {
    constraints::check(question, text, "")
        .into_iter()
        // slips and ruled-out programs are judged per command, not over the whole answer
        .filter(|miss| !matches!(miss.kind, "slip" | "forbidden") && kinds.map_or(true, |k| k.contains(&miss.kind)))
        .map(|miss| miss.detail)
        .collect()
}

/// The tasks of a request. A split that loses which files the request is about ("delete
/// .log files" -> "delete files") makes every step answer the wrong task, so with
/// `resample` it is tried again, and the split that keeps the most is used.
pub fn split(question: &str, chat: &mut Chat, mut resample: Option<&mut Resample>) -> Result<Vec<String>, Error> {
    let mut failure: Option<Error> = None;
    let mut tasks = tasks_for(question, &mut |m: &[Message]| guard(chat(m), &mut failure));
    if let Some(error) = failure.take() {
        return Err(error);
    }
    let Some(resample) = resample.as_deref_mut() else { return Ok(tasks) };
    if tasks.len() < 2 {
        return Ok(tasks);
    }
    let mut missing = lost(question, &tasks.join(" ; "), Some(SPLIT_KEEPS));
    for attempt in 0..RETRIES {
        if missing.is_empty() {
            break;
        }
        let candidate = tasks_for(question, &mut |m: &[Message]| guard(resample(m, attempt), &mut failure));
        if let Some(error) = failure.take() {
            return Err(error);
        }
        let candidate_missing = lost(question, &candidate.join(" ; "), Some(SPLIT_KEEPS));
        if candidate.len() >= 2 && candidate_missing.len() < missing.len() {
            tasks = candidate;
            missing = candidate_missing;
        }
    }
    Ok(tasks)
}

/// `tasks_for` takes an infallible model; a failed call is kept here and raised after.
fn guard(reply: Result<Option<String>, Error>, failure: &mut Option<Error>) -> Option<String> {
    match reply {
        Ok(reply) => reply,
        Err(error) => {
            failure.get_or_insert(error);
            None
        }
    }
}

/// What the request asks for that no step's command has, for a multi-step answer (a
/// single step is checked against the request itself).
pub fn unhandled(question: &str, steps: &[Step]) -> Vec<String> {
    let commands: Vec<&str> = steps.iter().map(|s| s.command.as_str()).filter(|c| !c.is_empty()).collect();
    if steps.len() < 2 || commands.is_empty() {
        return Vec::new();
    }
    lost(question, &commands.join(" ; "), None)
        .into_iter()
        .map(|text| text.replace("the command does not", "none of the commands"))
        .collect()
}

/// One step per task in the question. Without a catalogue, commands are not validated or
/// explained. With `resample`, an answer that is missing, or that the validator or the
/// task check objects to, gets up to RETRIES more tries, and the one with the fewest
/// objections is kept (the first answer on a tie). With `verify`, an answer nothing
/// objected to gets a second look from the model (lxask_core::verify): a "wrong" is one
/// more objection, retried like the others, and one still doubted is marked unverified.
pub fn answer(
    question: &str,
    chat: &mut Chat,
    search: &mut Search,
    catalog: Option<&Catalog>,
    mut resample: Option<&mut Resample>,
    mut verify: Option<&mut Chat>,
) -> Result<Vec<Step>, Error> {
    let mut steps = Vec::new();
    // "without lsof": notes on lsof would only talk the model into it
    let ruled_out = constraints::forbidden(question);
    for task in split(question, chat, resample.as_deref_mut())? {
        let mut notes = search(&task)?;
        let task_rules_out = constraints::forbidden(&task);
        notes.retain(|doc| {
            let program = crate::retrieve::program(&doc.cmd).to_lowercase();
            !ruled_out.contains(&program) && !task_rules_out.contains(&program)
        });
        let messages = build_messages(&task, &notes);
        let (mut command, mut issues, mut misses) = checked(&task, chat(&messages)?, catalog, question);
        let mut tries = 1;
        if let Some(resample) = resample.as_deref_mut() {
            while (command.is_empty() || !issues.is_empty() || !misses.is_empty()) && tries <= RETRIES {
                let candidate = checked(&task, resample(&messages, tries - 1)?, catalog, question);
                tries += 1;
                if !candidate.0.is_empty()
                    && (command.is_empty() || candidate.1.len() + candidate.2.len() < issues.len() + misses.len())
                {
                    (command, issues, misses) = candidate;
                }
            }
        }
        // The second look needs the catalogue: its evidence is what the options used are
        // documented to do plus the retrieved notes, and with neither it only guesses.
        let mut doubted = false;
        let mut doubt = String::new();
        let mut doubted_first = None;
        if let (Some(look), Some(catalog)) = (verify.as_deref_mut(), catalog) {
            let mut retried = 0;
            while !command.is_empty() && issues.is_empty() && misses.is_empty() {
                let described = explain(&command, catalog);
                let ruled: Vec<String> = ruled_out.iter().chain(task_rules_out.iter()).cloned().collect();
                if !verify::worth_a_look(&command, &described, &ruled) {
                    break;
                }
                let reply = look(&verify::messages(&task, &command, &notes, &described, &ruled))?.unwrap_or_default();
                let Some(reason) = verify::objection(&reply, &command, &task) else { break };
                doubted = true;
                doubt = reason;
                if retried == verify::RETRIES {
                    break;
                }
                let Some(resample) = resample.as_deref_mut() else { break };
                retried += 1;
                if doubted_first.is_none() {
                    doubted_first = Some((command.clone(), doubt.clone()));
                }
                let candidate = checked(&task, resample(&verify::retry_messages(&messages, &command, &doubt), tries - 1)?, Some(catalog), question);
                tries += 1;
                // the retry keeps the doubt unless it answers cleanly; its own look is next
                if candidate.0.is_empty() || !candidate.1.is_empty() || !candidate.2.is_empty() {
                    break;
                }
                (command, issues, misses) = candidate;
                doubted = false;
                doubt.clear();
            }
        }
        // both doubted: the first answer — the model's best — is the one shown,
        // with its own doubt
        if doubted {
            if let Some((first_command, first_doubt)) = doubted_first {
                command = first_command;
                doubt = first_doubt;
            }
            misses.push(if doubt.is_empty() {
                "the second look judges this answer wrong".to_string()
            } else {
                format!("the second look judges this answer wrong: {doubt}")
            });
        }
        let mut step = Step { task, command, issues, misses, notes, tries, ..Step::default() };
        if !step.command.is_empty() {
            step.warnings = find_warnings(&step.command);
            if let Some(catalog) = catalog {
                step.described = explain(&step.command, catalog);
                if !step.issues.is_empty() {
                    step.closest = step
                        .notes
                        .iter()
                        .find(|doc| doc.source == "tldr" && !doc.body.is_empty())
                        .map(|doc| doc.body.clone())
                        .unwrap_or_default();
                }
            }
        }
        steps.push(step);
    }
    Ok(steps)
}

/// A command someone already has, checked and described the way an answer is
/// (`lxask --explain`): no model, only the catalogue and the safety rules. The shell is
/// asked to parse it too, since unlike a model's answer it may be any length or shape.
pub fn describe(command: &str, catalog: Option<&Catalog>) -> Step {
    let command = py::strip(command).to_string();
    let mut step = Step { command: command.clone(), ..Step::new("") };
    if command.is_empty() {
        return step;
    }
    step.warnings = find_warnings(&command);
    if let Some(catalog) = catalog {
        step.issues = check(&command, catalog, true).into_iter().map(|issue| issue.detail).collect();
        step.described = explain(&command, catalog);
    }
    step
}

// `<<EOF`, `<<-'EOF'`, `<< "END"`: a here-document starts on the next line (not `<<<`,
// a here-string). Group 1 is `-` when leading tabs are stripped from its lines.
static HEREDOC: LazyRe = re!(r#"(?<!<)<<(?!<)(-?)\s*(?:'([^']+)'|"([^"]+)"|\\?([A-Za-z_][A-Za-z0-9_]*))"#);

/// `$ ` before each command line, continuation lines aligned under it. The lines of a
/// here-document are its data, so they are aligned under the command too, as written.
pub fn render_command(command: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut continued = false;
    // the delimiters of the here-documents still open, in order, and whether tabs go
    let mut heredocs: std::collections::VecDeque<(String, bool)> = std::collections::VecDeque::new();
    for line in py::splitlines(command) {
        if let Some((delimiter, tabs)) = heredocs.front() {
            let body = if *tabs { line.trim_start_matches('\t') } else { line };
            if body == delimiter {
                heredocs.pop_front();
            }
            lines.push(format!("  {}", py::rstrip(line)).trim_end().to_string());
            continue;
        }
        if py::strip(line).is_empty() {
            continue;
        }
        let prefix = if continued || line.starts_with([' ', '\t']) { "  " } else { "$ " };
        lines.push(format!("{prefix}{}", py::rstrip(line)));
        continued = py::rstrip(line).ends_with('\\');
        for caps in HEREDOC.finditer(line) {
            let delimiter = (2..=4).find_map(|i| py::group(&caps, i)).unwrap_or("");
            heredocs.push_back((delimiter.to_string(), py::group_or_empty(&caps, 1) == "-"));
        }
    }
    lines
}

/// The answer as printed. A single step is shown bare; several are numbered, each headed
/// by its task. With `question`, what the request asks for and no command has is noted
/// at the end.
pub fn render(steps: &[Step], explanations: bool, question: Option<&str>) -> String {
    let numbered = steps.len() > 1;
    let indent = if numbered { "   " } else { "" };
    let mut out: Vec<String> = Vec::new();
    for (number, step) in steps.iter().enumerate() {
        if numbered {
            if !out.is_empty() {
                out.push(String::new());
            }
            out.push(format!("{}. {}", number + 1, py::upper_first(&step.task)));
        }
        if step.command.is_empty() {
            out.push(format!("{indent}(could not produce a command for this step)"));
            continue;
        }
        out.extend(render_command(&step.command).into_iter().map(|line| format!("{indent}{line}")));
        let detail = format!("{indent}  ");
        for issue in step.issues.iter().chain(&step.misses) {
            out.push(format!("{detail}unverified: {issue}"));
        }
        if !step.closest.is_empty() {
            out.push(format!("{detail}closest documented example: {}", step.closest));
        }
        if explanations {
            out.extend(explain::render(&step.described).into_iter().map(|line| format!("{detail}{line}")));
        }
        for reason in &step.warnings {
            out.push(format!("{detail}warning: {reason}"));
        }
    }
    if let Some(question) = question {
        let missing = unhandled(question, steps);
        if !missing.is_empty() {
            out.push(String::new());
            out.extend(missing.into_iter().map(|text| format!("note: {text}")));
        }
    }
    out.join("\n")
}

/// The answer for a shell prompt (`lxask --command-only`, the Ctrl+G widget): the
/// commands as one line, the steps joined by `&&`, and apart from it what the plain
/// answer would say about them (unverified, warnings, notes), for the terminal.
///
/// A line with something unverified or a warning comes commented out, every line of it,
/// so Enter at the prompt (or `eval` in a script) does nothing until the `#` is deleted.
pub fn render_command_line(steps: &[Step], question: &str) -> (String, String) {
    let commands: Vec<&str> = steps.iter().map(|s| s.command.trim()).filter(|c| !c.is_empty()).collect();
    let mut line = commands.join(" && ");
    let unhandled = unhandled(question, steps);
    // A step with no command leaves the line doing only part of what was asked ("back up
    // the configs, then delete them" without the backup), and a request literal no command
    // keeps means it acts on the wrong files: both are read before they are run.
    let flagged = !commands.is_empty()
        && (steps.iter().any(|s| {
            s.command.trim().is_empty() || !s.issues.is_empty() || !s.misses.is_empty() || !s.warnings.is_empty()
        }) || !unhandled.is_empty());
    let mut notes: Vec<String> = Vec::new();
    for (number, step) in steps.iter().enumerate() {
        let label = if steps.len() > 1 { format!("step {}: ", number + 1) } else { String::new() };
        if step.command.is_empty() {
            notes.push(format!("{label}could not produce a command for: {}", step.task));
        }
        for issue in step.issues.iter().chain(&step.misses) {
            notes.push(format!("{label}unverified: {issue}"));
        }
        for reason in &step.warnings {
            notes.push(format!("{label}warning: {reason}"));
        }
    }
    notes.extend(unhandled.into_iter().map(|text| format!("note: {text}")));
    if flagged {
        line = line.lines().map(|l| format!("# {l}")).collect::<Vec<_>>().join("\n");
        notes.push(COMMENTED_OUT.to_string());
    }
    (line, notes.join("\n"))
}

/// Said after the notes when the line comes commented out.
pub const COMMENTED_OUT: &str =
    "the command is commented out because of the notes above: read it, then delete the # to run it";

#[cfg(test)]
mod tests {
    use super::*;

    fn step(task: &str, command: &str) -> Step {
        Step { command: command.into(), ..Step::new(task) }
    }

    #[test]
    fn one_line_for_the_prompt_and_the_notes_apart() {
        let mut first = step("list the processes by memory", "ps aux --sort=-%mem");
        first.misses.push("the request says without top; the command uses it".into());
        let mut second = step("stop it", "kill -TERM PID");
        second.warnings.push("sends a signal".into());
        let (line, notes) = render_command_line(&[first, second], "list processes by memory and stop the top one");
        assert_eq!(line, "# ps aux --sort=-%mem && kill -TERM PID");
        assert_eq!(
            notes,
            format!(
                "step 1: unverified: the request says without top; the command uses it\n\
                 step 2: warning: sends a signal\n{COMMENTED_OUT}"
            )
        );
    }

    #[test]
    fn a_here_document_is_shown_as_data_not_as_commands() {
        assert_eq!(
            render_command("cat > notes.txt <<'EOF'\nhello\n\nworld\nEOF\nwc -l notes.txt"),
            vec!["$ cat > notes.txt <<'EOF'", "  hello", "", "  world", "  EOF", "$ wc -l notes.txt"]
        );
        // <<- strips leading tabs from the delimiter line; <<< is a here-string, no body
        assert_eq!(render_command("cat <<-END\n\tx\n\tEND\nls"), vec!["$ cat <<-END", "  \tx", "  \tEND", "$ ls"]);
        assert_eq!(render_command("grep x <<< \"$v\"\nls"), vec!["$ grep x <<< \"$v\"", "$ ls"]);
    }

    #[test]
    fn a_checked_line_comes_as_it_is() {
        let (line, notes) = render_command_line(&[step("count the lines in data.txt", "wc -l data.txt")], "count the lines in data.txt");
        assert_eq!(line, "wc -l data.txt");
        assert_eq!(notes, "");
    }

    #[test]
    fn an_unverified_or_destructive_line_is_commented_out_every_line_of_it() {
        let mut unverified = step("find big files", "find . -size +1G -frobnicate");
        unverified.issues.push("find: -frobnicate is not a documented option".into());
        let (line, notes) = render_command_line(&[unverified], "find big files");
        assert_eq!(line, "# find . -size +1G -frobnicate");
        assert!(notes.ends_with(COMMENTED_OUT));

        let mut destructive = step("delete the logs", "find . -name '*.log' \\\n  -delete");
        destructive.warnings.push("deletes every file the find expression matches".into());
        let (line, _) = render_command_line(&[destructive], "delete the logs");
        assert_eq!(line, "# find . -name '*.log' \\\n#   -delete");
    }

    #[test]
    fn a_command_brought_to_explain_is_checked_described_and_warned_about() {
        let catalog = crate::validate::build_catalog(
            ["find", "rm"],
            // at least validate::MIN_OPTIONS of them, or find's options go unchecked
            [("find", "-name pattern", "Base of file name matches shell pattern pattern."),
             ("find", "-delete", "Delete files; true if removal succeeded."),
             ("find", "-type c", "File is of type c.")],
            [],
            [("find".to_string(), "Find files or directories under a directory tree, recursively.".to_string()),
             ("rm".to_string(), "Remove files or directories.".to_string())],
        );
        let step = describe("  find . -name '*.tmp' -delete -frobnicate ", Some(&catalog));
        assert_eq!(step.command, "find . -name '*.tmp' -delete -frobnicate");
        assert_eq!(step.issues, vec!["find: -frobnicate is not a documented option".to_string()]);
        assert_eq!(step.warnings, vec!["deletes every file the find expression matches".to_string()]);
        let printed = render(&[step], true, None);
        assert_eq!(
            printed,
            "$ find . -name '*.tmp' -delete -frobnicate\n\
             \x20 unverified: find: -frobnicate is not a documented option\n\
             \x20 find  Find files or directories under a directory tree, recursively\n\
             \x20   -name    Base of file name matches shell pattern pattern\n\
             \x20   -delete  Delete files; true if removal succeeded\n\
             \x20 warning: deletes every file the find expression matches"
        );
    }

    #[test]
    fn explaining_nothing_or_without_a_catalogue_still_warns() {
        assert_eq!(describe("   ", None).command, "");
        let step = describe("rm -rf build", None);
        assert!(step.issues.is_empty() && step.described.is_empty());
        assert_eq!(step.warnings, vec!["rm -r deletes whole directory trees and cannot be undone".to_string()]);
    }

    fn find_catalog() -> Catalog {
        crate::validate::build_catalog(
            ["find"],
            [("find", "-name pattern", "Base of file name matches pattern."),
             ("find", "-delete", "Delete files."),
             ("find", "-type c", "File is of type c.")],
            [],
            [("find".to_string(), "Find files.".to_string())],
        )
    }

    /// The split prompt asks for tasks; anything else answers with `command`.
    fn tasks_then(command: &'static str) -> impl FnMut(&[Message]) -> Result<Option<String>, Error> {
        move |m: &[Message]| {
            if m[0].content.starts_with("You split") {
                Ok(Some("- find the .log files".to_string()))
            } else {
                Ok(Some(crate::prompt::format_answer(command)))
            }
        }
    }

    #[test]
    fn a_clean_answer_the_second_look_likes_is_kept() {
        let catalog = find_catalog();
        let mut chat = tasks_then("find . -type f -name '*.log'");
        let mut search = |_: &str| Ok(Vec::new());
        let looks = std::cell::Cell::new(0);
        let mut verify = |_: &[Message]| {
            looks.set(looks.get() + 1);
            Ok(Some("ok".to_string()))
        };
        let steps = answer("find the .log files", &mut chat, &mut search, Some(&catalog), None, Some(&mut verify)).unwrap();
        assert_eq!(looks.get(), 1);
        assert_eq!(steps[0].command, "find . -type f -name '*.log'");
        assert!(steps[0].misses.is_empty());
    }

    #[test]
    fn a_doubted_answer_is_redone_and_a_clean_redo_kept() {
        let catalog = find_catalog();
        let mut chat = tasks_then("find /etc -type f -name '*.log'");
        let mut search = |_: &str| Ok(Vec::new());
        let mut resample = |_: &[Message], _: usize| Ok(Some(crate::prompt::format_answer("find /var/log -name '*.log' -type f")));
        let looks = std::cell::Cell::new(0);
        let mut verify = |_: &[Message]| {
            looks.set(looks.get() + 1);
            Ok(Some(if looks.get() == 1 { "wrong: it searches the wrong directory".to_string() } else { "ok".to_string() }))
        };
        let steps =
            answer("find the .log files", &mut chat, &mut search, Some(&catalog), Some(&mut resample), Some(&mut verify)).unwrap();
        assert_eq!(looks.get(), 2);
        assert_eq!(steps[0].command, "find /var/log -name '*.log' -type f");
        assert_eq!(steps[0].tries, 2);
        assert!(steps[0].misses.is_empty());
    }

    #[test]
    fn a_still_doubted_redo_is_marked_unverified() {
        let catalog = find_catalog();
        let mut chat = tasks_then("find /etc -name '*.log' -type f");
        let mut search = |_: &str| Ok(Vec::new());
        let mut resample = |_: &[Message], _: usize| Ok(Some(crate::prompt::format_answer("find /var/log -name '*.log' -type f")));
        let looks = std::cell::Cell::new(0);
        let mut verify = |_: &[Message]| {
            looks.set(looks.get() + 1);
            Ok(Some(if looks.get() == 1 { "wrong: not the right place".to_string() } else { "wrong: still off".to_string() }))
        };
        let steps =
            answer("find the .log files", &mut chat, &mut search, Some(&catalog), Some(&mut resample), Some(&mut verify)).unwrap();
        // both doubted: the first answer — the model's best — is kept, with its own doubt
        assert_eq!(steps[0].command, "find /etc -name '*.log' -type f");
        assert_eq!(steps[0].tries, 2);
        assert_eq!(steps[0].misses, vec!["the second look judges this answer wrong: not the right place".to_string()]);
    }

    #[test]
    fn a_doubt_without_a_reason_and_without_retry_still_marks() {
        let catalog = find_catalog();
        let mut chat = tasks_then("find . -name '*.log' -type f");
        let mut search = |_: &str| Ok(Vec::new());
        let mut verify = |_: &[Message]| Ok(Some("wrong".to_string()));
        let steps = answer("find the .log files", &mut chat, &mut search, Some(&catalog), None, Some(&mut verify)).unwrap();
        assert_eq!(steps[0].misses, vec!["the second look judges this answer wrong".to_string()]);
    }

    #[test]
    fn a_trivial_answer_gets_no_second_look() {
        let catalog = find_catalog();
        let mut chat = tasks_then("find . -name '*.log'");
        let mut search = |_: &str| Ok(Vec::new());
        let looks = std::cell::Cell::new(0);
        let mut verify = |_: &[Message]| {
            looks.set(looks.get() + 1);
            Ok(Some("wrong".to_string()))
        };
        let steps = answer("find the .log files", &mut chat, &mut search, Some(&catalog), None, Some(&mut verify)).unwrap();
        assert_eq!(looks.get(), 0);
        assert_eq!(steps[0].command, "find . -name '*.log'");
        assert!(steps[0].misses.is_empty());
    }

    #[test]
    fn an_answer_the_checks_flag_gets_no_second_look() {
        let catalog = find_catalog();
        let mut chat = tasks_then("find . -name '*.log' -frobnicate");
        let mut search = |_: &str| Ok(Vec::new());
        let looks = std::cell::Cell::new(0);
        let mut verify = |_: &[Message]| {
            looks.set(looks.get() + 1);
            Ok(Some("wrong".to_string()))
        };
        let steps = answer("find the .log files", &mut chat, &mut search, Some(&catalog), None, Some(&mut verify)).unwrap();
        assert_eq!(looks.get(), 0);
        assert_eq!(steps[0].issues, vec!["find: -frobnicate is not a documented option".to_string()]);
        assert!(steps[0].misses.is_empty());
    }

    #[test]
    fn a_step_without_a_command_is_left_out_of_the_line_and_said() {
        let (line, notes) = render_command_line(&[step("show the date", "date"), step("do magic", "")], "date and magic");
        assert_eq!(line, "# date");
        assert_eq!(notes, format!("step 2: could not produce a command for: do magic\n{COMMENTED_OUT}"));
        // no command at all: nothing to comment out, the line stays empty
        let (line, notes) = render_command_line(&[step("do magic", "")], "do magic");
        assert_eq!((line.as_str(), notes.as_str()), ("", "could not produce a command for: do magic"));
    }

    #[test]
    fn a_line_that_loses_what_the_request_names_is_commented_out() {
        // the split kept ".log" in no step's command, so the line acts on other files
        let steps = [step("find the files", "find . -type f"), step("count them", "wc -l")];
        let (line, notes) = render_command_line(&steps, "find the .log files and count them");
        assert!(line.starts_with("# "), "{line}");
        assert!(notes.contains("note: ") && notes.ends_with(COMMENTED_OUT), "{notes}");
    }
}
