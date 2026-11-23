//! The manual pages installed on this machine, read when a command uses a program, so
//! the check follows the version installed here and not the one the index was built from.
//!
//! A page is found in the usual man directories (sections 1 and 8, as the index uses),
//! rendered with groff (or mandoc) the way `man` would at 100 columns, and read with the
//! same rules as the index builder (`lxask.index.man_option_docs`). Nothing here runs the
//! program itself, and `man` is not used: on some systems one lookup takes over a second.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::constraints::which;
use crate::py::{self, LazyRe};
use crate::re;

/// Where pages are looked for after $MANPATH's entries.
const MAN_DIRS: [&str; 3] = ["/usr/share/man", "/usr/local/share/man", "/usr/local/man"];
const SECTIONS: [&str; 2] = ["1", "8"];
const RENDER_TIMEOUT: Duration = Duration::from_secs(5);

// As in lxask.index: headings usually sit 7 columns in, nested lists put them at 14.
const HEADING_INDENT: usize = 16;
const WRAPPED_CHARS: usize = 60;
const MAN_CHUNK_CHARS: usize = 400;

static PAGE_NAME: LazyRe = re!(r"[A-Za-z0-9_][A-Za-z0-9._+-]*");
static OPTION_HEADING: LazyRe = re!(r"-{1,2}[\w?]|-,\s*-");
static TWO_SPACES: LazyRe = re!(r"\s{2,}");
static SPACES: LazyRe = re!(r"\s+");
static HYPHENATED: LazyRe = re!(r"\w‐$"); // U+2010, where man hyphenated a word
static MAN_NAME: LazyRe = re!(r"^NAME\n\s+(.+?)\n\s*\n", "ms");
static OVERSTRIKE: LazyRe = re!(".\x08"); // a character struck over by the next: bold, underline
static ANSI_SGR: LazyRe = re!("\x1b\\[[0-9;]*m");

/// The rendered text of `name`'s manual page on this machine, or None when there is none
/// (or no groff or mandoc to render it).
pub fn page_text(name: &str) -> Option<String> {
    let path = find_page(name)?;
    let source = page_source(&path, 1)?;
    render(&source)
}

/// `/usr/share/man/man1/tar.1.gz` for `tar`.
fn find_page(name: &str) -> Option<PathBuf> {
    if !PAGE_NAME.fullmatches(name) {
        return None; // `ls-/tmp`, `grep-$x`: not a page name
    }
    let mut dirs: Vec<PathBuf> = std::env::var_os("MANPATH")
        .map(|value| std::env::split_paths(&value).filter(|d| !d.as_os_str().is_empty()).collect())
        .unwrap_or_default();
    dirs.extend(MAN_DIRS.iter().map(PathBuf::from));
    for dir in dirs {
        for section in SECTIONS {
            for file in [format!("{name}.{section}.gz"), format!("{name}.{section}")] {
                let candidate = dir.join(format!("man{section}")).join(file);
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
        }
    }
    None
}

/// The page's roff source, following one `.so man1/other.1` redirect (egrep -> grep).
fn page_source(path: &Path, redirects: usize) -> Option<Vec<u8>> {
    let source = if path.extension().is_some_and(|ext| ext == "gz") {
        run(&which("gzip")?, &["-dc".as_ref(), path.as_os_str()], None)?
    } else {
        std::fs::read(path).ok()?
    };
    let text = String::from_utf8_lossy(&source);
    if let Some(target) = text.trim_start().strip_prefix(".so ") {
        let target = target.lines().next()?.trim();
        // relative to the man directory: /usr/share/man/man1/egrep.1.gz -> /usr/share/man
        let root = path.parent()?.parent()?;
        if redirects == 0 || target.contains("..") {
            return None;
        }
        let target = root.join(target);
        let gz = PathBuf::from(format!("{}.gz", target.display()));
        let found = [target, gz].into_iter().find(|p| p.is_file())?;
        return page_source(&found, redirects - 1);
    }
    Some(source)
}

/// Plain text, as `man -l` then `col -bx` gives it at MANWIDTH=100 (the index's width).
fn render(source: &[u8]) -> Option<String> {
    let output = if let Some(groff) = which("groff") {
        let args = ["-t", "-k", "-mandoc", "-Tutf8", "-P-cbou", "-rLL=97n", "-rLT=97n"];
        run(&groff, &args.map(|a| a.as_ref()), Some(source.to_vec()))?
    } else {
        let mandoc = which("mandoc")?;
        run(&mandoc, &["-Tutf8".as_ref(), "-Owidth=97".as_ref()], Some(source.to_vec()))?
    };
    let text = String::from_utf8_lossy(&output);
    let text = ANSI_SGR.sub(&OVERSTRIKE.sub(&text, ""), "");
    (!text.trim().is_empty()).then_some(text)
}

/// Run a renderer with `input` on stdin; its stdout, or None after RENDER_TIMEOUT.
fn run(program: &Path, args: &[&std::ffi::OsStr], input: Option<Vec<u8>>) -> Option<Vec<u8>> {
    let mut child = Command::new(program)
        .args(args)
        .env("LC_ALL", "C.UTF-8")
        .env("GROFF_NO_SGR", "1")
        .stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    if let (Some(bytes), Some(mut stdin)) = (input, child.stdin.take()) {
        std::thread::spawn(move || {
            let _ = stdin.write_all(&bytes);
        });
    }
    let mut stdout = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut out = Vec::new();
        let _ = stdout.read_to_end(&mut out);
        out
    });
    let deadline = Instant::now() + RENDER_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(2)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
    let out = reader.join().ok()?;
    (!out.is_empty()).then_some(out)
}

