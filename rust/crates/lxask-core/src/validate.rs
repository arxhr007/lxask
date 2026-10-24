//! Check a command line against the Linux documentation. Nothing is ever executed.
//!
//! What it can tell: the program exists, its options are documented, the line parses.
//! What it cannot tell: whether the command does what was asked. An empty result means
//! "nothing wrong was found", not "correct".

use std::collections::{HashMap, HashSet};
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

use crate::constraints::which;
use crate::py::{self, LazyRe};
use crate::{re, shlex};

pub const BUILTINS: &[&str] = &[
    ".", ":", "[", "[[", "alias", "bg", "bind", "break", "builtin", "caller", "cd", "command", "compgen", "complete",
    "continue", "declare", "dirs", "disown", "echo", "enable", "eval", "exec", "exit", "export", "false", "fc", "fg",
    "getopts", "hash", "help", "history", "jobs", "kill", "let", "local", "logout", "mapfile", "popd", "printf",
    "pushd", "pwd", "read", "readarray", "readonly", "return", "set", "shift", "shopt", "source", "suspend", "test",
    "times", "trap", "true", "type", "typeset", "ulimit", "umask", "unalias", "unset", "wait",
];

// Words after which a new command starts, and words that open a construct with no command to check.
const COMMAND_FOLLOWS: &[&str] = &["if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"];
const NO_COMMAND: &[&str] = &["for", "case", "select", "function", "done", "fi", "esac", "}", "in"];

// Programs that run another command: `sudo -u bob ls -l` is checked as sudo, then as ls.
const WRAPPERS: &[&str] = &[
    "sudo", "doas", "xargs", "env", "nohup", "time", "watch", "timeout", "nice", "ionice", "stdbuf", "setsid", "exec",
    "command", "builtin", "chronic",
];

fn wrapper_operands(name: &str) -> usize {
    if name == "timeout" { 1 } else { 0 } // leading arguments of the wrapper itself (the duration)
}

/// Pages documenting the same options under another name.
fn families(name: &str) -> &'static [&'static str] {
    match name {
        "apt" => &["apt-get", "apt-cache"],
        "awk" => &["gawk", "mawk"],
        "egrep" | "fgrep" => &["grep"],
        "nc" => &["netcat", "nc.openbsd", "ncat"],
        "pip" => &["pip3"],
        "python" => &["python3"],
        "sh" => &["dash", "bash"],
        "vi" => &["vim"],
        _ => &[],
    }
}

const SIGNAL_PROGRAMS: &[&str] = &["kill", "pkill", "killall", "skill"];
// Programs whose one man page describes subcommand options in prose or synopses only.
const SUBCOMMAND_STYLE: &[&str] =
    &["tmux", "screen", "ip", "openssl", "nmcli", "virsh", "svn", "hg", "busybox", "lvm", "zfs", "zpool", "btrfs"];
// Programs whose every subcommand has a manual page of its own (git-commit(1)). A
// subcommand without one is an add-on: tldr documents git-extras (`git magic`,
// `git setup`) beside git itself, and those are not what a stock system has.
const PAGE_PER_SUBCOMMAND: &[&str] = &["git"];
pub const MIN_SUBCOMMAND_PAGES: usize = 20;
// Programs whose first operand is a verb their man page lists under COMMANDS
// (`systemctl start`, `loginctl list-sessions`): another word there is not a command
// they have (`systemctl inspect`). The verbs come from the man pages when the index
// is built (scripts/sources.py).
pub const VERB_PROGRAMS: &[&str] =
    &["systemctl", "loginctl", "hostnamectl", "timedatectl", "localectl", "resolvectl", "networkctl", "busctl"];
// Below this many verbs the COMMANDS section was not parsed well enough to rule anything out.
const MIN_VERBS: usize = 5;
static VERB_WORD: LazyRe = re!(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$");

// Verbs these programs accept that their man pages leave out: older names kept working.
fn extra_verbs(program: &str) -> &'static [&'static str] {
    match program {
        "systemctl" => &["force-reload", "condrestart", "condreload", "condstop", "reload-or-try-restart", "check", "sleep", "clear-jobs"],
        "hostnamectl" => &["set-hostname", "set-icon-name", "set-chassis", "set-deployment", "set-location"],
        _ => &[],
    }
}
const FIND_EXEC: &[&str] = &["-exec", "-execdir", "-ok", "-okdir"];
const ALWAYS_OPTIONS: &[&str] = &["--help", "--version"];
pub const MIN_OPTIONS: usize = 3; // below this the page was not parsed well enough to rule anything out

pub const NOT_LINUX_PROGRAMS: &[&str] = &[
    "pbcopy", "pbpaste", "brew", "launchctl", "dscl", "sw_vers", "diskutil", "mdfind", "mdls", "osascript",
    "defaults", "networksetup", "system_profiler", "softwareupdate", "scutil", "ipconfig", "gfind", "gsed", "ggrep",
    "md5",
];

