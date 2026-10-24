//! The parts of Python's string and `re` behaviour that lxask's rules depend on.
//!
//! The rules were written, tuned and measured in Python, and the training data was
//! filtered with them, so they must give the same answers here: whitespace as
//! `str.isspace` sees it, `splitlines`, `repr`, code-point lengths, and regular
//! expressions with Python's syntax and its empty-match rules.

use std::sync::OnceLock;

pub type Captures<'t> = fancy_regex::Captures<'t, str>;

/// `str.isspace`: Rust's White_Space plus the four ASCII separators U+001C..U+001F.
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.split()`.
pub fn split_ws(s: &str) -> Vec<&str> {
    s.split(is_space).filter(|w| !w.is_empty()).collect()
}

/// `str.strip()`.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

pub fn lstrip(s: &str) -> &str {
    s.trim_start_matches(is_space)
}

pub fn rstrip(s: &str) -> &str {
    s.trim_end_matches(is_space)
}

/// `str.strip(chars)`.
pub fn strip_chars<'a>(s: &'a str, chars: &str) -> &'a str {
    s.trim_matches(|c| chars.contains(c))
}

pub fn rstrip_chars<'a>(s: &'a str, chars: &str) -> &'a str {
    s.trim_end_matches(|c| chars.contains(c))
}

/// `str.splitlines()` (without the line ends).
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let end = match c {
            '\n' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}' => {
                i + c.len_utf8()
            }
            '\r' => {
                if let Some(&(_, '\n')) = chars.peek() {
                    chars.next();
                    i + 2
                } else {
                    i + 1
                }
            }
            _ => continue,
        };
        lines.push(&s[start..i]);
        start = end;
    }
    if start < s.len() {
        lines.push(&s[start..]);
    }
    lines
}

/// Length in code points, as Python's `len(str)`.
pub fn char_len(s: &str) -> usize {
    s.chars().count()
}

/// `s[:n]` in code points.
pub fn prefix(s: &str, n: usize) -> &str {
    match s.char_indices().nth(n) {
        Some((i, _)) => &s[..i],
        None => s,
    }
}

/// `s[:1].upper() + s[1:]`.
pub fn upper_first(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        Some(c) => c.to_uppercase().chain(chars).collect(),
        None => String::new(),
    }
}

/// `str.rpartition(sep)`.
pub fn rpartition<'a>(s: &'a str, sep: &str) -> (&'a str, &'a str, &'a str) {
    match s.rfind(sep) {
        Some(i) => (&s[..i], &s[i..i + sep.len()], &s[i + sep.len()..]),
        None => ("", "", s),
    }
}

/// `str.isprintable()` for one non-ASCII character, as far as it matters for `repr`:
/// the control, format, separator and private-use ranges.
fn printable(c: char) -> bool {
    let n = c as u32;
    !matches!(n,
        0x80..=0xa0 | 0xad | 0x600..=0x605 | 0x61c | 0x6dd | 0x70f | 0x1680 | 0x180e
        | 0x2000..=0x200f | 0x2028..=0x202f | 0x205f..=0x2064 | 0x2066..=0x206f | 0x3000
        | 0xe000..=0xf8ff | 0xfeff | 0xfff9..=0xfffb | 0xfffe | 0xffff | 0xe0001 | 0xe0020..=0xe007f
        | 0xf0000..=0x10ffff)
}

/// `repr(str)`.
pub fn repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            _ if c == quote || c == '\\' => {
                out.push('\\');
                out.push(c);
            }
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            _ if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            _ if (c as u32) < 0x7f || printable(c) => out.push(c),
            _ if (c as u32) <= 0xff => out.push_str(&format!("\\x{:02x}", c as u32)),
            _ if (c as u32) <= 0xffff => out.push_str(&format!("\\u{:04x}", c as u32)),
            _ => out.push_str(&format!("\\U{:08x}", c as u32)),
        }
    }
    out.push(quote);
    out
}

/// `str(float)`: the shortest repr, in Python's notation.
pub fn float_str(value: f64) -> String {
    if value == value.trunc() && value.abs() < 1e16 {
        return format!("{:.1}", value);
    }
    let magnitude = value.abs();
    if magnitude != 0.0 && !(1e-4..1e16).contains(&magnitude) {
        // Python: 1e-05, 1.5e+16
        let text = format!("{:e}", value);
        let (mantissa, exponent) = text.split_once('e').unwrap();
        let exponent: i32 = exponent.parse().unwrap();
        let sign = if exponent < 0 { '-' } else { '+' };
        return format!("{mantissa}e{sign}{:02}", exponent.abs());
    }
    format!("{}", value)
}

/// `re.escape`.
pub fn escape(s: &str) -> String {
    fancy_regex::escape(s).into_owned()
}

/// Python pattern syntax -> Rust's. Python's `\s` also matches U+001C..U+001F, its
/// `$` (without MULTILINE) matches before a final newline, `\Z` is Rust's `\z`, and
/// inside a class `[` is literal while Rust reads it (and `&&`, `~~`, `--`) as set
/// operations.
fn translate(pattern: &str, multiline: bool) -> String {
    const META: &str = "\\.+*?()|[]{}^$#&-~";
    let mut out = String::with_capacity(pattern.len() + 16);
    let mut chars = pattern.chars().peekable();
    let mut in_class = false;
    while let Some(c) = chars.next() {
        if c == '\\' {
            let Some(next) = chars.next() else {
                out.push_str("\\\\");
                break;
            };
            match (next, in_class) {
                ('Z', false) => out.push_str("\\z"),
                ('s', false) => out.push_str("[\\s\\x1c-\\x1f]"),
                ('S', false) => out.push_str("[^\\s\\x1c-\\x1f]"),
                ('s', true) => out.push_str("\\s\\x1c-\\x1f"),
                ('S', true) => panic!("\\S inside a character class is not supported: {pattern}"),
                _ if next.is_ascii_alphanumeric() || META.contains(next) => {
                    out.push('\\');
                    out.push(next);
                }
                // Python lets any character be escaped; `\'` is a quote, `\<` a `<`.
                _ => out.push(next),
            }
            continue;
        }
        if in_class {
            match c {
                ']' => {
                    in_class = false;
                    out.push(']');
                }
                '[' | '&' | '~' => {
                    out.push('\\');
                    out.push(c);
                }
                '-' if chars.peek() == Some(&'-') => out.push_str("\\-"),
                _ => out.push(c),
            }
            continue;
        }
        match c {
            '[' => {
                in_class = true;
                out.push('[');
                if chars.peek() == Some(&'^') {
                    out.push(chars.next().unwrap());
                }
                if chars.peek() == Some(&']') {
                    chars.next();
                    out.push_str("\\]"); // `[]...]`: a leading `]` is literal
                }
            }
            '$' if !multiline => out.push_str("(?=\\n?\\z)"),
            _ => out.push(c),
        }
    }
    out
}

