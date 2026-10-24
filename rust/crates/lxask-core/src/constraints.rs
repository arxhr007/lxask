//! Does a command keep what the task asked for?
//!
//! The validator checks that a command is well formed. This checks something it cannot:
//! that the literal details of the task survive into the command. "Delete .log files
//! older than 30 days under /var/log/myapp" names a file pattern, an age and a path; a
//! command without `log`, without a 30-day age or without that path has lost part of
//! the request, however valid it is.
//!
//! Only literals are checked, and each in the forms a command can write it (30 days is
//! `-mtime +30`, `-mmin +43200` or `--since '30 days ago'`). A command can still get the
//! meaning wrong with every literal in place. Plus a short list of known slips that are
//! valid but wrong (`ps ... | head -n 1` shows only the header).

use std::collections::{BTreeSet, HashSet};
use std::sync::OnceLock;

use crate::py::{self, escape, group, group_or_empty, Captures, LazyRe, Re};
use crate::{re, re_fn, shlex};

const WORD_NUMBERS: [(&str, i128); 15] = [
    ("one", 1), ("two", 2), ("three", 3), ("four", 4), ("five", 5), ("six", 6), ("seven", 7), ("eight", 8),
    ("nine", 9), ("ten", 10), ("eleven", 11), ("twelve", 12), ("fifteen", 15), ("twenty", 20), ("thirty", 30),
];

fn word_number(word: &str) -> Option<i128> {
    WORD_NUMBERS.iter().find(|(name, _)| *name == word).map(|&(_, n)| n)
}

fn number_words() -> String {
    WORD_NUMBERS.iter().map(|(name, _)| *name).collect::<Vec<_>>().join("|")
}

/// `(\d+|one|two|...)`
fn number() -> String {
    format!(r"(\d+|{})", number_words())
}

/// A number as Python keeps it: an int, or a float when written with a point. Equal
/// values are equal whatever their kind, as `30 == 30.0` in a Python set.
#[derive(Clone, Copy, Debug)]
pub enum Num {
    Int(i128),
    Float(f64),
}

impl Num {
    fn value(self) -> f64 {
        match self {
            Num::Int(n) => n as f64,
            Num::Float(f) => f,
        }
    }
}

impl PartialEq for Num {
    fn eq(&self, other: &Num) -> bool {
        match (*self, *other) {
            (Num::Int(a), Num::Int(b)) => a == b,
            (a, b) => a.value() == b.value(),
        }
    }
}

/// A set of numbers, deduplicated as a Python set would.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Nums(pub Vec<Num>);

impl Nums {
    fn add(&mut self, n: Num) {
        if !self.0.contains(&n) {
            self.0.push(n);
        }
    }
}

/// `_number`: a word number, a float when written with a point, else an int.
fn parse_number(token: &str) -> Option<Num> {
    let token = token.to_lowercase();
    if let Some(n) = word_number(&token) {
        return Some(Num::Int(n));
    }
    if token.contains('.') {
        token.parse::<f64>().ok().map(Num::Float)
    } else {
        token.parse::<i128>().ok().map(Num::Int)
    }
}

static WORD_NUMBER: LazyRe = re_fn!(format!(r"\b(?:{})\b", number_words()), "i");

/// How many minutes one of each unit is, for the forms an age or a span can take.
fn minutes_of(unit: &str) -> i128 {
    match unit.to_lowercase().as_str() {
        "minute" | "min" => 1,
        "hour" | "hr" => 60,
        "day" => 1440,
        "week" => 10080,
        "month" => 43200,
        other => panic!("unknown unit {other}"),
    }
}