// Usage that only works on macOS or BSD. The option check catches most of it; these
// are the cases where the option exists on Linux but means something else.
static SED_INPLACE: LazyRe = re!(r#"\bsed\s+(?:-\w+\s+)*-i\s+(?:''|"")(?:\s|$)"#);
static STAT_FORMAT: LazyRe = re!(r#"\bstat\s+(?:-\w+\s+)*-f\s+['"]?%"#);
static IFCONFIG_EN: LazyRe = re!(r"\bifconfig\s+en\d");
static DATE_V: LazyRe = re!(r"\bdate\s+(?:-\w+\s+)*-v\s*[-+]?\d");

fn not_linux() -> [(&'static LazyRe, &'static str); 4] {
    [
        (&SED_INPLACE, "sed -i '' is the BSD form; GNU sed takes no separate argument"),
        (&STAT_FORMAT, "stat -f FORMAT is the BSD form; GNU stat uses -c"),
        (&IFCONFIG_EN, "en0-style interface names are macOS"),
        (&DATE_V, "date -v is BSD; GNU date uses -d"),
    ]
}

// Words a program accepts that its man page does not list as option headings.
fn quirks(program: &str) -> &'static [&'static str] {
    if program == "find" { &["-o", "-a", "-or", "-and", "-not"] } else { &[] }
}

const PROSE_WORDS: &[&str] = &["a", "an", "and", "are", "as", "can", "does", "has", "is", "may", "or", "option", "options", "the", "was", "were", "will"];
// Arguments that are text or test operators (`[ "$i" -le 5 ]`): a leading dash proves nothing.
const TEXT_ARGUMENTS: &[&str] = &["echo", "printf", "[", "[[", "test"];

static ASSIGNMENT: LazyRe = re!(r"^[A-Za-z_]\w*\+?=");
static FLAG: LazyRe = re!(r"--?[A-Za-z0-9?@%#+:^!][\w.+:@%^?!-]*");
static NO_VARIANT: LazyRe = re!(r"^(--?)\[(no-?)\](.+)$");
const BOUNDARY: &[&str] = &["|", "||", "&&", ";", "&", "|&", ";;"];
static PROGRAM_NAME: LazyRe = re!(r"[A-Za-z_][\w.+-]*|\[\[?|[.:]");
const SEMI: &str = "\u{0}semi\u{0}";
static TITLE_SEPARATOR: LazyRe = re!(r",\s*|\s+\|\s+|\s+or\s+");
static NUMERIC_OPTION: LazyRe = re!(r"-\d+");
static SIGNAL_NAME: LazyRe = re!(r"-(?:SIG)?[A-Z][A-Z0-9+]*");
static CHMOD_MODE: LazyRe = re!(r"-[rwxXst]+");
static BASH_LINE: LazyRe = re!(r"^.*?line \d+: ");

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Issue {
    pub program: String,
    /// "unknown-program" | "undocumented-option" | "not-linux" | "syntax"
    pub kind: &'static str,
    pub detail: String,
}

/// `-C, --directory=DIR` -> [("-C", true), ("--directory", true)]: each option, and
/// whether it takes an argument (given on any spelling of it).
pub fn parse_option_title(title: &str) -> Vec<(String, bool)> {
    let mut flags: Vec<String> = Vec::new();
    let mut takes_argument = false;
    for piece in TITLE_SEPARATOR.split(py::strip(title), 0) {
        let variants: Vec<String> = match NO_VARIANT.match_(piece) {
            Some(caps) => {
                let (dash, no, rest) = (py::group_or_empty(&caps, 1), py::group_or_empty(&caps, 2), py::group_or_empty(&caps, 3));
                vec![format!("{dash}{rest}"), format!("{dash}{no}{rest}")]
            }
            None => vec![piece.to_string()],
        };
        for variant in variants {
            let Some(caps) = FLAG.match_(&variant) else { continue };
            let whole = caps.get(0).unwrap();
            flags.push(whole.as_str().to_string());
            takes_argument = takes_argument || names_an_argument(&variant[whole.end()..]);
        }
    }
    // less(1) writes `-ppattern or --pattern=pattern`: next to a long spelling, a
    // longer single-dash word is a short option with its argument attached.
    if flags.iter().any(|flag| flag.starts_with("--")) {
        let short: Vec<String> =
            flags.iter().filter(|f| !f.starts_with("--") && py::char_len(f) > 2).cloned().collect();
        for flag in short {
            flags.push(py::prefix(&flag, 2).to_string());
            takes_argument = true;
        }
    }
    let mut unique: Vec<String> = Vec::new();
    for flag in flags {
        if !unique.contains(&flag) {
            unique.push(flag);
        }
    }
    unique.into_iter().map(|flag| (flag, takes_argument)).collect()
}

/// Whether the text after an option in a heading is an argument (`-I replace-str`) and
/// not a description sharing the line (`-print True; print the full file name`).
fn names_an_argument(rest: &str) -> bool {
    if rest.starts_with('=') {
        return true;
    }
    let words = py::split_ws(rest);
    let starts_with_space = rest.chars().next().is_some_and(py::is_space);
    if !starts_with_space || !(1..=2).contains(&words.len()) {
        return false;
    }
    let first = words[0];
    let prose = first.ends_with(['.', ';', ':']) || PROSE_WORDS.contains(&first.to_lowercase().as_str());
    !prose && !words.iter().any(|word| word.starts_with('-'))
}

/// What the documentation knows: program names, and each program's options.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Catalog {
    pub programs: HashSet<String>,
    /// page name -> option -> takes a separate argument
    pub options: HashMap<String, HashMap<String, bool>>,
    /// programs whose options belong to subcommands (git, docker, tmux, ip, ...)
    pub with_subcommands: HashSet<String>,
    /// page name -> option -> what the manual says it does (for explain)
    pub texts: HashMap<String, HashMap<String, String>>,
    /// page name (`tar`, `git commit`) -> one-line description
    pub summaries: HashMap<String, String>,
    /// program -> the verbs its man page lists under COMMANDS (VERB_PROGRAMS only)
    pub subcommands: HashMap<String, HashSet<String>>,
}