/// `list directory contents` from the NAME section (lxask.index.man_summary).
pub fn summary(text: &str) -> Option<String> {
    let caps = MAN_NAME.search(text)?;
    let line = SPACES.sub(py::group_or_empty(&caps, 1), " ");
    let line = py::strip(&line);
    let (_, description) = line.split_once(" - ")?;
    Some(py::strip(description).to_string())
}

fn indent_of(line: &str) -> usize {
    py::char_len(line) - py::char_len(py::lstrip(line))
}

/// A line of running prose cut at the margin, not a short list item.
fn wrapped(line: &str) -> bool {
    let stripped = py::strip(line);
    py::char_len(stripped) >= WRAPPED_CHARS && !stripped.ends_with(['.', ':'])
}

/// Lines of man text as one line with single spaces; a word man hyphenated at a line end
/// (`lev‐` + `els`, U+2010) is joined again (lxask.index.join_wrapped).
fn join_wrapped(lines: &[&str]) -> String {
    let mut text = String::new();
    for line in lines {
        if HYPHENATED.is_match(&text) {
            text.pop();
            text.push_str(line);
        } else if text.is_empty() {
            text.push_str(line);
        } else {
            text.push(' ');
            text.push_str(line);
        }
    }
    SPACES.sub(&text, " ")
}

/// (heading, first paragraph of its description) for every option in a rendered man
/// page: a port of lxask.index.man_option_docs, which built the index, so a page read
/// here gives what the index would have.
pub fn option_docs(text: &str) -> Vec<(String, String)> {
    let lines = py::splitlines(text);
    let mut prose: Vec<bool> = Vec::with_capacity(lines.len());
    let mut docs = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        let stripped = py::strip(line);
        let indent = indent_of(line);
        if indent > HEADING_INDENT || OPTION_HEADING.match_(stripped).is_none() {
            prose.push(false);
            continue;
        }
        let above = if i > 0 { lines[i - 1] } else { "" };
        let below = lines.get(i + 1).copied().unwrap_or("");
        let split = TWO_SPACES.split(stripped, 1);
        let title = split[0].to_string();
        let mut parts: Vec<&str> = split[1..].to_vec();
        // a run of prose lines that each start with an option (lsof): the one above counts
        // as prose if it is
        let above_is_prose =
            OPTION_HEADING.match_(py::strip(above)).is_none() || (prose[i - 1] && wrapped(above));
        let described_below = !py::strip(below).is_empty() && indent_of(below) > indent;
        if !py::strip(above).is_empty() && indent_of(above) == indent && above_is_prose && !described_below {
            prose.push(true);
            docs.push((title, String::new()));
            continue;
        }
        prose.push(false);
        let mut may_skip_blank = parts.is_empty();
        for following in &lines[i + 1..] {
            if py::strip(following).is_empty() {
                if !parts.is_empty() || !may_skip_blank {
                    break;
                }
                may_skip_blank = false;
                continue;
            }
            if indent_of(following) <= indent {
                break; // the next heading, or text that belongs to something else
            }
            parts.push(py::strip(following));
        }
        docs.push((title, py::prefix(&join_wrapped(&parts), MAN_CHUNK_CHARS).to_string()));
    }
    docs
}

#[cfg(test)]
mod tests {
    use super::*;

    const PAGE: &str = "\
NAME
       tar - an archiving utility

OPTIONS
       -c, --create
              Create a new archive.  Arguments supply the names of the files to be ar\u{2010}
              chived.

       -f, --file=ARCHIVE
              Use archive file or device ARCHIVE.

       -v, --verbose  Verbosely list files processed.

       -s expression
              This is a long line of running prose that the man formatter wrapped here
       -x in the middle, so it is not a heading of its own
";

    #[test]
    fn options_as_the_index_reads_them() {
        let docs = option_docs(PAGE);
        assert_eq!(docs[0], ("-c, --create".into(), "Create a new archive. Arguments supply the names of the files to be archived.".into()));
        assert_eq!(docs[1], ("-f, --file=ARCHIVE".into(), "Use archive file or device ARCHIVE.".into()));
        assert_eq!(docs[2], ("-v, --verbose".into(), "Verbosely list files processed.".into()));
        assert_eq!(docs[3].0, "-s expression");
        assert_eq!(docs.len(), 5);
        assert_eq!(docs[4], ("-x in the middle, so it is not a heading of its own".into(), String::new()));
    }

    #[test]
    fn summary_from_the_name_line() {
        assert_eq!(summary(PAGE).as_deref(), Some("an archiving utility"));
        assert_eq!(summary("NAME\n       tar\n\n"), None);
    }

    #[test]
    fn only_page_names_are_looked_up() {
        assert!(find_page("ls-/tmp").is_none());
        assert!(find_page("../etc/passwd").is_none());
        assert!(find_page("").is_none());
    }
}
