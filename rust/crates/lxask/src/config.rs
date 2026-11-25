//! Where the model and the docs index live.

use std::path::{Path, PathBuf};

pub const MODEL_FILENAME: &str = "model.gguf";
pub const EMBED_FILENAME: &str = "embed.gguf";
pub const INDEX_FILENAME: &str = "docs.sqlite";
/// the restriction adapter for the installed model (lxask_core::route)
pub const ADAPTER_FILENAME: &str = "restrict.gguf";
/// the precision adapter for every question the restriction adapter does not take
pub const PRECISION_FILENAME: &str = "precision.gguf";

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

/// The restriction adapter, if there is one; LXASK_ADAPTER=off answers without it.
pub fn adapter_path() -> Option<PathBuf> {
    if env("LXASK_ADAPTER").as_deref() == Some("off") {
        return None;
    }
    Some(resolve("LXASK_ADAPTER", ADAPTER_FILENAME)).filter(|path| path.exists())
}

/// The precision adapter, if there is one; LXASK_PRECISION=off answers without it.
pub fn precision_adapter_path() -> Option<PathBuf> {
    if env("LXASK_PRECISION").as_deref() == Some("off") {
        return None;
    }
    Some(resolve("LXASK_PRECISION", PRECISION_FILENAME)).filter(|path| path.exists())
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

/// The environment variable, else `key` in $LXASK_HOME/settings.
fn setting(env_var: &str, key: &str) -> Option<String> {
    env(env_var).or_else(|| {
        std::fs::read_to_string(data_home().join(SETTINGS_FILENAME))
            .ok()
            .and_then(|text| settings_value(&text, key))
            .filter(|v| !v.is_empty())
    })
}

/// Whether a question starts a server when none is running (`serve = auto` in the
/// settings install.sh writes, or LXASK_SERVE=auto|off); off when nothing says so.
pub fn serve_auto() -> bool {
    setting("LXASK_SERVE", "serve").as_deref().and_then(truthy).unwrap_or(false)
}

/// Whether commands are also checked against this machine's own manual pages
/// (LXASK_LOCAL_MAN=off or `local_man = off` turns it off); on when nothing says so.
pub fn local_man() -> bool {
    setting("LXASK_LOCAL_MAN", "local_man").as_deref().and_then(truthy).unwrap_or(true)
}

/// Whether a newer release is looked for once a day in the background
/// (LXASK_UPDATE_CHECK or `update_check = off` turns it off); on when nothing says so.
pub fn update_check() -> bool {
    setting("LXASK_UPDATE_CHECK", "update_check").as_deref().and_then(truthy).unwrap_or(true)
}

/// Whether an answer the checks found nothing wrong with gets a second look from the
/// model before it is shown (LXASK_VERIFY or `verify = off` turns it off); on when
/// nothing says so.
pub fn verify() -> bool {
    setting("LXASK_VERIFY", "verify").as_deref().and_then(truthy).unwrap_or(true)
}

/// The catalogue in the index at `index`, reading this machine's manual pages too
/// unless that is turned off.
pub fn catalog(index: &Path) -> Option<lxask_core::validate::Catalog> {
    let mut catalog = lxask_core::validate::load_catalog(index)?;
    if local_man() {
        let index = index.to_path_buf();
        let examples = move |program: &str| lxask_core::validate::example_commands(&index, program);
        catalog.read_local_pages(lxask_core::manpage::page_text, std::sync::Arc::new(examples));
    }
    Some(catalog)
}

/// How many minutes without a question before a server stops (0: never), from
/// LXASK_IDLE or `idle = ...` in the settings; Err with the text when it is not a number.
/// `--idle` overrides it.
pub fn idle_minutes() -> Option<Result<f64, String>> {
    setting("LXASK_IDLE", "idle").map(|text| parse_minutes(&text).ok_or(text))
}

fn parse_minutes(text: &str) -> Option<f64> {
    text.parse::<f64>().ok().filter(|n| n.is_finite() && *n >= 0.0)
}

/// Another machine's `lxask --serve --listen` to ask instead of a model here:
/// LXASK_SERVER=host:port, or `server = host:port` in the settings.
pub fn remote_server() -> Option<String> {
    setting("LXASK_SERVER", "server")
}

/// the secret a remote client must send (`lxask --serve --listen` creates it)
pub const TOKEN_FILENAME: &str = "token";

pub fn token_path() -> PathBuf {
    data_home().join(TOKEN_FILENAME)
}

/// The token for a server that listens on the network, or for asking one:
/// LXASK_TOKEN, else `token = ...` in the settings, else the file $LXASK_HOME/token.
pub fn token() -> Option<String> {
    token_with_source().map(|(token, _)| token)
}

/// The token and where it was found, for saying so.
pub fn token_with_source() -> Option<(String, String)> {
    if let Some(token) = env("LXASK_TOKEN") {
        return Some((token, "LXASK_TOKEN".into()));
    }
    let settings = data_home().join(SETTINGS_FILENAME);
    if let Some(token) =
        std::fs::read_to_string(&settings).ok().and_then(|text| settings_value(&text, "token")).filter(|t| !t.is_empty())
    {
        return Some((token, format!("`token =` in {}", settings.display())));
    }
    let path = token_path();
    std::fs::read_to_string(&path)
        .ok()
        .map(|text| text.trim().to_string())
        .filter(|t| !t.is_empty())
        .map(|token| (token, path.display().to_string()))
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

    #[test]
    fn idle_values() {
        assert_eq!(settings_value("serve = auto\nidle = 10 # minutes\n", "idle").as_deref(), Some("10"));
        assert_eq!(parse_minutes("10"), Some(10.0));
        assert_eq!(parse_minutes("0"), Some(0.0));
        assert_eq!(parse_minutes("0.5"), Some(0.5));
        assert_eq!(parse_minutes("-1"), None);
        assert_eq!(parse_minutes("10m"), None);
        assert_eq!(parse_minutes("inf"), None);
    }
}