impl Catalog {
    /// `name` is a page name: `tar`, or `git commit` for a subcommand's page.
    pub fn add_program(&mut self, name: &str) {
        let words = py::split_ws(name);
        if let Some(first) = words.first() {
            self.programs.insert(first.to_string());
            if words.len() > 1 {
                self.with_subcommands.insert(first.to_string());
            }
        }
    }

    /// Index of the first argument that is not an option or an option's value: `commit`
    /// in `git -C /repo commit -m x`.
    pub fn subcommand_position(&self, name: &str, arguments: &[String]) -> Option<usize> {
        let own = self.options.get(name);
        let mut skip = false;
        for (position, argument) in arguments.iter().enumerate() {
            if skip {
                skip = false;
            } else if argument.starts_with('-') {
                skip = own.and_then(|o| o.get(argument)).copied().unwrap_or(false); // `-C` takes the next word
            } else {
                return Some(position);
            }
        }
        None
    }

    pub fn subcommand<'a>(&self, name: &str, arguments: &'a [String]) -> Option<&'a str> {
        self.subcommand_position(name, arguments).map(|p| arguments[p].as_str())
    }

    /// Where the subcommand sits in `arguments`, when its options are documented nowhere
    /// we can read. Everything after it is then left unchecked.
    pub fn subcommand_at(&self, name: &str, arguments: &[String]) -> Option<usize> {
        let position = self.subcommand_position(name, arguments)?;
        if self.options.contains_key(&format!("{name}-{}", arguments[position])) {
            return None;
        }
        (self.with_subcommands.contains(name) || SUBCOMMAND_STYLE.contains(&name)).then_some(position)
    }

    /// The subcommand, if `name` is a program whose every real subcommand has a manual
    /// page of its own and this one has none (`git magic`: a third-party add-on).
    pub fn unknown_subcommand<'a>(&self, name: &str, arguments: &'a [String]) -> Option<&'a str> {
        if !PAGE_PER_SUBCOMMAND.contains(&name) {
            return None;
        }
        let prefix = format!("{name}-");
        if self.options.keys().filter(|page| page.starts_with(&prefix)).count() < MIN_SUBCOMMAND_PAGES {
            return None; // those pages are not installed here: nothing to judge by
        }
        let subcommand = self.subcommand(name, arguments)?;
        (!subcommand.is_empty() && !self.options.contains_key(&format!("{name}-{subcommand}"))).then_some(subcommand)
    }

    pub fn add_subcommand(&mut self, program: &str, verb: &str) {
        self.subcommands.entry(program.to_string()).or_default().insert(verb.to_string());
    }

    /// The verb, if `name` takes a verb first (VERB_PROGRAMS) and its manual does not
    /// list this one (`systemctl inspect`).
    pub fn unknown_verb<'a>(&self, name: &str, arguments: &'a [String]) -> Option<&'a str> {
        if !VERB_PROGRAMS.contains(&name) {
            return None;
        }
        let verbs = self.subcommands.get(name).filter(|verbs| verbs.len() >= MIN_VERBS)?;
        let verb = self.subcommand(name, arguments)?;
        // `systemctl "$action"`, `systemctl nginx`: not a word to judge
        if !VERB_WORD.is_match(verb) || verbs.contains(verb) || extra_verbs(name).contains(&verb) {
            return None;
        }
        Some(verb)
    }

    pub fn add_option_title(&mut self, page: &str, title: &str, text: &str) {
        let parsed = parse_option_title(title);
        let known = self.options.entry(page.to_string()).or_default();
        for (flag, takes_argument) in &parsed {
            let entry = known.entry(flag.clone()).or_insert(false);
            *entry = *entry || *takes_argument;
        }
        if !text.is_empty() && !parsed.is_empty() {
            let texts = self.texts.entry(page.to_string()).or_default();
            for (flag, _) in parsed {
                texts.entry(flag).or_insert_with(|| text.to_string());
            }
        }
    }

    /// The pages that document `name` as used: its own, its family's, its subcommand's.
    pub fn pages_for(&self, name: &str, arguments: &[String]) -> Vec<String> {
        let mut pages = vec![name.to_string()];
        pages.extend(families(name).iter().map(|s| s.to_string()));
        if let Some(subcommand) = self.subcommand(name, arguments) {
            if !subcommand.is_empty() {
                pages.push(format!("{name}-{subcommand}")); // git commit -> git-commit(1)
            }
        }
        pages
    }

    /// What the manual says `flag` does for this use of `name`, or "".
    pub fn text_for(&self, name: &str, arguments: &[String], flag: &str) -> String {
        for page in self.pages_for(name, arguments).iter().rev() {
            // the subcommand's page knows best
            if let Some(text) = self.texts.get(page).and_then(|t| t.get(flag)) {
                if !text.is_empty() {
                    return text.clone();
                }
            }
        }
        String::new()
    }

    pub fn summary_for(&self, name: &str, arguments: &[String]) -> String {
        if let Some(subcommand) = self.subcommand(name, arguments).filter(|s| !s.is_empty()) {
            if let Some(summary) = self.summaries.get(&format!("{name} {subcommand}")).filter(|s| !s.is_empty()) {
                return summary.clone();
            }
        }
        self.summaries.get(name).cloned().unwrap_or_default()
    }

    /// Options used in a documented example count as documented.
    pub fn add_example(&mut self, command: &str) {
        let Ok(commands) = simple_commands(command) else { return };
        for tokens in commands {
            let (name, arguments) = head(&tokens);
            let Some(known) = self.options.get_mut(name) else {
                continue; // nothing is enforced for this program, so nothing to widen
            };
            for token in arguments {
                let flag = token.split('=').next().unwrap();
                if FLAG.fullmatches(flag) && !NUMERIC_OPTION.fullmatches(token) {
                    known.entry(flag.to_string()).or_insert(false);
                }
            }
        }
    }

    /// The options documented for `name`, or None when there is nothing to check against.
    pub fn options_for(&self, name: &str, arguments: &[String]) -> Option<HashMap<String, bool>> {
        let mut merged: HashMap<String, bool> = HashMap::new();
        for page in self.pages_for(name, arguments) {
            if let Some(options) = self.options.get(&page) {
                for (flag, takes_argument) in options {
                    let entry = merged.entry(flag.clone()).or_insert(false);
                    *entry = *entry || *takes_argument;
                }
            }
        }
        (merged.len() >= MIN_OPTIONS).then_some(merged)
    }
}