static DURATION: LazyRe = re_fn!(format!(r"{}[\s-]*(minute|min|hour|hr|day|week|month)s?\b", number()), "i");
static SIZE: LazyRe = re!(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(kib|kb|k|mib|mb|m|gib|gb|g|tib|tb|t)\b", "i");
const UNIT_AFTER: &str =
    r"(?!\s*(?:minute|min|hour|hr|day|week|month|year|second|sec|kib|kb|mib|mb|gib|gb|k|m|g)s?\b)";
static COUNT: LazyRe = re_fn!(
    format!(
        r"\b(?:top|first|last|biggest|largest|smallest|newest|oldest)\s+{n}\b{UNIT_AFTER}|\b{n}\s+(?:largest|biggest|smallest|newest|oldest|entries|processes|results|most\s+(?:frequent|common|used|recent)|least\s+(?:frequent|common|used))\b",
        n = number()
    ),
    "i"
);
// "sizes such as 800K and 2G" gives examples, not a condition.
static EXAMPLES_BEFORE: LazyRe = re!(r"\b(?:like|such as|e\.g\.?|for example|values)\s+(?:\S+\s+){0,3}$", "i");
// A command that works on a placeholder (`kill -TERM PID`) has already narrowed the
// task down to that object: the task's port or file name described how it was found.
static PLACEHOLDER: LazyRe = re!(r"\b(?:PID|PIDS|PID_LIST|PROCESS_ID|UNIT|RULE_NUMBER)\b|\bprocess_id\b");
static PORT: LazyRe = re!(r"\bport\s+(\d{2,5})\b", "i");
// An extension used to pick files: `.log files`, `*.tmp`, "ending in .conf". Not "into a
// .gz file", which the compressor names by itself.
const EXT: &str = r"\.([A-Za-z][A-Za-z0-9]{0,5}(?:\.[A-Za-z0-9]{1,4})?)\b(?![/\w])";
static EXTENSION: LazyRe = re_fn!(
    format!(r#"\*{EXT}|(?:^|(?<=[\s'"(`])){EXT}['"`]?\s+files\b|\b(?:ending|end)\s+(?:in|with)\s+['"`]?{EXT}"#),
    "i"
);
const FILE_TYPES: &str = "log|txt|csv|tsv|json|yaml|yml|conf|cfg|ini|sh|py|tar|gz|tgz|zip|bz2|xz|tmp|bak|md|html|xml|\
sql|pdf|jpg|jpeg|png|iso|deb|rpm|service|timer|socket|mp4|mp3|part|pem|key|crt|db|sqlite|sha256";
static FILENAME: LazyRe =
    re_fn!(format!(r"(?<![\w/.~$-])([\w-]+(?:\.[\w-]+)*\.(?:{FILE_TYPES}))\b(?![/\w])"), "i");
static HOME: LazyRe =
    re!(r"\b(?:in|under|inside|within|from|across)\s+(?:my|your)\s+home\s+(?:folder|directory|dir)\b", "i");
static PATH: LazyRe = re!(r"(?<![\w.:$~/-])((?:/|~/)[\w.@-]+(?:/[\w.@*-]+)*)/?");
static QUOTED: LazyRe = re!(
    "\\b(?:containing|contain|contains|matching|match|matches|named|called|word|text|string|pattern|\
excluding|exclude|excludes|without)\\s+(?:the\\s+(?:word|text|string)\\s+)?\
(?:'([^']{2,40})'|\"([^\"]{2,40})\"|\u{201c}([^\u{201d}]{2,40})\u{201d}|`([^`]{2,40})`)",
    "i"
);

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Miss {
    /// duration | size | count | port | extension | filename | path | home | text | forbidden | slip
    pub kind: &'static str,
    pub detail: String,
}

impl Miss {
    fn slip(detail: impl Into<String>) -> Miss {
        Miss { kind: "slip", detail: detail.into() }
    }
}

/// What a literal is looked for as.
#[derive(Clone, Debug, PartialEq)]
pub enum Wanted {
    Numbers(Nums),
    Text(String),
    Nothing,
}

/// (kind, as written in the task, what to look for)
pub type Requirement = (&'static str, String, Wanted);

fn has_number(command: &str, values: &Nums) -> bool {
    for &value in &values.0 {
        let integer = match value {
            Num::Float(f) if f != f.trunc() => {
                let pattern = format!(r"(?<![\d.]){}(?![\d])", escape(&py::float_str(f)));
                if Re::plain(&pattern).is_match(command) {
                    return true;
                }
                continue;
            }
            Num::Float(f) => f as i128,
            Num::Int(n) => n,
        };
        if Re::plain(&format!(r"(?<![\d.]){integer}(?![\d])")).is_match(command) {
            return true;
        }
    }
    false
}

/// 30 days -> {30, 720, 43200, 2592000, ...}: the number in each unit a command might use.
fn duration_forms(amount: i128, unit: &str) -> Nums {
    let minutes = amount * minutes_of(unit);
    let mut forms = Nums::default();
    for n in [amount, minutes, minutes * 60] {
        forms.add(Num::Int(n));
    }
    let unit = unit.to_lowercase();
    if matches!(unit.as_str(), "day" | "week" | "month") {
        // find counts whole days down: "at least 7 days old" is `-mtime +6`
        forms.add(Num::Int(minutes.div_euclid(1440) - 1));
    }
    for size in [60, 1440, 10080] {
        if minutes.rem_euclid(size) == 0 {
            forms.add(Num::Int(minutes.div_euclid(size)));
        }
    }
    if unit == "month" {
        for n in [amount * 30, amount * 31, amount * 4] {
            forms.add(Num::Int(n));
        }
    }
    forms
}

/// 100 MB -> {100, 102400, 104857600, ...}; 1.5G -> {1.5, 1536}.
fn size_forms(amount: f64, unit: &str) -> Nums {
    let mut forms = Nums::default();
    forms.add(Num::Float(amount));
    let power = match unit.chars().next().unwrap().to_ascii_lowercase() {
        'k' => 1,
        'm' => 2,
        'g' => 3,
        _ => 4,
    };
    for base in [1024i128, 1000] {
        for down in 1..=power {
            let converted = amount * base.pow(down) as f64;
            if converted == converted.trunc() && converted.abs() < 1e38 {
                forms.add(Num::Int(converted as i128));
            }
        }
    }
    forms
}

fn path_present(path: &str, command: &str) -> bool {
    let path = path.trim_end_matches('/');
    let mut candidates = vec![path.to_string()];
    if let Some(rest) = path.strip_prefix("~/") {
        candidates.extend([format!("$HOME/{rest}"), format!("${{HOME}}/{rest}"), rest.to_string()]);
        // ~/backups may be written out as /home/<user>/backups
        if Re::plain(&format!(r"/home/[\w.-]+/{}(?![\w.-])", escape(rest))).is_match(command) {
            return true;
        }
    }
    if candidates.iter().any(|c| Re::plain(&format!(r"{}(?![\w.-])", escape(c))).is_match(command)) {
        return true;
    }
    // `cd /home/alex && zip ... project` and `tar -C /home/alex project`: the directory
    // and the name are given apart.
    let (parent, _, name) = py::rpartition(path, "/");
    !parent.is_empty()
        && !name.is_empty()
        && path_present(if parent.is_empty() { "/" } else { parent }, command)
        && Re::plain(&format!(r"(?<![\w.-]){}(?![\w-])", escape(name))).is_match(command)
}

/// (kind, as written in the task, what to look for) for each literal in the task.
pub fn requirements(task: &str) -> Vec<Requirement> {
    let mut found: Vec<Requirement> = Vec::new();
    for caps in DURATION.finditer(task) {
        if let Some(Num::Int(amount)) = parse_number(group_or_empty(&caps, 1)) {
            if amount > 1 {
                // "1 hour" is too easily matched by chance to be worth checking
                found.push((
                    "duration",
                    group_or_empty(&caps, 0).to_string(),
                    Wanted::Numbers(duration_forms(amount, group_or_empty(&caps, 2))),
                ));
            }
        }
    }
    for caps in SIZE.finditer(task) {
        let start = caps.get(0).unwrap().start();
        if !EXAMPLES_BEFORE.is_match(&task[..start]) {
            let amount: f64 = group_or_empty(&caps, 1).parse().unwrap_or(0.0);
            found.push((
                "size",
                group_or_empty(&caps, 0).to_string(),
                Wanted::Numbers(size_forms(amount, group_or_empty(&caps, 2))),
            ));
        }
    }
    for caps in COUNT.finditer(task) {
        let token = group(&caps, 1).filter(|t| !t.is_empty()).or(group(&caps, 2)).unwrap_or("");
        if let Some(Num::Int(amount)) = parse_number(token) {
            if amount > 1 {
                let mut forms = Nums::default();
                forms.add(Num::Int(amount));
                forms.add(Num::Int(amount + 1)); // +1: a header line kept
                found.push(("count", group_or_empty(&caps, 0).to_string(), Wanted::Numbers(forms)));
            }
        }
    }
    for caps in PORT.finditer(task) {
        if let Ok(port) = group_or_empty(&caps, 1).parse::<i128>() {
            found.push(("port", group_or_empty(&caps, 0).to_string(), Wanted::Numbers(Nums(vec![Num::Int(port)]))));
        }
    }
    for caps in EXTENSION.finditer(task) {
        let extension = (1..caps.len()).filter_map(|i| group(&caps, i)).find(|g| !g.is_empty()).unwrap_or("");
        found.push(("extension", format!(".{extension}"), Wanted::Text(extension.to_string())));
    }
    let mut filenames: HashSet<String> = HashSet::new();
    for caps in FILENAME.finditer(task) {
        let start = caps.get(0).unwrap().start();
        let before = &task[..start];
        if !(before.ends_with('.') || before.ends_with('/')) {
            let name = group_or_empty(&caps, 1).to_string();
            filenames.insert(name.clone());
            found.push(("filename", name.clone(), Wanted::Text(name)));
        }
    }
    for caps in PATH.finditer(task) {
        let path = py::rstrip_chars(group_or_empty(&caps, 1), ".,;:"); // the sentence's own punctuation
        if py::char_len(path) > 2 && !filenames.contains(path) {
            found.push(("path", path.to_string(), Wanted::Text(path.to_string())));
        }
    }
    if let Some(caps) = HOME.search(task) {
        if !found.iter().any(|(kind, _, _)| *kind == "path") {
            found.push(("home", group_or_empty(&caps, 0).to_string(), Wanted::Nothing));
        }
    }
    for caps in QUOTED.finditer(task) {
        let text = (1..caps.len()).filter_map(|i| group(&caps, i)).find(|g| !g.is_empty()).unwrap_or("");
        found.push(("text", text.to_string(), Wanted::Text(text.to_string())));
    }
    found
}

static HEAD_ONE: LazyRe = re!(r"head\s+(?:-n\s*1|-1|--lines=1)");
static PS_HEADERLESS: LazyRe = re!(r"--no-headers?\b|\bh\b|=[,\s]|=$|-o\s*\S*=(?:\s|$)");
static PS: LazyRe = re!(r"(?:sudo\s+)?ps\b");

/// `ps ... | head -n 1` with ps printing its header: the one line kept is the header.
fn ps_head_one(first: &str, second: &str) -> bool {
    PS.match_(first).is_some() && HEAD_ONE.fullmatches(second) && !PS_HEADERLESS.is_match(first)
}

// grep given a directory without -r prints "Is a directory" and searches nothing. Only
// file operands that are surely directories count: well-known system ones, and paths
// written with a trailing slash.
static GREP: LazyRe = re!(r"(?:^|(?<=[|;&(]))(\s*(?:sudo\s+)?[ez]?grep)\b([^|;&)]*)");
static GREP_RECURSIVE: LazyRe =
    re!(r"(?:^|\s)-[a-zA-Z]*[rR]|--recursive|--dereference-recursive|-d\s*recurse|--directories");
static KNOWN_DIR: LazyRe = re!(r"/var/log|/etc|/var|/var/lib|/var/www|/home|/srv|/opt|/usr|/tmp|/root");
const GREP_SHORT_WITH_VALUE: &str = "efmABCdD";
const GREP_LONG_WITH_VALUE: [&str; 12] = [
    "--regexp", "--file", "--max-count", "--context", "--after-context", "--before-context", "--include",
    "--exclude", "--exclude-dir", "--label", "--devices", "--binary-files",
];

/// The file operands of a grep command line (the pattern and option values left out).
fn grep_files(args: &str) -> Vec<String> {
    let tokens = shlex::split(args).unwrap_or_else(|_| py::split_ws(args).into_iter().map(String::from).collect());
    let mut positional = Vec::new();
    let mut pattern_given = false;
    let mut skip = false;
    for token in tokens {
        if skip {
            skip = false;
        } else if token.starts_with("--") {
            let name = token.split('=').next().unwrap();
            pattern_given |= name == "--regexp" || name == "--file";
            skip = !token.contains('=') && GREP_LONG_WITH_VALUE.contains(&name);
        } else if token.starts_with('-') && py::char_len(&token) > 1 {
            let letters = &token[1..];
            pattern_given |= letters.contains('e') || letters.contains('f');
            skip = GREP_SHORT_WITH_VALUE.contains(token.chars().last().unwrap());
        } else {
            positional.push(token);
        }
    }
    if pattern_given {
        positional
    } else {
        positional.into_iter().skip(1).collect()
    }
}

fn grep_on_directory(args: &str) -> bool {
    if GREP_RECURSIVE.is_match(args) {
        return false;
    }
    grep_files(args).iter().any(|path| path.ends_with('/') || KNOWN_DIR.fullmatches(path))
}

// "shut down in 10 minutes" answered with `shutdown --reboot +10`, and the reverse.
static SHUT_DOWN: LazyRe = re!(r"\b(?:shut\s*down|power\s*off|turn\s+off|halt)\b", "i");
static RESTART: LazyRe = re!(r"\b(?:reboot|restart)", "i");
static POWER_COMMAND: LazyRe =
    re!(r"(?:^|(?<=[|;&(]))\s*(?:sudo\s+)?(reboot|poweroff|halt|shutdown|systemctl)\b([^|;&)]*)");
static SHUTDOWN_CANCEL: LazyRe = re!(r"(?:^|\s)-\w*c\b");
static SHUTDOWN_REBOOT: LazyRe = re!(r"(?:^|\s)-\w*r|--reboot\b");
static REBOOT_FLAG: LazyRe = re!(r"--reboot\b");
static POWEROFF_FLAG: LazyRe = re!(r"--halt\b|--poweroff\b|(?:^|\s)-p\b");

/// "reboot" or "poweroff" for a command that does one of them, else None. reboot,
/// poweroff and halt are one program whose flags pick the action.
fn power_action(command: &str) -> Option<&'static str> {
    for caps in POWER_COMMAND.finditer(command) {
        let program = group_or_empty(&caps, 1);
        let args = group_or_empty(&caps, 2);
        return match program {
            "systemctl" => {
                let words = py::split_ws(args);
                if words.contains(&"reboot") {
                    Some("reboot")
                } else if words.contains(&"poweroff") || words.contains(&"halt") {
                    Some("poweroff")
                } else {
                    continue; // another systemctl call: look further
                }
            }
            "shutdown" => {
                if SHUTDOWN_CANCEL.is_match(args) {
                    None // cancels a pending shutdown
                } else if SHUTDOWN_REBOOT.is_match(args) {
                    Some("reboot")
                } else {
                    Some("poweroff")
                }
            }
            _ if REBOOT_FLAG.is_match(args) => Some("reboot"),
            _ if POWEROFF_FLAG.is_match(args) => Some("poweroff"),
            "reboot" => Some("reboot"),
            _ => Some("poweroff"),
        };
    }
    None
}

// pip, npm and gem list one language's packages; "list installed packages" on its own
// means the system's (dpkg, rpm, apt, dnf).
static LANGUAGE_LISTING: LazyRe = re!(r"^\s*(?:sudo\s+)?(pip3?|python3?\s+-m\s+pip|npm|gem)\s+(?:list|freeze|ls)\b");
static INSTALLED: LazyRe = re!(r"\b(?:installed|upgrad\w*|outdated|up[\s-]to[\s-]date)\b", "i");
static LANGUAGE_WORDS: LazyRe = re!(
    r"\b(?:python|pip|pypi|venv|virtualenv|virtual\s+environment|requirements|wheels?|editable|freeze|site-packages|user[\s-]site|node|npm|javascript|js|ruby|gems?)\b",
    "i"
);
static PACKAGES_WORD: LazyRe = re!(r"\bpackages?\b", "i");
static POWEROFF_OBJECT: LazyRe = re!(r"\b(?:computer|system|machine|server|host)\b|^\s*reboot", "i");
static LIST_SEPARATORS: LazyRe = re!(r"[;&]+");
// "show which files can safely be removed" answered with `find / -delete`: a task that
// only asks to look may not get a command that deletes. "list them and delete them" may.
static LOOK_ONLY: LazyRe = re!(
    r"^\s*(?:please\s+)?(?:show|list|display|print|identify|which|what|check|inspect|count|report|view|tell|preview)\b",
    "i"
);
static THEN_DELETES: LazyRe = re!(
    r"\b(?:and|then|before|after)\s+(?:also\s+)?(?:delete|remove|rm|erase|wipe|purge|clean|truncate|empty|shred|unlink)\b|,\s*(?:delete|remove)\b",
    "i"
);
static DELETES: LazyRe = re!(r"(?:^|\s)-delete\b");
static ONLY_ASKS: LazyRe = re!(r"(?:^|\s)--(?:help|version)\b");
const DELETING_PROGRAMS: [&str; 4] = ["rm", "rmdir", "unlink", "shred"];

fn deletes(command: &str) -> bool {
    !ONLY_ASKS.is_match(command)
        && (DELETES.is_match(command)
            || crate::validate::programs_run(command).iter().any(|p| DELETING_PROGRAMS.contains(&p.to_lowercase().as_str())))
}

fn slips(task: &str, command: &str, request: &str) -> Vec<Miss> {
    let mut misses = Vec::new();
    for part in LIST_SEPARATORS.split(command, 0) {
        let stages: Vec<&str> = part.split('|').map(py::strip).collect();
        for pair in stages.windows(2) {
            if ps_head_one(pair[0], pair[1]) {
                misses.push(Miss::slip("ps prints a header line first: `head -n 1` shows only the header"));
            }
        }
    }
    if GREP.finditer(command).iter().any(|caps| grep_on_directory(group_or_empty(caps, 2))) {
        misses.push(Miss::slip("grep searches files, not directories: a directory needs -r"));
    }
    if LOOK_ONLY.is_match(task) && !THEN_DELETES.is_match(task) && deletes(command) {
        misses.push(Miss::slip("the request only asks to look; this command deletes"));
    }
    match power_action(command) {
        Some("reboot") if SHUT_DOWN.is_match(task) && !RESTART.is_match(task) => {
            misses.push(Miss::slip("the request says shut down; this command reboots"))
        }
        Some("poweroff")
            if RESTART.is_match(task) && !SHUT_DOWN.is_match(task) && POWEROFF_OBJECT.is_match(task) =>
        {
            misses.push(Miss::slip("the request says reboot; this command powers off"))
        }
        _ => {}
    }
    if let Some(listing) = LANGUAGE_LISTING.match_(command) {
        if PACKAGES_WORD.is_match(task) && INSTALLED.is_match(task) && !LANGUAGE_WORDS.is_match(task) {
            let name = py::split_ws(group_or_empty(&listing, 1))[0];
            misses.push(Miss::slip(format!(
                "{name} lists one language's packages; the system's are listed by dpkg -l or rpm -qa"
            )));
        }
    }
    if IN_HOME.is_match(command) && !HOME_WORDS.is_match(task) && !HOME_WORDS.is_match(request) {
        misses.push(Miss::slip("the command looks in the home directory; the request does not say home"));
    }
    if let Some((program, family)) = foreign_manager(command, &format!("{task} {request}")) {
        misses.push(Miss::slip(format!(
            "{program} is not installed here; this system's packages are managed by {}",
            family_tool(family)
        )));
    }
    misses
}

// A package manager of another distribution: `yum list installed` on Ubuntu. The
// machine's own families are found once (LXASK_PACKAGE_MANAGERS, a comma-separated list
// of programs, stands in for the search: tests, evaluation, containers). A request that
// names a manager or a distribution chose it itself.
const FAMILIES: [(&str, &[&str]); 5] = [
    ("debian", &["apt-get", "apt", "dpkg", "aptitude"]),
    ("redhat", &["dnf", "yum", "rpm", "microdnf"]),
    ("arch", &["pacman"]),
    ("suse", &["zypper"]),
    ("alpine", &["apk"]),
];
// The program whose presence says a family is this system's: rpm or dpkg alone can be
// installed anywhere as a tool for the other kind of package.
const PRIMARY: [(&str, &[&str]); 5] = [
    ("debian", &["apt-get"]),
    ("redhat", &["dnf", "yum"]),
    ("arch", &["pacman"]),
    ("suse", &["zypper"]),
    ("alpine", &["apk"]),
];

fn family_tool(family: &str) -> &'static str {
    match family {
        "debian" => "apt and dpkg",
        "redhat" => "dnf and rpm",
        "arch" => "pacman",
        "suse" => "zypper",
        _ => "apk",
    }
}

fn manager_programs() -> impl Iterator<Item = &'static str> {
    FAMILIES.iter().flat_map(|(_, programs)| programs.iter().copied())
}

