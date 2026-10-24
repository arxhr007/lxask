"""The prompt formats, shared by inference, evaluation and fine-tuning.

The model does two jobs, told apart by the system prompt:
  SPLIT   a request -> the separate tasks in it, one per line
  ANSWER  one task plus reference notes -> one command line in a bash block

It writes no prose. What a command does is described by `lxask.explain`, from the
documentation. The formats live in the Rust core (lxask-core), so the `lxask` binary
and the training data are built from the same text.
"""

from __future__ import annotations

from typing import Iterable

from . import _core
from .retrieve import Doc

ANSWER_SYSTEM = _core.ANSWER_SYSTEM
SPLIT_SYSTEM = _core.SPLIT_SYSTEM
MAX_CONTEXT_CHARS = _core.MAX_CONTEXT_CHARS
MAX_TASKS = _core.MAX_TASKS


def format_doc(doc: Doc) -> str:
    return _core.format_doc(doc)


def format_context(docs: Iterable[Doc], max_chars: int = MAX_CONTEXT_CHARS) -> str:
    return _core.format_context(list(docs), max_chars)


def build_messages(task: str, docs: Iterable[Doc] = ()) -> list[dict[str, str]]:
    """The ANSWER prompt for one task."""
    return _core.build_messages(task, list(docs))


def format_answer(command: str) -> str:
    """The reply the model is trained to give; `parse_answer` reads it back."""
    return _core.format_answer(command)


def parse_answer(text: str) -> str:
    """The command in a model reply, or "" if it gave none. A block left open (cut off
    by the token limit) still counts."""
    return _core.parse_answer(text)


def build_split_messages(request: str) -> list[dict[str, str]]:
    """The SPLIT prompt for a request."""
    return _core.build_split_messages(request)


def format_tasks(tasks: Iterable[str]) -> str:
    """The reply the model is trained to give; `parse_tasks` reads it back."""
    return _core.format_tasks(list(tasks))


def parse_tasks(text: str, limit: int = MAX_TASKS) -> list[str]:
    """The tasks in a model reply: one per bulleted or numbered line, duplicates dropped."""
    return _core.parse_tasks(text, limit)
