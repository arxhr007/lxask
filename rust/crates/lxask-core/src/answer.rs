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
use crate::py;
use crate::safety::find_warnings;
use crate::split::tasks_for;
use crate::validate::{check, Catalog};
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
/// objections is kept (the first answer on a tie).
pub fn answer(
    question: &str,
    chat: &mut Chat,
    search: &mut Search,
    catalog: Option<&Catalog>,
    mut resample: Option<&mut Resample>,
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

/// `$ ` before each command line, continuation lines aligned under it.
pub fn render_command(command: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut continued = false;
    for line in py::splitlines(command) {
        if py::strip(line).is_empty() {
            continue;
        }
        let prefix = if continued || line.starts_with([' ', '\t']) { "  " } else { "$ " };
        lines.push(format!("{prefix}{}", py::rstrip(line)));
        continued = py::rstrip(line).ends_with('\\');
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
pub fn render_command_line(steps: &[Step], question: &str) -> (String, String) {
    let commands: Vec<&str> = steps.iter().map(|s| s.command.trim()).filter(|c| !c.is_empty()).collect();
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
    notes.extend(unhandled(question, steps).into_iter().map(|text| format!("note: {text}")));
    (commands.join(" && "), notes.join("\n"))
}

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
        assert_eq!(line, "ps aux --sort=-%mem && kill -TERM PID");
        assert_eq!(notes, "step 1: unverified: the request says without top; the command uses it\nstep 2: warning: sends a signal");
    }

    #[test]
    fn a_step_without_a_command_is_left_out_of_the_line_and_said() {
        let (line, notes) = render_command_line(&[step("show the date", "date"), step("do magic", "")], "date and magic");
        assert_eq!(line, "date");
        assert_eq!(notes, "step 2: could not produce a command for: do magic");
    }
}
