//! `lxask --shell-init bash|zsh`: a key binding for the shell. Type what you want to do
//! at the prompt, press Ctrl+G, and the line is replaced by the command, to read, edit
//! and run yourself; nothing is run. What lxask says about the command (unverified,
//! warnings), a question back or the about text goes to the terminal, and the line is
//! left as it was when there is no command.

const BASH: &str = r#"# lxask: type what you want to do, press Ctrl+G, and the line becomes the command.
# Nothing runs until you press Enter. In ~/.bashrc: eval "$(lxask --shell-init bash)"
__lxask_widget() {
    local request="$READLINE_LINE" command
    [[ -n "${request//[[:space:]]/}" ]] || return
    printf '\033[2mlxask: %s\033[0m\n' "$request" >&2
    command=$(lxask --command-only -- "$request")
    if [[ -n "$command" ]]; then
        READLINE_LINE=$command
        READLINE_POINT=${#READLINE_LINE}
    fi
}
bind -x '"\C-g": __lxask_widget'
"#;

const ZSH: &str = r#"# lxask: type what you want to do, press Ctrl+G, and the line becomes the command.
# Nothing runs until you press Enter. In ~/.zshrc: eval "$(lxask --shell-init zsh)"
__lxask_widget() {
    local request=$BUFFER command
    [[ -n ${request//[[:space:]]/} ]] || return
    zle -I
    print -u2 -- $'\e[2mlxask: '"$request"$'\e[0m'
    command=$(lxask --command-only -- "$request")
    if [[ -n $command ]]; then
        BUFFER=$command
        CURSOR=${#BUFFER}
    fi
    zle reset-prompt
}
zle -N __lxask_widget
bindkey '^G' __lxask_widget
"#;

/// The script for `shell`, or None for a shell without one.
pub fn init(shell: &str) -> Option<&'static str> {
    match shell {
        "bash" => Some(BASH),
        "zsh" => Some(ZSH),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    #[test]
    fn bash_and_zsh_only() {
        assert!(init("bash").unwrap().contains(r#"bind -x '"\C-g": __lxask_widget'"#));
        assert!(init("zsh").unwrap().contains("bindkey '^G' __lxask_widget"));
        assert!(init("fish").is_none());
    }

    #[test]
    fn the_scripts_parse() {
        for (shell, script) in [("bash", BASH), ("zsh", ZSH)] {
            // a shell that is not installed is skipped (zsh is often missing)
            if let Ok(out) = Command::new(shell).args(["-n", "-c", script]).output() {
                assert!(out.status.success(), "{shell}: {}", String::from_utf8_lossy(&out.stderr));
            }
        }
    }
}