fn family_of(program: &str) -> Option<&'static str> {
    FAMILIES.iter().find(|(_, programs)| programs.contains(&program)).map(|(family, _)| *family)
}

fn managers_alternation() -> String {
    manager_programs().collect::<Vec<_>>().join("|")
}

static MANAGER: LazyRe =
    re_fn!(format!(r"(?:^|(?<=[|;&(]))\s*(?:sudo\s+(?:-\S+\s+)*)?({})(?![\w.-])", managers_alternation()), "m");
static NAMES_A_DISTRO: LazyRe = re_fn!(
    format!(
        r"\b(?:{}|debian|ubuntu|mint|fedora|rhel|red\s*hat|centos|rocky|alma(?:linux)?|amazon\s+linux|arch|manjaro|endeavour\w*|suse|opensuse|alpine|deb|rpms?|distros?|distributions?|docker\w*|containers?|images?)\b",
        managers_alternation()
    ),
    "i"
);

fn present_programs() -> BTreeSet<String> {
    if let Some(given) = crate::setting("PACKAGE_MANAGERS") {
        return given.split(',').map(str::trim).filter(|s| !s.is_empty()).map(String::from).collect();
    }
    found_programs().clone()
}

fn found_programs() -> &'static BTreeSet<String> {
    static FOUND: OnceLock<BTreeSet<String>> = OnceLock::new();
    FOUND.get_or_init(|| manager_programs().filter(|p| which(p).is_some()).map(String::from).collect())
}