fn all_in(token: &str, set: &str) -> bool {
    token.chars().all(|c| set.contains(c))
}

/// Split a command line into its simple commands: pipelines, lists, `$(...)`, `find -exec`.
pub fn simple_commands(command: &str) -> Result<Vec<Vec<String>>, &'static str> {
    if command.contains("<<") {
        return Ok(Vec::new()); // a here-document's body is data, not commands
    }
    let joined = command.replace("\\\n", " ");
    let lines: Vec<&str> = py::splitlines(&joined).into_iter().filter(|line| !py::lstrip(line).starts_with('#')).collect();
    let text = lines.join(" ; ").replace("\\;", &format!(" {SEMI} "));
    // backticks open and close alternately
    let mut text_ticks = String::with_capacity(text.len());
    let mut opened = false;
    for c in text.chars() {
        if c == '`' {
            text_ticks.push_str(if opened { " ) " } else { " ( " });
            opened = !opened;
        } else {
            text_ticks.push(c);
        }
    }
    let tokens = shlex::Lexer::new(&text_ticks, true).tokens()?;

    // `stack` holds the command being read at each nesting depth, so the words after
    // `$(...)` go back to the command the substitution was an argument of.
    // Each stack entry is a command's index, or None for words that are dropped.
    let mut commands: Vec<Vec<String>> = vec![Vec::new()];
    let mut stack: Vec<Option<usize>> = vec![Some(0)];
    let mut skip_next = false;
    for token in tokens {
        if skip_next {
            skip_next = false;
        } else if !token.is_empty() && all_in(&token, ")") {
            for _ in token.chars() {
                if stack.len() > 1 {
                    stack.pop();
                }
            }
        } else if token.ends_with("((") {
            stack.extend([None, None]); // $(( ... )) is arithmetic: its words are collected and dropped
        } else if all_in(&token, "();|&<>") && token.ends_with('(') {
            commands.push(Vec::new());
            stack.push(Some(commands.len() - 1));
        } else if BOUNDARY.contains(&token.as_str()) {
            commands.push(Vec::new());
            *stack.last_mut().unwrap() = Some(commands.len() - 1);
        } else if all_in(&token, "<>&|;()") {
            skip_next = true; // a redirection: the next word is a file, not an argument
        } else if let Some(current) = *stack.last().unwrap() {
            commands[current].push(token);
        }
    }
    Ok(commands.into_iter().filter(|tokens| !tokens.is_empty()).collect())
}

/// The program a simple command runs, and its arguments; ("", []) if there is none to check.
fn head(tokens: &[String]) -> (&str, &[String]) {
    let mut i = 0;
    while i < tokens.len() && (ASSIGNMENT.match_(&tokens[i]).is_some() || COMMAND_FOLLOWS.contains(&tokens[i].as_str())) {
        i += 1;
    }
    if i == tokens.len() || NO_COMMAND.contains(&tokens[i].as_str()) {
        return ("", &[]);
    }
    let mut name = tokens[i].as_str();
    if name.starts_with('/') {
        name = name.rsplit('/').next().unwrap();
    }
    if !PROGRAM_NAME.fullmatches(name) {
        return ("", &[]); // a variable, a glob, arithmetic or a local script: nothing to look up
    }
    (name, &tokens[i + 1..])
}

