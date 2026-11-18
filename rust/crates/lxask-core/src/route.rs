//! Which adapter answers a request. The restriction adapter is a LoRA fine-tune of the
//! model on requests that rule a program out ("without lsof") and on the proxy contexts
//! (APT, /etc/environment, the Docker daemon, ...): it keeps those far better than the
//! model alone, but drifts on everyday requests (`watch` -> `free`), so only these go
//! to it. Every other request gets the precision adapter, a fine-tune on exact
//! filenames, flags and filters; without it installed, the model alone answers.

use crate::clarify;
use crate::constraints;
use crate::validate::Catalog;

/// The adapter a question answers with. `Restrict` handles requests that rule a program
/// out or name a proxy context; `Precision` answers everything else, since it scores
/// higher than the bare model on everyday requests too. An adapter that is not
/// installed means the model alone.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Adapter {
    Restrict,
    Precision,
}

/// Whether `question` is one for the restriction adapter: it rules out a program (a word
/// the catalogue knows as one; without a catalogue, any ruled-out word), or it mentions
/// a topic that has contexts to tell apart (a proxy).
pub fn specialist(question: &str, catalog: Option<&Catalog>) -> bool {
    clarify::mentions_topic(question)
        || constraints::forbidden(question)
            .iter()
            .any(|word| catalog.map_or(true, |catalog| catalog.programs.contains(&word.to_lowercase())))
}

/// Which adapter `question` goes to (`specialist` for the details).
pub fn adapter(question: &str, catalog: Option<&Catalog>) -> Adapter {
    if specialist(question, catalog) { Adapter::Restrict } else { Adapter::Precision }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn catalog() -> Catalog {
        let mut catalog = Catalog::default();
        catalog.programs.extend(["lsof", "ss", "tar", "sudo", "find"].map(String::from));
        catalog
    }

    #[test]
    fn ruled_out_programs_and_proxies_go_to_the_adapter() {
        let catalog = catalog();
        for question in [
            "which process listens on port 8080 without lsof",
            "list open ports, don't use ss",
            "install nginx without sudo",
            "set a proxy for apt",
            "add a proxy",
        ] {
            assert!(specialist(question, Some(&catalog)), "{question}");
        }
    }

    #[test]
    fn everything_else_stays_with_the_model() {
        let catalog = catalog();
        for question in [
            "create a tar archive of a folder with no compression",
            "show the log without the header",
            "list the files without recursion",
            "rerun a command every 2 seconds",
            "edit proxy.conf",
        ] {
            assert!(!specialist(question, Some(&catalog)), "{question}");
        }
        // without a catalogue any ruled-out word counts
        assert!(specialist("show the log without the header", None));
    }
}
