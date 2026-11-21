//! Spot commands that can destroy data, so the CLI can warn before the user runs them.
//!
//! This is a pattern list, not a guarantee: a command that passes is not thereby safe.

use std::sync::OnceLock;

use crate::py::Re;

/// A flag is only attributed to a command within the same simple command, so the
/// lookaheads stop at pipes, `;` and `&`.
const SAME_CMD: &str = r"[^|;&]*";

fn rules() -> &'static [(Re, &'static str)] {
    static RULES: OnceLock<Vec<(Re, &'static str)>> = OnceLock::new();
    RULES.get_or_init(|| {
        let rule = |pattern: String, reason| (Re::plain(&pattern), reason);
        vec![
            rule(format!(r"\brm\b(?={SAME_CMD}\s(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b)"),
                 "rm -r deletes whole directory trees and cannot be undone"),
            rule(r"--no-preserve-root\b".into(), "--no-preserve-root lets the command operate on / itself"),
            rule(format!(r"\bdd\b(?={SAME_CMD}\bof=/dev/)"), "dd with of=/dev/... overwrites that device"),
            rule(r"\bmkfs(?:\.\w+)?\b|\bmke2fs\b|\bmkswap\b".into(), "formats the target, destroying what is on it"),
            rule(r"\b(?:wipefs|shred|blkdiscard)\b".into(), "irreversibly erases data"),
            rule(r"\b(?:fdisk|parted|sgdisk|sfdisk|cfdisk)\b".into(),
                 "edits the partition table; a mistake can lose every partition on the disk"),
            rule(r">\s*/dev/(?:sd|nvme|vd|hd|mmcblk)\w*".into(), "redirecting output into a block device overwrites it"),
            rule(format!(r"\bchmod\b(?={SAME_CMD}\s0?777\b)"), "chmod 777 makes the files writable by every user"),
            rule(format!(r"\b(?:chmod|chown|chgrp)\b(?={SAME_CMD}\s(?:-[a-zA-Z]*R[a-zA-Z]*|--recursive)\b)"),
                 "recursive permission or ownership changes are hard to revert"),
            rule(r":\(\)\s*\{.*\}\s*;\s*:".into(), "this is a fork bomb; it will hang the machine"),
            rule(r"\b(?:curl|wget)\b[^;&]*\|\s*(?:(?:sudo|doas)(?:\s+-\w+)*\s+)?(?:ba|z|da|k)?sh\b".into(),
                 "pipes a downloaded script straight into a shell without review"),
            rule(format!(r"\bfind\b(?={SAME_CMD}\s-delete\b)|\bfind\b(?={SAME_CMD}-(?:exec|execdir|ok|okdir)\s+(?:sudo\s+)?rm\b)"),
                 "deletes every file the find expression matches"),
            rule(r"\bxargs\b[^|;&]*\brm\b".into(), "deletes every file passed in by the pipeline"),
            rule(format!(r"\brsync\b(?={SAME_CMD}\s--(?:delete|del)(?:-\w+)*\b)"),
                 "rsync --delete removes files the source does not have from the destination"),
            rule(r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-zA-Z]*f|push\b[^|;&]*(?:--force\b|\s-f\b))".into(),
                 "discards or overwrites git history or uncommitted work"),
            rule(r"\bcrontab\s+-r\b".into(), "crontab -r removes all of your cron jobs"),
            rule(r"\b(?:userdel|groupdel|deluser)\b".into(), "removes an account"),
            rule(r"\b(?:shutdown|reboot|poweroff|halt)\b|\binit\s+[06]\b".into(), "shuts down or restarts the machine"),
            rule(r"\bkill\s+(?:-9|-KILL|-SIGKILL)\s+-1\b|\bkillall5\b".into(),
                 "kills every process you are allowed to signal"),
            rule(r"\b(?:iptables|nft)\b[^|;&]*(?:\s-F\b|\bflush\b)".into(),
                 "flushes firewall rules; over SSH this can lock you out or expose the host"),
        ]
    })
}

/// Reasons `command` deserves a second look, in rule order, without duplicates.
pub fn find_warnings(command: &str) -> Vec<String> {
    let mut found: Vec<String> = Vec::new();
    for (pattern, reason) in rules() {
        if pattern.is_match(command) && !found.iter().any(|f| f == reason) {
            found.push(reason.to_string());
        }
    }
    found
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deletions_by_other_spellings_are_flagged() {
        for command in [
            "find . -name '*.log' -execdir rm {} +",
            "find /tmp -name '*.tmp' -ok rm {} \\;",
            "find . -type f -exec sudo rm -f {} +",
            "curl -fsSL https://example.com/install.sh | sudo -E bash",
            "rsync -av --delete src/ /backup/",
            "rsync -a --delete-after src/ dst/",
        ] {
            assert!(!find_warnings(command).is_empty(), "{command}");
        }
        for command in ["rsync -av src/ /backup/", "find . -name '*.log' -execdir ls -l {} +", "rsync -av src/ dst/ ; echo --delete"] {
            assert!(find_warnings(command).is_empty(), "{command}");
        }
    }
}