// Options of common wrappers that take the next word as their value (`sudo -u bob ls`).
fn wrapper_value_options(name: &str) -> &'static [&'static str] {
    match name {
        "sudo" | "doas" => &["-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"],
        "xargs" => &["-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"],
        "nice" | "ionice" => &["-n", "-c"],
        "timeout" => &["-k", "-s"],
        "watch" => &["-n", "-d"],
        "env" => &["-u", "-C", "-S"],
        _ => &[],
    }
}

/// Every program a command line runs, wrapped ones too (`sudo lsof`, `xargs grep`,
/// `find -exec rm`, `sh -c 'ss -tlnp'`), with no catalogue: for checking what a
/// request ruled out. Unparseable lines run nothing we can name.
pub fn programs_run(command: &str) -> Vec<String> {
    let mut found = Vec::new();
    if let Ok(commands) = simple_commands(command) {
        for tokens in commands {
            run_by(&tokens, &mut found, 0);
            // `echo "$(whoami)"`: a substitution inside quotes stays in one token
            for token in &tokens {
                for inner in substitutions(token) {
                    for name in programs_run(inner) {
                        if !found.contains(&name) {
                            found.push(name);
                        }
                    }
                }
            }
        }
    }
    found
}

/// The commands inside `$(...)` and backquotes in one word (not `$((...))` arithmetic).
fn substitutions(token: &str) -> Vec<&str> {
    let mut found = Vec::new();
    let bytes = token.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'$' && bytes.get(i + 1) == Some(&b'(') && bytes.get(i + 2) != Some(&b'(') {
            let start = i + 2;
            let mut depth = 1;
            let mut j = start;
            while j < bytes.len() && depth > 0 {
                match bytes[j] {
                    b'(' => depth += 1,
                    b')' => depth -= 1,
                    _ => {}
                }
                j += 1;
            }
            if depth == 0 {
                found.push(&token[start..j - 1]);
            }
            i = j;
        } else if bytes[i] == b'`' {
            match token[i + 1..].find('`') {
                Some(end) => {
                    found.push(&token[i + 1..i + 1 + end]);
                    i += end + 2;
                }
                None => break,
            }
        } else {
            i += 1;
        }
    }
    found
}

fn run_by(tokens: &[String], found: &mut Vec<String>, depth: usize) {
    let (name, arguments) = head(tokens);
    if name.is_empty() || depth > 4 {
        return;
    }
    if !found.iter().any(|f| f == name) {
        found.push(name.to_string());
    }
    if WRAPPERS.contains(&name) {
        let mut i = 0;
        let mut operands = wrapper_operands(name);
        while i < arguments.len() {
            let token = arguments[i].as_str();
            if token == "--" {
                i += 1;
                break;
            }
            if token.starts_with('-') {
                i += 1 + wrapper_value_options(name).contains(&token) as usize;
            } else if ASSIGNMENT.match_(token).is_some() {
                i += 1;
            } else if operands > 0 {
                operands -= 1;
                i += 1;
            } else {
                break;
            }
        }
        if i < arguments.len() {
            run_by(&arguments[i..], found, depth + 1);
        }
    } else if name == "find" {
        let mut i = 0;
        while i < arguments.len() {
            if FIND_EXEC.contains(&arguments[i].as_str()) {
                let end = (i + 1..arguments.len()).find(|&j| arguments[j] == SEMI || arguments[j] == "+").unwrap_or(arguments.len());
                run_by(&arguments[i + 1..end], found, depth + 1);
                i = end;
            }
            i += 1;
        }
    } else if matches!(name, "sh" | "bash" | "dash" | "zsh") {
        if let Some(position) = arguments.iter().position(|a| a == "-c") {
            if let Some(script) = arguments.get(position + 1) {
                if let Ok(commands) = simple_commands(script) {
                    for inner in commands {
                        run_by(&inner, found, depth + 1);
                    }
                }
            }
        }
    }
}

/// One program as a command line uses it: what `check` judges and `explain` describes.
#[derive(Clone, Debug, PartialEq)]
pub struct Use {
    pub program: String,
    /// "known" | "unknown" | "not-linux" | "add-on" | "unknown-verb" (the verb in `arguments`)
    pub status: &'static str,
    pub arguments: Vec<String>,
    /// (option as written, the documented options it stands for). None: not documented.
    /// Empty: accepted without a manual entry of its own (`-9`, a signal name).
    pub options: Vec<(String, Option<Vec<String>>)>,
}

