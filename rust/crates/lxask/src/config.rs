//! Where the model and the docs index live.

use std::path::PathBuf;

pub const MODEL_FILENAME: &str = "model.gguf";
pub const EMBED_FILENAME: &str = "embed.gguf";
pub const INDEX_FILENAME: &str = "docs.sqlite";
/// the restriction adapter for the installed model (lxask_core::route)
pub const ADAPTER_FILENAME: &str = "restrict.gguf";

/// `LXASK_*` names also read their old `LINU_*` spelling (lxask_core::setting).
fn env(name: &str) -> Option<String> {
    match name.strip_prefix("LXASK_") {
        Some(setting) => lxask_core::setting(setting),
        None => std::env::var(name).ok(),
    }
    .filter(|v| !v.is_empty())
}

pub fn home_dir() -> PathBuf {
    env("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

pub fn data_home() -> PathBuf {
    if let Some(home) = env("LXASK_HOME") {
        return PathBuf::from(home);
    }
    let base = env("XDG_DATA_HOME").map(PathBuf::from).unwrap_or_else(|| home_dir().join(".local").join("share"));
    let home = base.join("lxask");
    // installed as linu (0.8 and earlier) and not moved yet: install.sh moves it
    let old = base.join("linu");
    if !home.exists() && old.join(MODEL_FILENAME).exists() {
        return old;
    }
    home
}

/// The explicit environment variable, else the installed location.
fn resolve(env_var: &str, filename: &str) -> PathBuf {
    env(env_var).map(PathBuf::from).unwrap_or_else(|| data_home().join(filename))
}

pub fn model_path() -> PathBuf {
    resolve("LXASK_MODEL", MODEL_FILENAME)
}

pub fn embed_model_path() -> PathBuf {
    resolve("LXASK_EMBED_MODEL", EMBED_FILENAME)
}

pub fn index_path() -> PathBuf {
    resolve("LXASK_INDEX", INDEX_FILENAME)
}

/// The adapter, if there is one; LXASK_ADAPTER=off answers without it.
pub fn adapter_path() -> Option<PathBuf> {
    if env("LXASK_ADAPTER").as_deref() == Some("off") {
        return None;
    }
    Some(resolve("LXASK_ADAPTER", ADAPTER_FILENAME)).filter(|path| path.exists())
}

/// the choices install.sh asked for, as `key = value` lines
pub const SETTINGS_FILENAME: &str = "settings";

/// `key` from $LXASK_HOME/settings (`# comments` and blank lines ignored).
fn settings_value(text: &str, key: &str) -> Option<String> {
    text.lines()
        .map(|line| line.split('#').next().unwrap_or("").trim())
        .filter_map(|line| line.split_once('='))
        .filter(|(k, _)| k.trim() == key)
        .map(|(_, v)| v.trim().to_string())
        .last()
}

fn truthy(value: &str) -> Option<bool> {
    match value.to_ascii_lowercase().as_str() {
        "auto" | "on" | "yes" | "true" | "1" => Some(true),
        "off" | "no" | "false" | "0" => Some(false),
        _ => None,
    }
}

/// Whether a question starts a server when none is running (`serve = auto` in the
/// settings install.sh writes, or LXASK_SERVE=auto|off); off when nothing says so.
pub fn serve_auto() -> bool {
    if let Some(value) = env("LXASK_SERVE").as_deref().and_then(truthy) {
        return value;
    }
    std::fs::read_to_string(data_home().join(SETTINGS_FILENAME))
        .ok()
        .and_then(|text| settings_value(&text, "serve"))
        .as_deref()
        .and_then(truthy)
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings_lines() {
        let text = "# written by install.sh\nserve = auto\n\nother=1 # note\n";
        assert_eq!(settings_value(text, "serve").as_deref(), Some("auto"));
        assert_eq!(settings_value(text, "other").as_deref(), Some("1"));
        assert_eq!(settings_value("serve=off\nserve=auto\n", "serve").as_deref(), Some("auto"));
        assert_eq!(settings_value("# serve = auto\n", "serve"), None);
        assert_eq!(truthy("AUTO"), Some(true));
        assert_eq!(truthy("off"), Some(false));
        assert_eq!(truthy("sometimes"), None);
    }
}
