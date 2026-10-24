"""Check a command line against the Linux documentation. Nothing is ever executed.

    issues = check("top -l 1 -s 10", catalog)
    # [Issue(program='top', kind='undocumented-option', detail='top: -l is not a documented option')]

What it can tell: the program exists, its options are documented, the line parses.
What it cannot tell: whether the command does what was asked. An empty result
means "nothing wrong was found", not "correct".

The validator and the catalogue are in the Rust core (lxask-core/src/validate.rs).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from . import _core

BUILTINS = _core.BUILTINS
NOT_LINUX_PROGRAMS = _core.NOT_LINUX_PROGRAMS
VERB_PROGRAMS = _core.VERB_PROGRAMS  # verbs checked against the manual (systemctl start)
MIN_OPTIONS = _core.MIN_OPTIONS
MIN_SUBCOMMAND_PAGES = _core.MIN_SUBCOMMAND_PAGES

# What the documentation knows: program names, and each program's options. Its
# programs, options, with_subcommands, texts and summaries read as Python copies.
Catalog = _core.Catalog


@dataclass(frozen=True)
class Issue:
    program: str
    kind: str  # "unknown-program" | "undocumented-option" | "not-linux" | "syntax"
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True)
class Use:
    """One program as a command line uses it: what `check` judges and `explain` describes."""

    program: str
    status: str  # "known" | "unknown" | "not-linux" | "add-on" | "unknown-verb"
    arguments: tuple[str, ...] = ()
    # (option as written, the documented options it stands for). None: not documented.
    # Empty: accepted without a manual entry of its own (`-9`, a signal name).
    options: tuple[tuple[str, tuple[str, ...] | None], ...] = ()


def parse_option_title(title: str) -> list[tuple[str, bool]]:
    """`-C, --directory=DIR` -> [("-C", True), ("--directory", True)]: each option, and
    whether it takes an argument (given on any spelling of it)."""
    return _core.parse_option_title(title)


def simple_commands(command: str) -> list[list[str]]:
    """Split a command line into its simple commands: pipelines, lists, `$(...)`, `find -exec`.
    Raises ValueError when the line cannot be parsed (unbalanced quotes)."""
    return _core.simple_commands(command)


def uses(command: str, catalog: Catalog) -> list[Use]:
    """Every program in `command`, in order, with the options it is given. Raises ValueError
    when the line cannot be parsed (unbalanced quotes)."""
    return [
        Use(program, status, tuple(arguments),
            tuple((written, None if flags is None else tuple(flags)) for written, flags in options))
        for program, status, arguments, options in _core.uses(command, catalog)
    ]


def bash_syntax_error(command: str) -> str | None:
    """bash's complaint about the line, or None. `bash -n` parses without running anything."""
    return _core.bash_syntax_error(command)


def check(command: str, catalog: Catalog, syntax: bool = False) -> list[Issue]:
    """Everything found wrong with `command`. `syntax=True` also asks bash to parse it."""
    return [Issue(*issue) for issue in _core.validate_check(command, catalog, syntax)]


def build_catalog(
    programs: Iterable[str],
    option_titles: Iterable[tuple[str, ...]],
    examples: Iterable[str] = (),
    summaries: Mapping[str, str] | None = None,
) -> Catalog:
    """A catalogue from program names, (page, option title[, description]) tuples,
    documented example commands and one-line page summaries."""
    return _core.build_catalog(list(programs), list(option_titles), list(examples), dict(summaries or {}))


def save_catalog(db_path: Path, catalog: Catalog) -> None:
    """Store the catalogue in the docs index, replacing any earlier copy."""
    _core.save_catalog(Path(db_path), catalog)


def load_catalog(db_path: Path) -> Catalog | None:
    """The catalogue stored in the docs index, or None if the index was built without one."""
    return _core.load_catalog(Path(db_path))