/// (the documented options one option-looking token stands for, consumes the next word)
fn resolve(token: &str, options: &HashMap<String, bool>, program: &str) -> (Option<Vec<String>>, bool) {
    let flag = token.split('=').next().unwrap();
    if let Some(&takes) = options.get(flag) {
        return (Some(vec![flag.to_string()]), takes && !token.contains('='));
    }
    if ALWAYS_OPTIONS.contains(&flag) {
        return (Some(vec![]), false);
    }
    if token.starts_with("--") {
        // GNU programs accept any unambiguous prefix of a long option.
        let matches: Vec<&String> = options.keys().filter(|known| known.starts_with(flag)).collect();
        return if matches.len() == 1 { (Some(vec![matches[0].clone()]), false) } else { (None, false) };
    }
    if NUMERIC_OPTION.fullmatches(token) {
        return (Some(vec![]), false); // head -5, kill -9, nice -10
    }
    if SIGNAL_PROGRAMS.contains(&program) && SIGNAL_NAME.fullmatches(token) {
        return (Some(vec![]), false);
    }
    if program == "chmod" && CHMOD_MODE.fullmatches(token) {
        return (Some(vec![]), false); // a mode, not an option
    }
    if quirks(program).contains(&token) {
        return (Some(vec![]), false);
    }
    let letters: Vec<char> = token.chars().skip(1).collect();
    if !options.contains_key(&format!("-{}", letters[0])) {
        return (None, false);
    }
    // A cluster (-la) or an option with its value attached (-n5). Letters after the
    // first are not judged: they may be a value this parser does not know about.
    let mut flags = Vec::new();
    for (position, letter) in letters.iter().enumerate() {
        let short = format!("-{letter}");
        let Some(&takes) = options.get(&short) else { break };
        flags.push(short);
        if takes {
            return (Some(flags), position + 1 == letters.len()); // value follows as the next word
        }
    }
    (Some(flags), false)
}

/// The program a simple command runs, then any program it runs in turn (sudo, xargs, find -exec).
fn walk(tokens: &[String], catalog: &Catalog, out: &mut Vec<Use>) {
    let (name, arguments) = head(tokens);
    if name.is_empty() {
        return;
    }
    let simple = |status| Use { program: name.to_string(), status, arguments: vec![], options: vec![] };
    if NOT_LINUX_PROGRAMS.contains(&name) {
        out.push(simple("not-linux"));
        return;
    }
    if !catalog.programs.contains(name) && !BUILTINS.contains(&name) {
        out.push(simple("unknown"));
        return;
    }
    if let Some(add_on) = catalog.unknown_subcommand(name, arguments) {
        out.push(Use { program: format!("{name} {add_on}"), status: "add-on", arguments: vec![], options: vec![] });
        return;
    }
    if let Some(verb) = catalog.unknown_verb(name, arguments) {
        out.push(Use { program: name.to_string(), status: "unknown-verb", arguments: vec![verb.to_string()], options: vec![] });
        return;
    }

    let mut nested: Vec<&[String]> = Vec::new();
    let mut used: Vec<(String, Option<Vec<String>>)> = Vec::new();
    let options = if TEXT_ARGUMENTS.contains(&name) { None } else { catalog.options_for(name, arguments) };
    let unchecked_from = catalog.subcommand_at(name, arguments);
    let mut operands = wrapper_operands(name);
    let mut i = 0;
    while i < arguments.len() {
        let token = arguments[i].as_str();
        if unchecked_from.is_some_and(|from| i >= from) {
            break;
        }
        if token == "--" {
            if WRAPPERS.contains(&name) {
                nested.push(&arguments[i + 1..]);
            }
            break;
        }
        if name == "find" && FIND_EXEC.contains(&token) {
            let end = (i + 1..arguments.len()).find(|&j| arguments[j] == SEMI || arguments[j] == "+").unwrap_or(arguments.len());
            let documented = options.as_ref().is_some_and(|o| o.contains_key(token));
            used.push((token.to_string(), Some(if documented { vec![token.to_string()] } else { vec![] })));
            nested.push(&arguments[i + 1..end]);
            i = end + 1;
            continue;
        }
        if token.starts_with('-') && !token.trim_matches('-').is_empty() {
            if let Some(options) = &options {
                let (flags, consumes) = resolve(token, options, name);
                used.push((token.split('=').next().unwrap().to_string(), flags));
                i += consumes as usize;
            }
        } else if WRAPPERS.contains(&name) && ASSIGNMENT.match_(token).is_none() {
            if operands > 0 {
                operands -= 1;
            } else {
                nested.push(&arguments[i..]);
                break;
            }
        }
        i += 1;
    }
    out.push(Use { program: name.to_string(), status: "known", arguments: arguments.to_vec(), options: used });
    for inner in nested {
        walk(inner, catalog, out);
    }
}

/// Every program in `command`, in order, with the options it is given. Err when the
/// line cannot be parsed (unbalanced quotes).
pub fn uses(command: &str, catalog: &Catalog) -> Result<Vec<Use>, &'static str> {
    let mut found = Vec::new();
    for tokens in simple_commands(command)? {
        walk(&tokens, catalog, &mut found);
    }
    Ok(found)
}

/// bash's complaint about the line, or None. `bash -n` parses without running anything.
pub fn bash_syntax_error(command: &str) -> Option<String> {
    let bash = which("bash")?;
    if bash.to_string_lossy().to_lowercase().contains("system32") {
        return None; // Windows' bash.exe would boot WSL
    }
    let mut child = Command::new(bash).arg("-n").stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().ok()?;
    child.stdin.take()?.write_all(command.as_bytes()).ok()?;
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if std::time::Instant::now() < deadline => std::thread::sleep(Duration::from_millis(5)),
            _ => {
                let _ = child.kill();
                return None;
            }
        }
    }
    let output = child.wait_with_output().ok()?;
    if output.status.success() {
        return None;
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    let lines = py::splitlines(py::strip(&stderr));
    Some(match lines.first() {
        Some(line) => BASH_LINE.sub(line, ""),
        None => "syntax error".to_string(),
    })
}

