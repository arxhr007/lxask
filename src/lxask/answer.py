"""From a question to checked, explained steps. No I/O here: the model, the search and
the catalogue are passed in, so the whole flow can be tested without any of them.

    question -> split into tasks -> per task: retrieve -> command -> validate and check
             against the task (another try if either objects) -> explain -> safety

The flow is in the Rust core (lxask-core/src/answer.rs); this module gives it Python's
model and search callables and returns the steps as dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from . import _core
from .explain import Described
from .retrieve import Doc
from .split import Chat
from .validate import Catalog

Search = Callable[[str], Sequence[Doc]]
# Another answer to the same messages; the int is the attempt, so each try differs.
Resample = Callable[[list[dict[str, str]], int], "str | None"]
RETRIES = _core.RETRIES


@dataclass
class Step:
    task: str
    command: str = ""  # empty when the model produced nothing usable
    issues: list[str] = field(default_factory=list)  # what the validator could not confirm
    misses: list[str] = field(default_factory=list)  # what the task asks for and the command lacks
    closest: str = ""  # a documented example to fall back on, shown when there are issues
    described: list[Described] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # destructive-command warnings
    notes: list[Doc] = field(default_factory=list)  # what retrieval gave the model
    tries: int = 1  # answers asked for before this one was kept


def _step(row) -> Step:
    task, command, issues, misses, closest, described, warnings, notes, tries = row
    return Step(task, command, issues, misses, closest,
                [Described(program, summary, tuple(map(tuple, options))) for program, summary, options in described],
                warnings, [Doc(*note) for note in notes], tries)


def clarification(question: str) -> str | None:
    """The question to ask back when the request is too vague for one command ("add a
    proxy": for this shell, Git, APT, ...?), or None. The CLI asks it instead of answering."""
    return _core.clarification(question)


def split(question: str, chat: Chat, resample: Resample | None = None) -> list[str]:
    """The tasks of a request. A split that loses which files the request is about
    ("delete .log files" -> "delete files") makes every step answer the wrong task, so
    with `resample` it is tried again, and the split that keeps the most is used."""
    return _core.split(question, chat, resample)


def unhandled(question: str, steps: list[Step]) -> list[str]:
    """What the request asks for that no step's command has, for a multi-step answer
    (a single step is checked against the request itself)."""
    return _core.unhandled(question, steps)


def answer(question: str, chat: Chat, search: Search, catalog: Catalog | None,
           resample: Resample | None = None) -> list[Step]:
    """One step per task in the question. Without a catalogue, commands are not
    validated or explained. With `resample`, an answer that is missing, or that the
    validator or the task check objects to, gets up to RETRIES more tries, and the one
    with the fewest objections is kept (the first answer on a tie)."""
    return [_step(row) for row in _core.answer(question, chat, search, catalog, resample)]


def render_command(command: str) -> list[str]:
    """`$ ` before each command line, continuation lines aligned under it."""
    return _core.render_command(command)


def render(steps: list[Step], explanations: bool = True, question: str | None = None) -> str:
    """The answer as printed. A single step is shown bare; several are numbered, each
    headed by its task. With `question`, what the request asks for and no command
    has is noted at the end."""
    return _core.render(steps, explanations, question)