/// A compiled Python regular expression. `flags` is any of "i", "m", "s" (re.I, re.M, re.S).
pub struct Re {
    pattern: String,
    flags: String,
    search: fancy_regex::Regex,
    anchored: OnceLock<fancy_regex::Regex>,
    full: OnceLock<fancy_regex::Regex>,
}

fn compile(text: &str) -> fancy_regex::Regex {
    fancy_regex::RegexBuilder::new(text)
        .backtrack_limit(10_000_000)
        .build()
        .unwrap_or_else(|e| panic!("bad pattern {text}: {e}"))
}

impl Re {
    pub fn new(pattern: &str, flags: &str) -> Re {
        let translated = translate(pattern, flags.contains('m'));
        let prefix = if flags.is_empty() { String::new() } else { format!("(?{flags})") };
        Re {
            search: compile(&format!("{prefix}{translated}")),
            pattern: translated,
            flags: prefix,
            anchored: OnceLock::new(),
            full: OnceLock::new(),
        }
    }

    pub fn plain(pattern: &str) -> Re {
        Re::new(pattern, "")
    }

    /// `re.search`.
    pub fn search<'t>(&self, text: &'t str) -> Option<Captures<'t>> {
        self.search.captures(text).ok().flatten()
    }

    pub fn is_match(&self, text: &str) -> bool {
        self.search.is_match(text).unwrap_or(false)
    }

    /// `re.match`: at the start of `text`.
    pub fn match_<'t>(&self, text: &'t str) -> Option<Captures<'t>> {
        self.anchored
            .get_or_init(|| compile(&format!("{}\\A(?:{})", self.flags, self.pattern)))
            .captures(text)
            .ok()
            .flatten()
    }

    /// `re.fullmatch`.
    pub fn fullmatch<'t>(&self, text: &'t str) -> Option<Captures<'t>> {
        self.full
            .get_or_init(|| compile(&format!("{}\\A(?:{})\\z", self.flags, self.pattern)))
            .captures(text)
            .ok()
            .flatten()
    }

    pub fn fullmatches(&self, text: &str) -> bool {
        self.fullmatch(text).is_some()
    }

    /// `re.finditer`, with Python's rule for empty matches: one may follow a non-empty
    /// match directly, but two never start at the same place.
    pub fn finditer<'t>(&self, text: &'t str) -> Vec<Captures<'t>> {
        let mut found = Vec::new();
        let mut pos = 0;
        let mut must_advance = false;
        while pos <= text.len() {
            let Some(caps) = self.search.captures_from_pos(text, pos).ok().flatten() else { break };
            let whole = caps.get(0).unwrap();
            if must_advance && whole.start() == pos && whole.end() == pos {
                match text[pos..].chars().next() {
                    Some(c) => {
                        pos += c.len_utf8();
                        must_advance = false;
                        continue;
                    }
                    None => break,
                }
            }
            must_advance = whole.start() == whole.end();
            pos = whole.end();
            found.push(caps);
        }
        found
    }

    /// `re.sub` with a function; `count` 0 replaces every match.
    pub fn sub_with(&self, text: &str, count: usize, mut replace: impl FnMut(&Captures) -> String) -> String {
        let mut out = String::with_capacity(text.len());
        let mut last = 0;
        for (n, caps) in self.finditer(text).iter().enumerate() {
            if count != 0 && n >= count {
                break;
            }
            let whole = caps.get(0).unwrap();
            out.push_str(&text[last..whole.start()]);
            out.push_str(&replace(caps));
            last = whole.end();
        }
        out.push_str(&text[last..]);
        out
    }

    /// `re.sub` with a fixed replacement (no group references).
    pub fn sub(&self, text: &str, replacement: &str) -> String {
        self.sub_with(text, 0, |_| replacement.to_string())
    }

    /// `re.split` for a pattern without groups; `maxsplit` 0 splits at every match.
    pub fn split<'t>(&self, text: &'t str, maxsplit: usize) -> Vec<&'t str> {
        let mut parts = Vec::new();
        let mut last = 0;
        for (n, caps) in self.finditer(text).iter().enumerate() {
            if maxsplit != 0 && n >= maxsplit {
                break;
            }
            let whole = caps.get(0).unwrap();
            parts.push(&text[last..whole.start()]);
            last = whole.end();
        }
        parts.push(&text[last..]);
        parts
    }
}