/// `shutil.which`: the first executable `name` on PATH.
pub fn which(name: &str) -> Option<std::path::PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(name);
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }
    None
}

#[cfg(unix)]
fn is_executable(path: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path).map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable(path: &std::path::Path) -> bool {
    path.is_file()
}

fn families() -> Vec<&'static str> {
    let present = present_programs();
    PRIMARY
        .iter()
        .filter(|(_, primary)| primary.iter().any(|p| present.contains(*p)))
        .map(|(family, _)| *family)
        .collect()
}

/// (program, this system's family) for a package manager the system does not use.
fn foreign_manager(command: &str, request: &str) -> Option<(String, &'static str)> {
    let families = families();
    if families.len() != 1 || NAMES_A_DISTRO.is_match(request) {
        return None;
    }
    for caps in MANAGER.finditer(command) {
        let program = group_or_empty(&caps, 1);
        if family_of(program) != Some(families[0]) {
            return Some((program.to_string(), families[0]));
        }
    }
    None
}

// Listing the installed packages has one form per family, so a foreign one has one fix.
static LIST_INSTALLED: LazyRe = re!(
    r"(?:^|(?<=[|;&(]))(\s*)(?:sudo\s+)?(?:(?:dnf|yum)\s+list\s+(?:--)?installed|rpm\s+-qa|pacman\s+-Q|apt\s+list\s+--installed|dpkg\s+(?:-l|--list)|apk\s+info|zypper\s+(?:se|search)\s+(?:-i|--installed-only))(?=\s*(?:$|[|;&)]))",
    "m"
);

