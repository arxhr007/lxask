//! What lxask says about itself: `lxask --about`, and the answer to a question about
//! lxask rather than about Linux ("who made you?"), which needs no model.

use crate::py::Re;
use std::sync::OnceLock;

pub const AUTHOR: &str = "Aaron Thomas (arxhr007)";
pub const PROJECT_URL: &str = "https://github.com/arxhr007/lxask";
pub const AUTHOR_URL: &str = "https://github.com/arxhr007";
pub const WEBSITE: &str = "https://arxhr007.github.io";

/// The about text, for the program of `version`.
pub fn about(version: &str) -> String {
    format!(
        "lxask {version}: an offline Linux command assistant.\n\
         Made by {AUTHOR}.\n\
         \n  project  {PROJECT_URL}\n  GitHub   {AUTHOR_URL}\n  website  {WEBSITE}\n\
         \n\
         Its model is a fine-tune of Qwen2.5-Coder-1.5B-Instruct, and what it knows about\n\
         commands comes from tldr-pages and the man pages (credits: NOTICE).\n\
         Ask it how to do something on Linux: lxask \"find files bigger than 1GB\""
    )
}

// A question about lxask itself names it, or "you", as the thing made or asked about:
// "who made you", "who created lxask", "what are you", "who is your creator". A Linux
// question that happens to say "who created" names something else ("this file").
const ABOUT_ITSELF: &str = concat!(
    r"^\s*(?:hey\s+|hi\s+|so\s+)?(?:lxask[\s,:]+)?",
    r"(?:",
    r"who\s+(?:made|created|built|wrote|develop(?:ed|s)|maintains|programmed|trained)\s+(?:you|lxask)\b",
    r"|who\s+(?:is|are|'s)\s+(?:your|lxask'?s)\s+(?:creator|author|maker|developer|owner)s?\b",
    r"|who'?s\s+(?:your|lxask'?s)\s+(?:creator|author|maker|developer|owner)s?\b",
    r"|(?:what|who)\s+(?:are\s+you|is\s+lxask)\b",
    r"|(?:tell\s+me\s+)?about\s+(?:yourself|lxask)\s*[?.!]*\s*$",
    r"|what(?:'s|\s+is)\s+your\s+name\b",
    r")"
);

fn pattern() -> &'static Re {
    static RE: OnceLock<Re> = OnceLock::new();
    RE.get_or_init(|| Re::new(ABOUT_ITSELF, "i"))
}

/// Whether `question` asks about lxask itself rather than how to do something.
pub fn about_itself(question: &str) -> bool {
    pattern().is_match(question)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn questions_about_lxask_itself() {
        for question in [
            "who made you",
            "who made you?",
            "Who created lxask?",
            "lxask, who built you",
            "who is your creator",
            "who's your author",
            "what are you",
            "what is lxask?",
            "tell me about yourself",
            "what's your name",
            "hey who developed you",
        ] {
            assert!(about_itself(question), "{question}");
        }
    }

    #[test]
    fn linux_questions_are_not_about_lxask() {
        for question in [
            "who created this file",
            "show who made the last commit",
            "what are the biggest files here",
            "find out who is logged in",
            "who owns /var/log/syslog",
            "tell me about disk usage of /home",
            "what is my ip address",
            "show what you can find about the cpu",
            "list who wrote to the log",
        ] {
            assert!(!about_itself(question), "{question}");
        }
    }

    #[test]
    fn the_text_names_the_author_and_links() {
        let text = about("0.9.1");
        assert!(text.starts_with("lxask 0.9.1: "));
        for part in [AUTHOR, PROJECT_URL, AUTHOR_URL, WEBSITE] {
            assert!(text.contains(part), "{part}");
        }
    }
}
