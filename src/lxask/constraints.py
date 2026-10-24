"""Does a command keep what the task asked for?

The validator checks that a command is well formed. This checks something it cannot:
that the literal details of the task survive into the command. "Delete .log files
older than 30 days under /var/log/myapp" names a file pattern, an age and a path; a
command without `log`, without a 30-day age or without that path has lost part of
the request, however valid it is.

Only literals are checked, and each in the forms a command can write it (30 days is
`-mtime +30`, `-mmin +43200` or `--since '30 days ago'`). A command can still get the
meaning wrong with every literal in place. Plus a short list of known slips that are
valid but wrong (`ps ... | head -n 1` shows only the header), and the repairs of those
that have one right fix.

The rules are in the Rust core (lxask-core/src/constraints.rs), shared with the `lxask`
binary. LXASK_PACKAGE_MANAGERS and LXASK_AS_ROOT override what it finds on the machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import _core


@dataclass(frozen=True)
class Miss:
    kind: str  # duration | size | count | port | extension | filename | path | home | text | forbidden | slip
    detail: str

    def __str__(self) -> str:
        return self.detail


def requirements(task: str) -> list[tuple[str, str, object]]:
    """(kind, as written in the task, what to look for) for each literal in the task."""
    return [tuple(entry) for entry in _core.requirements(task)]


def repair(command: str, task: str = "") -> str:
    """Fix the known slips that have one right fix: `ps ... | head -n 1` keeps the
    header and the first process with `head -n 2`; `grep` on a directory gets `-r`; a
    bare crontab entry is installed with `crontab`. With the task: a tldr placeholder
    `path/to/build` for the "build" the task names becomes `build`, a bare `head`
    or `tail` gets the task's "first N lines" or "last N lines", `find -mtime 0`
    for "the last hour" becomes `-mmin -60`, another distribution's listing of the
    installed packages becomes this system's, and a command that always needs root
    gets `sudo` (unless lxask runs as root)."""
    return _core.repair(command, task)


def forbidden(text: str) -> list[str]:
    """The programs `text` rules out: "without lsof", "don't use sudo", "instead of
    curl", "netstat isn't installed"."""
    return _core.forbidden(text)


def programs_run(command: str) -> list[str]:
    """Every program a command line runs, wrapped ones included (`sudo lsof`, `xargs
    grep`, `find -exec rm`, `sh -c '...'`)."""
    return _core.programs_run(command)


def check(task: str, command: str, request: str = "") -> list[Miss]:
    """The literals of `task` that `command` does not contain, programs the task or the
    request ruled out that it runs, and known slips. `request` is the whole question a
    task was split from: context a slip check may need (a step says "the project
    directory" where the request said "in my home"), and restrictions a split dropped."""
    return [Miss(kind, detail) for kind, detail in _core.constraints_check(task, command, request)]