fn listing_of(family: &str) -> &'static str {
    match family {
        "debian" => "dpkg -l",
        "redhat" => "rpm -qa",
        "arch" => "pacman -Q",
        "suse" => "zypper se --installed-only",
        _ => "apk info",
    }
}

static LEADING_PROGRAM: LazyRe = re!(r"\s*(?:sudo\s+)?(\S+)");

// So has searching the repositories by name; the search terms carry over.
static SEARCH: LazyRe = re!(
    r"(?:^|(?<=[|;&(]))(\s*)(?:sudo\s+)?(?:(dnf|yum)\s+search|(pacman)\s+-Ss|(apt|apt-cache)\s+search|(zypper)\s+(?:se|search)(?!\s+-i\b|\s+--installed-only)|(apk)\s+search)(?=\s+[^\s|;&)-])",
    "m"
);

fn search_of(family: &str) -> &'static str {
    match family {
        "debian" => "apt search",
        "redhat" => "dnf search",
        "arch" => "pacman -Ss",
        "suse" => "zypper search",
        _ => "apk search",
    }
}

fn search_family(program: &str) -> &'static str {
    match program {
        "dnf" | "yum" => "redhat",
        "pacman" => "arch",
        "apt" | "apt-cache" => "debian",
        "zypper" => "suse",
        _ => "alpine",
    }
}

fn listing_repair(command: &str, request: &str) -> String {
    let families = families();
    if families.len() != 1 || NAMES_A_DISTRO.is_match(request) {
        return command.to_string();
    }
    let family = families[0];
    let command = LIST_INSTALLED.sub_with(command, 0, |caps: &Captures| {
        let whole = group_or_empty(caps, 0);
        let program = LEADING_PROGRAM.match_(whole).map(|m| group_or_empty(&m, 1).to_string()).unwrap_or_default();
        if family_of(&program) == Some(family) {
            whole.to_string()
        } else {
            format!("{}{}", group_or_empty(caps, 1), listing_of(family))
        }
    });
    SEARCH.sub_with(&command, 0, |caps: &Captures| {
        let program = (2..caps.len()).filter_map(|i| group(caps, i)).find(|g| !g.is_empty()).unwrap_or("");
        if search_family(program) == family {
            group_or_empty(caps, 0).to_string()
        } else {
            format!("{}{}", group_or_empty(caps, 1), search_of(family))
        }
    })
}

// Commands that always need root: package changes, disks and file systems, system
// services, users and groups, the firewall. tldr writes them with sudo, and without it
// they fail with "permission denied" for the user who asked.
static NEEDS_ROOT: LazyRe = re!(
    r"(?:^|(?<=[|;&(]))(\s*)(?=(?:(?:apt|apt-get|aptitude)\s+(?:-\S+\s+)*(?:install|reinstall|remove|purge|update|upgrade|full-upgrade|dist-upgrade|autoremove)\b|(?:dnf|yum)\s+(?:-\S+\s+)*(?:install|reinstall|remove|erase|update|upgrade|autoremove|downgrade)\b|pacman\s+-(?:S(?![a-z]*[sigl])|R|U)|zypper\s+(?:-\S+\s+)*(?:in|install|rm|remove|up|update|dup|dist-upgrade|ref|refresh)\b|apk\s+(?:add|del|update|upgrade)\b|snap\s+(?:install|remove|refresh)\b|(?:fsck|mkfs)(?:\.\w+)?\s|fdisk\s|parted\s|umount\s|mount\s+(?:-a\b|[^\s|;&)]+\s+[^\s|;&)]+)|systemctl\s+(?![^|;&)]*--user)(?:-\S+\s+)*(?:start|stop|restart|reload|enable|disable|mask|unmask)\b|(?:useradd|userdel|usermod|groupadd|groupdel|modprobe|iptables|ufw)\s|visudo\b))",
    "m"
);
// Already root, or no sudo to be had: a container, a chroot, "without sudo".
static ROOT_CONTEXT: LazyRe = re!(
    r"\b(?:as|am|i'?m|being)\s+(?:the\s+)?root\b|\broot\s+(?:shell|user|account|prompt)\b|\b(?:without|no)\s+sudo\b|\bdoas\b|\bdocker\w*|\bcontainers?\b|\bchroot\b",
    "i"
);
static OTHER_ELEVATION: LazyRe = re!(r"\b(?:doas|pkexec|su\s+-c)\b");

fn as_root() -> bool {
    if let Some(given) = crate::setting("AS_ROOT") {
        return given == "1";
    }
    #[cfg(unix)]
    {
        // SAFETY: geteuid has no preconditions and cannot fail.
        unsafe { libc_geteuid() == 0 }
    }
    #[cfg(not(unix))]
    {
        false
    }
}

#[cfg(unix)]
extern "C" {
    #[link_name = "geteuid"]
    fn libc_geteuid() -> u32;
}

fn sudo_repair(command: &str, request: &str) -> String {
    if as_root() || OTHER_ELEVATION.is_match(command) || ROOT_CONTEXT.is_match(request) {
        return command.to_string();
    }
    NEEDS_ROOT.sub_with(command, 0, |caps: &Captures| format!("{}sudo ", group_or_empty(caps, 1)))
}