/// Everything found wrong with `command`. `syntax` also asks bash to parse it.
pub fn check(command: &str, catalog: &Catalog, syntax: bool) -> Vec<Issue> {
    let mut issues: Vec<Issue> = not_linux()
        .iter()
        .filter(|(pattern, _)| pattern.is_match(command))
        .map(|(_, reason)| Issue { program: String::new(), kind: "not-linux", detail: reason.to_string() })
        .collect();
    let found = match uses(command, catalog) {
        Ok(found) => found,
        Err(error) => {
            // unbalanced quotes
            issues.push(Issue { program: String::new(), kind: "syntax", detail: format!("cannot be parsed: {error}") });
            return issues;
        }
    };
    for use_ in found {
        let name = &use_.program;
        let issue = |kind, detail: String| Issue { program: name.clone(), kind, detail };
        match use_.status {
            "not-linux" => issues.push(issue("not-linux", format!("{name}: not a Linux command"))),
            "unknown" => issues.push(issue("unknown-program", format!("{name}: not a known Linux command"))),
            "add-on" => issues.push(issue("unknown-program", format!("{name}: not a standard command (no manual page)"))),
            "unknown-verb" => {
                let verb = use_.arguments.first().map(String::as_str).unwrap_or("");
                issues.push(issue("undocumented-option", format!("{name}: {verb} is not a documented command")))
            }
            _ => {}
        }
        for (written, flags) in &use_.options {
            if flags.is_none() {
                issues.push(issue("undocumented-option", format!("{name}: {written} is not a documented option")));
            }
        }
    }
    if syntax {
        if let Some(error) = bash_syntax_error(command) {
            issues.push(Issue { program: String::new(), kind: "syntax", detail: error });
        }
    }
    let mut unique: Vec<Issue> = Vec::new();
    for issue in issues {
        if !unique.contains(&issue) {
            unique.push(issue);
        }
    }
    unique
}

/// A catalogue from program names, (page, option title, description) entries,
/// documented example commands and one-line page summaries.
pub fn build_catalog<'a>(
    programs: impl IntoIterator<Item = &'a str>,
    option_titles: impl IntoIterator<Item = (&'a str, &'a str, &'a str)>,
    examples: impl IntoIterator<Item = &'a str>,
    summaries: impl IntoIterator<Item = (String, String)>,
) -> Catalog {
    let mut catalog = Catalog { summaries: summaries.into_iter().collect(), ..Catalog::default() };
    for name in programs {
        catalog.add_program(name);
    }
    for (page, title, text) in option_titles {
        catalog.add_program(page);
        catalog.add_option_title(page, title, text);
    }
    for example in examples {
        catalog.add_example(example);
    }
    catalog
}

/// What `validate` and `explain` need at answer time, so a command can be checked and
/// described without the raw documentation.
pub const CATALOG_SCHEMA: [&str; 4] = [
    "CREATE TABLE programs (name TEXT PRIMARY KEY, has_subcommands INTEGER NOT NULL) WITHOUT ROWID",
    "CREATE TABLE summaries (page TEXT PRIMARY KEY, summary TEXT NOT NULL) WITHOUT ROWID",
    "CREATE TABLE options (page TEXT NOT NULL, flag TEXT NOT NULL, takes_arg INTEGER NOT NULL, text TEXT NOT NULL, \
     PRIMARY KEY (page, flag)) WITHOUT ROWID",
    "CREATE TABLE subcommands (program TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (program, name)) WITHOUT ROWID",
];

/// Store the catalogue in the docs index, replacing any earlier copy.
pub fn save_catalog(db_path: &Path, catalog: &Catalog) -> rusqlite::Result<()> {
    let mut con = Connection::open(db_path)?;
    let tx = con.transaction()?;
    for table in ["programs", "summaries", "options", "subcommands"] {
        tx.execute(&format!("DROP TABLE IF EXISTS {table}"), [])?;
    }
    for statement in CATALOG_SCHEMA {
        tx.execute(statement, [])?;
    }
    {
        let mut programs: Vec<&String> = catalog.programs.iter().collect();
        programs.sort();
        let mut insert = tx.prepare("INSERT INTO programs VALUES (?, ?)")?;
        for name in programs {
            insert.execute(rusqlite::params![name, catalog.with_subcommands.contains(name) as i64])?;
        }
        let mut summaries: Vec<(&String, &String)> = catalog.summaries.iter().collect();
        summaries.sort();
        let mut insert = tx.prepare("INSERT INTO summaries VALUES (?, ?)")?;
        for (page, summary) in summaries {
            insert.execute(rusqlite::params![page, summary])?;
        }
        let mut pages: Vec<(&String, &HashMap<String, bool>)> = catalog.options.iter().collect();
        pages.sort_by(|a, b| a.0.cmp(b.0));
        let mut insert = tx.prepare("INSERT INTO options VALUES (?, ?, ?, ?)")?;
        for (page, flags) in pages {
            let mut flags: Vec<(&String, &bool)> = flags.iter().collect();
            flags.sort();
            for (flag, takes_argument) in flags {
                let text = catalog.texts.get(page).and_then(|t| t.get(flag)).map(String::as_str).unwrap_or("");
                insert.execute(rusqlite::params![page, flag, *takes_argument as i64, text])?;
            }
        }
        let mut verbs: Vec<(&String, &String)> =
            catalog.subcommands.iter().flat_map(|(program, verbs)| verbs.iter().map(move |verb| (program, verb))).collect();
        verbs.sort();
        let mut insert = tx.prepare("INSERT INTO subcommands VALUES (?, ?)")?;
        for (program, verb) in verbs {
            insert.execute(rusqlite::params![program, verb])?;
        }
    }
    tx.commit()
}

