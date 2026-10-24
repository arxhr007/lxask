//! Requests too vague for one command. "Add a proxy" is a different command for this
//! shell, for Git, for APT or for Docker, and a guess at one of them (or worse, at an
//! `scp` that retrieval found under "proxy") is worse than asking which. A topic here
//! asks back only when the request names none of its contexts.

use std::sync::OnceLock;

use crate::py::Re;

struct Context {
    name: &'static str,
    recognizer: &'static str,
    /// a request naming this context, as the user could ask it next
    ask: &'static str,
}

struct Topic {
    trigger: &'static str,
    contexts: &'static [Context],
    /// contexts that settle what is meant but need no entry in the question
    also_clear: &'static str,
}

const PROXY_CONTEXTS: &[Context] = &[
    Context {
        name: "this shell session",
        recognizer: concat!(
            r"\b(?:shell|terminal|session|bash|zsh|export|environment|env|variables?|http_proxy|https_proxy|no_proxy|curl(?:rc)?|wget(?:rc)?|",
            r"requests?|https?|urls?|fetch\w*|download\w*|web\s*(?:sites?|pages?)|programs?\s+i\s+run)\b"
        ),
        ask: "set an http proxy for this shell session",
    },
    Context {
        name: "the whole system",
        recognizer: r"\bsystem[\s-]*wide\b|\ball\s+users\b|/etc/environment\b|\bevery\s+user\b",
        ask: "set an http proxy system-wide for all users",
    },
    Context { name: "Git", recognizer: r"\bgit\b|\brepo(?:sitory|sitories|s)?\b|\bclone\b|\bgithub\b|\bgitlab\b", ask: "set a proxy for git" },
    Context {
        name: "APT",
        recognizer: r"\bapt(?:-get)?\b|\bubuntu\b|\bdebian\b|\bpackage\s+manager\b|\bpackages?\b",
        ask: "set a proxy for apt",
    },
    Context { name: "dnf / yum", recognizer: r"\b(?:dnf|yum|fedora|rhel|centos|rocky)\b", ask: "set a proxy for dnf" },
    Context { name: "pip", recognizer: r"\bpip3?\b|\bpython\b|\bpypi\b", ask: "set a proxy for pip" },
    Context { name: "npm", recognizer: r"\bnpm\b|\bnode(?:js)?\b|\byarn\b", ask: "set a proxy for npm" },
    Context {
        name: "Docker",
        recognizer: r"\bdocker\b|\bpodman\b|\bcontainers?\b|\bimages?\b",
        ask: "make docker pull images through an http proxy",
    },
    Context {
        name: "SSH",
        recognizer: r"\bssh\b|\bscp\b|\bsftp\b|\bjump\s*hosts?\b|\bbastion\b|\btunnel\w*\b|\bsocks\d?\b|\bproxy\s*(?:jump|command)\b",
        ask: "connect over ssh through a jump host",
    },
    Context { name: "run a proxy server", recognizer: r"\bsquid\b|\btinyproxy\b|\bproxy\s+server\s+on\b", ask: "set up a squid proxy server" },
];

const TOPICS: &[Topic] = &[Topic {
    // not `proxy.conf` or `--proxy`: a file name or an option is not a request for one
    trigger: r"(?<![\w./-])prox(?:y|ies)(?![\w-]|\.\w)",
    contexts: PROXY_CONTEXTS,
    // looking into a proxy (its connections, ports, logs) is not configuring one
    also_clear: r"\b(?:tcp|udp|listen\w*|connections?|traffic|logs?|troubleshoot\w*|debug\w*|investigat\w*)\b|\breverse\s+prox(?:y|ies)\b|\bnginx\b|\bapache2?\b|\bhaproxy\b|\bcaddy\b|\btraefik\b|\bfirefox\b|\bchrom(?:e|ium)\b|\bsnap\b|\bsystemd\b|\bproxychains\b|\bkubectl\b|\bconda\b|\bcargo\b|\bgo\s+(?:get|mod)\b|\bmaven\b|\bgradle\b",
}];

struct Compiled {
    trigger: Re,
    contexts: Vec<(Re, &'static Context)>,
    also_clear: Re,
}

fn compiled() -> &'static [Compiled] {
    static TABLE: OnceLock<Vec<Compiled>> = OnceLock::new();
    TABLE.get_or_init(|| {
        TOPICS
            .iter()
            .map(|topic| Compiled {
                trigger: Re::new(topic.trigger, "i"),
                contexts: topic.contexts.iter().map(|c| (Re::new(c.recognizer, "i"), c)).collect(),
                also_clear: Re::new(topic.also_clear, "i"),
            })
            .collect()
    })
}

/// Whether the request mentions a topic here at all (a proxy), whether or not it says
/// enough to answer.
pub fn mentions_topic(question: &str) -> bool {
    compiled().iter().any(|topic| topic.trigger.is_match(question))
}

/// The question to ask back, or None when the request says enough to answer.
pub fn clarification(question: &str) -> Option<String> {
    for topic in compiled() {
        if !topic.trigger.is_match(question) || topic.also_clear.is_match(question) {
            continue;
        }
        if topic.contexts.iter().any(|(recognizer, _)| recognizer.is_match(question)) {
            continue;
        }
        let shown: String = question.trim().chars().take(60).collect();
        let shown = if question.trim().chars().count() > 60 { format!("{shown}...") } else { shown };
        let mut lines = vec![format!("\"{shown}\" can mean several things. Which one? Ask again naming it, e.g.:")];
        let width = topic.contexts.iter().map(|(_, c)| c.name.chars().count()).max().unwrap_or(0);
        for (_, context) in &topic.contexts {
            lines.push(format!("  {:width$}  lxask \"{}\"", context.name, context.ask));
        }
        return Some(lines.join("\n"));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_bare_proxy_request_is_asked_back() {
        for question in ["add a proxy", "configure a proxy", "how do I use my company proxy?", "set proxy 10.0.0.1:3128"] {
            let asked = clarification(question).unwrap_or_else(|| panic!("{question}"));
            assert!(asked.contains("lxask \"set a proxy for git\""), "{asked}");
        }
    }

    #[test]
    fn a_named_context_is_answered() {
        for question in [
            "set a proxy for git", "make apt use a proxy", "export http_proxy for this terminal",
            "pull docker images through a proxy", "ssh to the server through a socks proxy",
            "set up nginx as a reverse proxy", "pip install through a proxy", "find files named proxy.conf",
        ] {
            assert_eq!(clarification(question), None, "{question}");
        }
        // every suggestion it offers is itself answerable
        let asked = clarification("add a proxy").unwrap();
        for line in asked.lines().skip(1) {
            let suggestion = line.split("lxask \"").nth(1).unwrap().trim_end_matches('"');
            assert_eq!(clarification(suggestion), None, "{suggestion}");
        }
    }

    #[test]
    fn other_requests_are_left_alone() {
        assert_eq!(clarification("show disk usage of each folder"), None);
    }
}