/// Group `i` of a match, as `match.group(i)` (None when it did not take part).
pub fn group<'t>(caps: &Captures<'t>, i: usize) -> Option<&'t str> {
    caps.get(i).map(|m| m.as_str())
}

/// Group `i`, or "" when it did not take part.
pub fn group_or_empty<'t>(caps: &Captures<'t>, i: usize) -> &'t str {
    group(caps, i).unwrap_or("")
}

/// A lazily compiled `Re`, for patterns held in statics.
pub struct LazyRe {
    pattern: fn() -> String,
    flags: &'static str,
    cell: OnceLock<Re>,
}

impl LazyRe {
    pub const fn new(pattern: fn() -> String, flags: &'static str) -> LazyRe {
        LazyRe { pattern, flags, cell: OnceLock::new() }
    }
}

impl std::ops::Deref for LazyRe {
    type Target = Re;
    fn deref(&self) -> &Re {
        self.cell.get_or_init(|| Re::new(&(self.pattern)(), self.flags))
    }
}

/// `static NAME: LazyRe = re!(r"pattern", "flags");`
#[macro_export]
macro_rules! re {
    ($pattern:expr) => {
        $crate::py::LazyRe::new(|| String::from($pattern), "")
    };
    ($pattern:expr, $flags:expr) => {
        $crate::py::LazyRe::new(|| String::from($pattern), $flags)
    };
}