// "the data directory" answered with "$HOME/data": a guess that only works from home.
// (awk's `$1 ~ /re/` is a match, not a home directory)
static IN_HOME: LazyRe = re!(r#"\$\{?HOME\b|(?:^|(?<=[\s'"=:(]))~(?:/|(?=\s+-|\s*$|\s*[;|&)]))"#);
static HOME_WORDS: LazyRe = re!(
    r"\bhome\b|~|\$\{?HOME\b|\b(?:my|mine|me|user'?s?|dotfiles?|profile|desktop|downloads|documents)\b|\.\w+rc\b",
    "i"
);

// A crontab entry (`0 3 * * * script.sh`) is not a command: run as one, it tries to
// execute a program called `0`. Installing it into the user's crontab is.
const CRON_FIELD: &str = r"(?:\*|\d+)(?:[-,/](?:\*|\d+))*";
static CRON_LINE: LazyRe = re_fn!(
    format!(r"^(?:{CRON_FIELD}\s+){{4}}{CRON_FIELD}\s+\S.*$|^@(?:reboot|yearly|annually|monthly|weekly|daily|midnight|hourly)\s+\S.*$"),
    ""
);

// tldr writes its placeholders as path/to/<name>; when the task names <name> itself
// ("delete the directory build"), the placeholder prefix is a leftover.
static PLACEHOLDER_PATH: LazyRe = re!(r"(?<![\w.$~-])/?path/to/([\w.-]+)/?(?![\w/.-])");
// tldr's generic placeholder names, which any task may use as ordinary words
const GENERIC_NAMES: [&str; 18] = [
    "file", "files", "directory", "directories", "dir", "folder", "path", "source", "target", "destination",
    "archive", "output", "input", "script", "program", "command", "image", "device",
];

/// Whether the task names `name` as a file or directory of its own: a file name
/// (requirements.txt), or "the directory build" / "the build directory". Not "set a
/// default shell", whose placeholder /path/to/shell stands for a path to find out.
fn names_path(task: &str, name: &str) -> bool {
    if GENERIC_NAMES.contains(&name.to_lowercase().as_str()) {
        return false;
    }
    let word = escape(name);
    if name.trim_matches('.').contains('.') {
        return Re::new(&format!(r"(?<![\w/.-]){word}(?![\w/-])"), "i").is_match(task);
    }
    let kind = r"(?:directory|folder|dir|file)";
    Re::new(&format!(r#"\b{kind}\s+(?:called\s+|named\s+)?['"`]?{word}\b|\b{word}['"`]?\s+{kind}\b"#), "i").is_match(task)
}

// "changed in the last hour" answered with `find -mtime 0` or `-mtime -1`: -mtime counts
// whole days, so that is the last 24 hours. The minute form is the one right fix.
static HOURS_WINDOW: LazyRe =
    re_fn!(format!(r"\b(?:last|past|previous)\s+(?:{}\s+)?(hour|hr|minute|min)s?\b", number()), "i");
static DAY_AGE: LazyRe = re!(r"-([acm])time\s+(?:0|-0|-1)(?![\d])");
static LONGER_UNITS: LazyRe = re!(r"\b(?:day|week|month|year)s?\b", "i");
static FIND: LazyRe = re!(r"(?:^|[|;&(]\s*)(?:sudo\s+)?find\b");
static MINUTE_AGE: LazyRe = re!(r"-[acm]min\b");

fn hours_repair(command: &str, task: &str) -> String {
    let windows = HOURS_WINDOW.finditer(task);
    if windows.len() != 1 || LONGER_UNITS.is_match(task) {
        return command.to_string();
    }
    let amount = match group(&windows[0], 1) {
        Some(token) if !token.is_empty() => match parse_number(token) {
            Some(Num::Int(n)) => n,
            _ => return command.to_string(),
        },
        _ => 1,
    };
    let minutes = amount * minutes_of(group_or_empty(&windows[0], 2));
    if !FIND.is_match(command) || MINUTE_AGE.is_match(command) {
        return command.to_string();
    }
    DAY_AGE.sub_with(command, 0, |caps: &Captures| format!("-{}min -{minutes}", group_or_empty(caps, 1)))
}

// "the last 2 lines" answered with a bare `tail`, which prints 10.
static LINES: LazyRe = re_fn!(format!(r"\b(first|top|last)\s+{}\s+lines?\b", number()), "i");

fn bare_head_tail(program: &str) -> String {
    format!(r"(?:^|(?<=[|;&(]))(\s*{program})\b(?![^|;&)]*(?:\s-n|\s-\d|--lines|\s-c\b|--bytes))")
}

static HOME_IN_COMMAND: LazyRe = re!(r"~|\$\{?HOME\b|/home/|/root\b");
static FIND_HERE: LazyRe = re!(r"(?<![\w-])((?:sudo\s+)?find)\s+\.(?=\s|$)");
static REGEX_CLASS_OF_ONE: LazyRe = re!(r"\[(.)\]");

/// Fix the known slips that have one right fix: `ps ... | head -n 1` keeps the header
/// and the first process with `head -n 2`; `grep` on a directory gets `-r`; a bare
/// crontab entry is installed with `crontab`. With the task: a tldr placeholder
/// `path/to/build` for the "build" the task names becomes `build`, a bare `head` or
/// `tail` gets the task's "first N lines" or "last N lines", `find -mtime 0` for "the
/// last hour" becomes `-mmin -60`, another distribution's listing of the installed
/// packages becomes this system's, and a command that always needs root gets `sudo`
/// (unless lxask runs as root).
pub fn repair(command: &str, task: &str) -> String {
    let stripped = py::strip(command);
    if !stripped.contains('\n') && CRON_LINE.match_(stripped).is_some() {
        let entry = stripped.replace('\'', "'\\''");
        return format!("(crontab -l 2>/dev/null; echo '{entry}') | crontab -");
    }
    let mut stages: Vec<String> = command.split('|').map(String::from).collect();
    for i in 0..stages.len().saturating_sub(1) {
        let before = py::strip(&stages[i]);
        let before = before.rsplit_once(';').map_or(before, |(_, last)| last);
        let before = before.rsplit_once("&&").map_or(before, |(_, last)| last);
        if ps_head_one(py::strip(before), py::strip(&stages[i + 1])) {
            stages[i + 1] = HEAD_ONE.sub(&stages[i + 1], "head -n 2");
        }
    }
    let command = stages.join("|");
    let mut command = GREP.sub_with(&command, 0, |caps: &Captures| {
        if grep_on_directory(group_or_empty(caps, 2)) {
            format!("{} -r{}", group_or_empty(caps, 1), group_or_empty(caps, 2))
        } else {
            group_or_empty(caps, 0).to_string()
        }
    });
    if !task.is_empty() {
        command = PLACEHOLDER_PATH.sub_with(&command, 0, |caps: &Captures| {
            let name = group_or_empty(caps, 1);
            if names_path(task, name) { name.to_string() } else { group_or_empty(caps, 0).to_string() }
        });
        let mut wanted: Vec<(bool, i128)> = Vec::new();
        for caps in LINES.finditer(task) {
            if let Some(Num::Int(amount)) = parse_number(group_or_empty(&caps, 2)) {
                let entry = (group_or_empty(&caps, 1).to_lowercase() != "last", amount);
                if !wanted.contains(&entry) {
                    wanted.push(entry);
                }
            }
        }
        if let [(from_start, amount)] = wanted[..] {
            if !has_number(&command, &Nums(vec![Num::Int(amount)])) {
                let bare = Re::plain(&bare_head_tail(if from_start { "head" } else { "tail" }));
                if bare.finditer(&command).len() == 1 {
                    command = bare.sub_with(&command, 0, |caps: &Captures| {
                        format!("{} -n {amount}", group_or_empty(caps, 1))
                    });
                }
            }
        }
        command = hours_repair(&command, task);
        // "in my home folder" answered with `find .`: the search starts where the user
        // happens to be. Home is the one right start.
        if HOME.is_match(task) && !HOME_IN_COMMAND.is_match(&command) {
            command = FIND_HERE.sub_with(&command, 1, |caps: &Captures| format!("{} ~", group_or_empty(caps, 1)));
        }
        command = listing_repair(&command, task);
        command = sudo_repair(&command, task);
        command = ruled_out_repair(&command, task);
    }
    command
}

static LEADING_SUDO: LazyRe = re!(r"(^|[;&|(]\s*)sudo\s+(?:-[EHiknSs]+\s+)*");
static SUDO_AS_OTHER: LazyRe = re!(r"\bsudo\s+(?:-\S+\s+)*(?:-u|--user|-g|--group)\b");

/// "no sudo", "without sudo" ("I'm root", "in a container") in `text` (a task, or the
/// request it came from) and the command still runs sudo: dropping it is the one right
/// fix. `sudo -u bob` (another user) is left alone.
pub fn ruled_out_repair(command: &str, text: &str) -> String {
    if !forbidden(text).iter().any(|name| name == "sudo") || SUDO_AS_OTHER.is_match(command) {
        return command.to_string();
    }
    LEADING_SUDO.sub_with(command, 0, |caps: &Captures| group_or_empty(caps, 1).to_string())
}

// What a request rules out: "without lsof", "don't use sudo", "instead of curl",
// "avoid netstat", "no sudo", "lsof isn't installed", and lists ("without lsof or
// netstat"). Only a word the command then runs as a program counts, so "without
// deleting" or "no digit" never object. An unquoted word must end the phrase: in
// "without the watch header" or "no column headings" it describes something else.
const RULED_OUT_NAME: &str = concat!(
    r#"(?:`([^`\s]{1,40})`|'([^'\s]{1,40})'|"([^"\s]{1,40})"|(?!(?:and|or|nor|but|then|also|so)\b)([A-Za-z](?:[\w.+-]{0,38}[\w+])?)(?![\w'’+-]|\.\w)"#,
    r"(?=\s*(?:$|[,;.:!?)]|-\w|(?:or|and|nor|for|to|on|in|at|here|this|that|please|but|so|since|because|as|when|either|",
    r"anymore|again|installed|available|command|commands|tool|program|utility|binary|is|isn|are|aren|does|doesn|at\s+all)\b)))"
);
const RULED_OUT_LEAD: &str = concat!(
    r"\b(?:without(?:\s+using)?|(?:don'?t|do\s+not|never|not|cannot|can'?t)\s+(?:use|using|run|call)|not\s+(?:with|via)|",
    r"instead\s+of(?:\s+using)?|rather\s+than(?:\s+using)?|avoid(?:ing)?(?:\s+using)?|other\s+than|no|not)\s+(?:the\s+)?"
);
static RULED_OUT: LazyRe = re_fn!(format!("{RULED_OUT_LEAD}{RULED_OUT_NAME}"), "i");
static RULED_OUT_MORE: LazyRe = re_fn!(format!(r"^\s*(?:,\s*|or\s+|and\s+|nor\s+)(?:the\s+)?{RULED_OUT_NAME}"), "i");
// ", or ss" ends a list a bare comma began ("without lsof, netstat, or ss"); after a
// single name it starts the next clause ("without the header, and grep for ERROR").
static RULED_OUT_LAST: LazyRe = re_fn!(format!(r"^\s*,\s*(?:or|and|nor)\s+(?:the\s+)?{RULED_OUT_NAME}"), "i");
static NOT_INSTALLED: LazyRe = re_fn!(
    format!(r"{RULED_OUT_NAME}\s+(?:is\s*n[o']t|isn't|is\s+not|isn’t)\s+(?:installed|available)"),
    "i"
);

fn ruled_out_name(caps: &Captures) -> String {
    (1..=4).find_map(|i| group(caps, i)).unwrap_or("").to_lowercase()
}

/// The programs `text` rules out, in order.
pub fn forbidden(text: &str) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for caps in RULED_OUT.finditer(text) {
        names.push(ruled_out_name(&caps));
        let mut end = caps.get(0).unwrap().end();
        let mut listed = false;
        loop {
            let rest = &text[end..];
            let more = RULED_OUT_MORE.search(rest);
            listed |= more.is_some() && rest.trim_start().starts_with(',');
            let Some(more) = more.or_else(|| if listed { RULED_OUT_LAST.search(rest) } else { None }) else { break };
            names.push(ruled_out_name(&more));
            end += more.get(0).unwrap().end();
        }
    }
    for caps in NOT_INSTALLED.finditer(text) {
        names.push(ruled_out_name(&caps));
    }
    let mut unique = Vec::new();
    for name in names {
        if !name.is_empty() && !unique.contains(&name) {
            unique.push(name);
        }
    }
    unique
}

// Other names of a program: "without grep" rules out `fgrep` too.
fn same_program(ruled_out: &str, program: &str) -> bool {
    let program = program.to_lowercase();
    program == ruled_out
        || match ruled_out {
            "grep" => matches!(program.as_str(), "egrep" | "fgrep" | "rgrep"),
            "vim" | "vi" => matches!(program.as_str(), "vi" | "vim" | "vim.basic" | "vim.tiny" | "view"),
            "python" => program.starts_with("python"),
            _ => false,
        }
}

fn ruled_out_misses(task: &str, command: &str, request: &str) -> Vec<Miss> {
    let run = crate::validate::programs_run(command);
    let mut misses = Vec::new();
    for name in forbidden(task).into_iter().chain(forbidden(request)) {
        let miss = Miss { kind: "forbidden", detail: format!("the request says without {name}; the command uses it") };
        if run.iter().any(|program| same_program(&name, program)) && !misses.contains(&miss) {
            misses.push(miss);
        }
    }
    misses
}

/// The literals of `task` that `command` does not contain, programs the task or the
/// request ruled out that it runs, and known slips. `request` is the whole question a
/// task was split from: context a slip check may need (a step says "the project
/// directory" where the request said "in my home"), and restrictions a split dropped.
pub fn check(task: &str, command: &str, request: &str) -> Vec<Miss> {
    let mut misses: Vec<Miss> = Vec::new();
    // `backup_worker\.py` and `[b]ackup[.]py` are regex spellings of backup_worker.py
    let loose = REGEX_CLASS_OF_ONE.sub_with(&command.replace('\\', ""), 0, |caps: &Captures| {
        group_or_empty(caps, 1).to_string()
    });
    let lowered = loose.to_lowercase();
    // When the checked text is a task list, its numbers may be words ("first six lines").
    let numeric = WORD_NUMBER.sub_with(command, 0, |caps: &Captures| {
        word_number(&group_or_empty(caps, 0).to_lowercase()).unwrap().to_string()
    });
    let on_placeholder = PLACEHOLDER.is_match(command);
    for (kind, written, wanted) in requirements(task) {
        if on_placeholder && matches!(kind, "port" | "filename" | "path" | "text") {
            continue;
        }
        let ok = match (kind, &wanted) {
            ("duration" | "size" | "count" | "port", Wanted::Numbers(values)) => has_number(&numeric, values),
            // `.log`, not the `log` in /var/log
            ("extension", Wanted::Text(extension)) => lowered.contains(&format!(".{}", extension.to_lowercase())),
            ("home", _) => HOME_IN_COMMAND.is_match(&loose),
            ("filename", Wanted::Text(name)) => {
                let stem = name.rsplit_once('.').map_or(name.as_str(), |(stem, _)| stem);
                lowered.contains(&name.to_lowercase()) || lowered.contains(&format!("{}.", stem.to_lowercase()))
            }
            ("path", Wanted::Text(path)) => path_present(path, &loose),
            (_, Wanted::Text(text)) => lowered.contains(&text.to_lowercase()),
            _ => true,
        };
        if !ok {
            let miss = Miss { kind, detail: format!("the request says {}; the command does not have it", py::repr(&written)) };
            if !misses.contains(&miss) {
                misses.push(miss);
            }
        }
    }
    misses.extend(ruled_out_misses(task, command, request));
    misses.extend(slips(task, command, request));
    misses
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn literals_are_found() {
        let found = requirements("delete .log files older than 30 days in /var/log/myapp");
        let kinds: Vec<_> = found.iter().map(|(k, w, _)| (*k, w.as_str())).collect();
        assert_eq!(kinds, vec![("duration", "30 days"), ("extension", ".log"), ("path", "/var/log/myapp")]);
    }

    #[test]
    fn a_dropped_literal_is_a_miss() {
        let misses = check("delete .log files older than 30 days in /var/log/myapp",
                           "find /var/log/myapp -type f -mtime +30 -delete", "");
        assert_eq!(misses, vec![Miss { kind: "extension", detail: "the request says '.log'; the command does not have it".into() }]);
        assert!(check("delete .log files older than 30 days in /var/log/myapp",
                      "find /var/log/myapp -name '*.log' -mtime +29 -delete", "").is_empty());
    }

    #[test]
    fn crontab_entries_are_installed() {
        assert_eq!(repair("0 3 * * * /usr/local/bin/backup.sh", ""),
                   "(crontab -l 2>/dev/null; echo '0 3 * * * /usr/local/bin/backup.sh') | crontab -");
    }

    #[test]
    fn ps_header_and_grep_directory() {
        assert_eq!(repair("ps aux --sort=-%cpu | head -n 1", ""), "ps aux --sort=-%cpu | head -n 2");
        assert_eq!(repair("grep error /var/log", ""), "grep -r error /var/log");
    }

    #[test]
    fn ruled_out_programs() {
        assert_eq!(forbidden("show what is on port 8080 without lsof"), vec!["lsof"]);
        assert_eq!(forbidden("count the lines without using wc or `awk`, and don't use sudo"), vec!["wc", "awk", "sudo"]);
        assert_eq!(forbidden("download it instead of curl; netstat isn't installed"), vec!["curl", "netstat"]);
        assert_eq!(forbidden("without lsof, netstat, or ss"), vec!["lsof", "netstat", "ss"]);
        assert_eq!(forbidden("show the log without the header, and grep for ERROR"), vec!["header"]);
        assert_eq!(forbidden("without timestamps, and sort them by count"), vec!["timestamps"]);
        let slipped = |task: &str, command: &str| check(task, command, "").iter().any(|m| m.detail.contains("only asks to look"));
        assert!(slipped("Show which files can safely be removed from root.", "sudo find / -xdev -type f -delete"));
        assert!(slipped("list the empty directories under /srv", "find /srv -type d -empty -exec rmdir {} +"));
        assert!(!slipped("list the .tmp files and then delete them", "find . -name '*.tmp' -print -delete"));
        assert!(!slipped("show deleted files that are still open", "sudo find /proc/[0-9]*/fd -lname '* (deleted)'"));
        assert!(!slipped("delete files older than 30 days in /tmp", "find /tmp -type f -mtime +30 -delete"));
        let objections = |task: &str, command: &str, request: &str| -> Vec<String> {
            check(task, command, request).into_iter().filter(|m| m.kind == "forbidden").map(|m| m.detail).collect()
        };
        assert_eq!(objections("find the process on port 8080 without lsof", "sudo lsof -i :8080", ""),
                   vec!["the request says without lsof; the command uses it"]);
        // the split dropped the restriction; the request still has it
        assert_eq!(objections("find the process on port 8080", "lsof -i :8080", "port 8080 is busy; don't use lsof").len(), 1);
        assert_eq!(objections("count lines without wc", "find . -name '*.txt' -exec wc -l {} +", "").len(), 1);
        assert_eq!(objections("count lines without wc", "sh -c 'cat a | wc -l'", "").len(), 1);
        assert_eq!(objections("print my name without whoami", "echo \"$(whoami)\"", "").len(), 1);
        assert_eq!(objections("print my name without whoami", "echo `whoami`", "").len(), 1);
        assert_eq!(repair("sudo apt update", "update the package lists; I'm already root so no sudo"), "apt update");
        assert_eq!(repair("sudo -E apt update && sudo apt upgrade -y", "upgrade everything without sudo"),
                   "apt update && apt upgrade -y");
        assert_eq!(repair("sudo -u bob ls ~bob", "list bob's home without sudo"), "sudo -u bob ls ~bob");
        for (task, command) in [
            ("find the process on port 8080 without lsof", "ss -tlnp 'sport = :8080'"),
            ("delete old logs without deleting the directory", "find logs -type f -mtime +30 -delete"),
            ("lines with no digit", "grep -v '[0-9]' phrases.txt"),
            ("list files without the .log extension", "find . -type f ! -name '*.log'"),
        ] {
            assert!(objections(task, command, "").is_empty(), "{task} | {command}");
        }
    }
}