/// Open an index read-only, never creating it.
pub fn open_read_only(db_path: &Path) -> rusqlite::Result<Connection> {
    Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)
}

/// The catalogue stored in the docs index, or None if the index was built without one.
pub fn load_catalog(db_path: &Path) -> Option<Catalog> {
    let con = open_read_only(db_path).ok()?;
    let mut catalog = Catalog::default();
    let mut rows = con.prepare("SELECT name, has_subcommands FROM programs").ok()?;
    for row in rows.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))).ok()? {
        let (name, has_subcommands) = row.ok()?;
        if has_subcommands != 0 {
            catalog.with_subcommands.insert(name.clone());
        }
        catalog.programs.insert(name);
    }
    let mut rows = con.prepare("SELECT page, summary FROM summaries").ok()?;
    for row in rows.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).ok()? {
        let (page, summary) = row.ok()?;
        catalog.summaries.insert(page, summary);
    }
    let mut rows = con.prepare("SELECT page, flag, takes_arg, text FROM options").ok()?;
    let mapped = rows
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?, r.get::<_, String>(3)?)))
        .ok()?;
    for row in mapped {
        let (page, flag, takes_argument, text) = row.ok()?;
        if !text.is_empty() {
            catalog.texts.entry(page.clone()).or_default().insert(flag.clone(), text);
        }
        catalog.options.entry(page).or_default().insert(flag, takes_argument != 0);
    }
    // an index from before 0.6.1 has no subcommands table: no verbs are checked then
    if let Ok(mut rows) = con.prepare("SELECT program, name FROM subcommands") {
        for row in rows.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).ok()? {
            let (program, verb) = row.ok()?;
            catalog.add_subcommand(&program, &verb);
        }
    }
    Some(catalog)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tokens(command: &str) -> Vec<Vec<String>> {
        simple_commands(command).unwrap()
    }

    #[test]
    fn splits_into_simple_commands() {
        assert_eq!(tokens("ls -l | grep x && echo done"), vec![vec!["ls", "-l"], vec!["grep", "x"], vec!["echo", "done"]]);
        assert_eq!(tokens("echo $(date +%F) > out.txt"), vec![vec!["echo", "$"], vec!["date", "+%F"]]);
        assert_eq!(tokens("find . -name '*.log' -exec rm {} \\;"), vec![vec!["find", ".", "-name", "*.log", "-exec", "rm", "{}", SEMI]]);
        assert_eq!(tokens("echo $((1 + 2)) x"), vec![vec!["echo", "$", "x"]]);
        assert_eq!(tokens("cat <<EOF\nx\nEOF"), Vec::<Vec<String>>::new());
    }

    #[test]
    fn option_titles_parse() {
        assert_eq!(parse_option_title("-C, --directory=DIR"), vec![("-C".into(), true), ("--directory".into(), true)]);
        assert_eq!(parse_option_title("--[no-]color"), vec![("--color".into(), false), ("--no-color".into(), false)]);
        assert_eq!(parse_option_title("-ppattern or --pattern=pattern"),
                   vec![("-ppattern".into(), true), ("--pattern".into(), true), ("-p".into(), true)]);
    }

    #[test]
    fn verbs_outside_the_manual_are_flagged() {
        let mut catalog = build_catalog(["systemctl", "sudo"], [("systemctl", "-H, --host=", ""), ("systemctl", "--user", "")], [], []);
        let flagged = |catalog: &Catalog, command: &str| -> Vec<String> {
            check(command, catalog, false).into_iter().map(|issue| issue.detail).collect()
        };
        // no verbs in the index (built before 0.6.1): nothing to judge by
        assert!(flagged(&catalog, "systemctl inspect x").is_empty());
        for verb in ["status", "start", "stop", "restart", "enable", "list-units"] {
            catalog.add_subcommand("systemctl", verb);
        }
        assert_eq!(flagged(&catalog, "sudo systemctl inspect /etc/x"), vec!["systemctl: inspect is not a documented command"]);
        assert_eq!(flagged(&catalog, "systemctl -H host --user frobnicate"), vec!["systemctl: frobnicate is not a documented command"]);
        for fine in ["systemctl status nginx", "systemctl --user restart x", "systemctl -H host start x", "systemctl force-reload x",
                     "systemctl \"$action\" x", "systemctl", "systemctl --user"] {
            assert!(flagged(&catalog, fine).is_empty(), "{fine}");
        }
        let uses = uses("systemctl inspect x", &catalog).unwrap();
        assert_eq!((uses[0].program.as_str(), uses[0].status), ("systemctl", "unknown-verb"));
    }
}