/// `re!` for a pattern built at first use.
#[macro_export]
macro_rules! re_fn {
    ($body:expr, $flags:expr) => {
        $crate::py::LazyRe::new(|| $body, $flags)
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitlines_matches_python() {
        assert_eq!(splitlines("a\nb\r\nc\rd"), vec!["a", "b", "c", "d"]);
        assert_eq!(splitlines("a\n"), vec!["a"]);
        assert_eq!(splitlines("\n"), vec![""]);
        assert_eq!(splitlines(""), Vec::<&str>::new());
        assert_eq!(splitlines("a\n\nb"), vec!["a", "", "b"]);
        assert_eq!(splitlines("a\u{1c}b\u{1f}c"), vec!["a", "b\u{1f}c"]);
    }

    #[test]
    fn repr_matches_python() {
        assert_eq!(repr(".log"), "'.log'");
        assert_eq!(repr("it's"), "\"it's\"");
        assert_eq!(repr("a'b\"c"), "'a\\'b\"c'");
        assert_eq!(repr("tab\there\\"), "'tab\\there\\\\'");
        assert_eq!(repr("caf\u{e9}\u{a0}"), "'caf\u{e9}\\xa0'");
    }

    #[test]
    fn float_str_matches_python() {
        assert_eq!(float_str(1.5), "1.5");
        assert_eq!(float_str(100.0), "100.0");
        assert_eq!(float_str(314572.8), "314572.8");
        assert_eq!(float_str(0.00001), "1e-05");
    }

    #[test]
    fn dollar_matches_before_a_final_newline() {
        let re = Re::plain(r"ab$");
        assert!(re.is_match("ab\n"));
        assert!(!re.is_match("ab\nc"));
        assert!(Re::new(r"ab$", "m").is_match("ab\nc"));
    }

    #[test]
    fn empty_matches_follow_python() {
        // re.sub('x*', '-', 'abxd') == '-a-b--d-'
        assert_eq!(Re::plain("x*").sub("abxd", "-"), "-a-b--d-");
        assert_eq!(Re::plain(r"[;&]+").split("a;b&&c", 0), vec!["a", "b", "c"]);
        assert_eq!(Re::plain(r"\s{2,}").split("a  b  c", 1), vec!["a", "b  c"]);
    }

    #[test]
    fn match_and_fullmatch_are_anchored() {
        let re = Re::plain(r"-\d+");
        assert!(re.fullmatches("-10"));
        assert!(!re.fullmatches("-10x"));
        assert!(re.match_("-1x").is_some());
        assert!(re.match_("x-1").is_none());
        assert!(Re::plain("a|ab").fullmatches("ab"));
    }

    #[test]
    fn classes_translate() {
        assert!(Re::plain(r"[\w.-]+").fullmatches("a.b-c"));
        assert!(Re::plain(r#"[\s'\"(`]"#).is_match("\""));
        assert!(Re::plain(r"[\w/.~$-]").is_match("~"));
        assert!(Re::plain(r"\s").is_match("\u{1d}"));
        assert!(Re::plain(r"[^\s|;&)]+").fullmatches("abc"));
        assert!(!Re::plain(r"[^\s|;&)]+").is_match("&"));
    }
}
