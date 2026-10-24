"""Describe a command from the documentation.

No model is involved: every line comes from a tldr summary or a man page, and where
the documentation says nothing, nothing is printed. So an explanation can be
incomplete, but it cannot be made up. The work is done in the Rust core
(lxask-core/src/explain.rs).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import _core
from .validate import Catalog

MAX_CHARS = _core.MAX_CHARS


@dataclass(frozen=True)
class Described:
    program: str
    summary: str
    options: tuple[tuple[str, str], ...]  # (option, what the manual says it does)


def brief(text: str, limit: int = MAX_CHARS) -> str:
    """The first sentence, without its full stop, cut at a word if it runs long. A
    sentence that leads into a list (`File is of type c:`) loses its colon."""
    return _core.brief(text, limit)


def explain(command: str, catalog: Catalog) -> list[Described]:
    """What each program in `command` is, and what each documented option it is given does."""
    return [Described(program, summary, tuple(map(tuple, options)))
            for program, summary, options in _core.explain_command(command, catalog)]


def render(described: list[Described]) -> list[str]:
    """Lines for the terminal: each program with its summary, its options beneath it,
    in columns wide enough for the longest name."""
    return _core.render_described(described)
