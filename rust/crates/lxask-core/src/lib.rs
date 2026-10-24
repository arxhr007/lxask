//! lxask's answering logic, shared by the `lxask` binary and (through `lxask-py`) by the
//! Python training and evaluation scripts, so both run exactly the same rules.

pub mod py;

pub mod about;
pub mod answer;
pub mod clarify;
pub mod constraints;
pub mod explain;
pub mod looped;
pub mod prompt;
pub mod retrieve;
pub mod route;
pub mod safety;
pub mod shlex;
pub mod split;
pub mod validate;

/// An error from a model, an embedder or the index, passed through the answer flow.
pub type Error = Box<dyn std::error::Error + Send + Sync>;

/// The environment setting `LXASK_<name>`, else `LINU_<name>` (its name up to 0.8, when
/// lxask was called linu). An empty value is returned as set; callers decide what it means.
pub fn setting(name: &str) -> Option<String> {
    ["LXASK_", "LINU_"].iter().find_map(|prefix| std::env::var(format!("{prefix}{name}")).ok())
}
