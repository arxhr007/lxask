"""Turn a request into the separate tasks in it.

A long request retrieves badly as a whole ("server is slow, find X, check Y, stop Z"
brings back notes about disk usage) and each part retrieves well on its own, so a
multi-part request is answered part by part. The model does the splitting; short
single-clause questions skip that call.
"""

from __future__ import annotations

from typing import Callable

from . import _core

# Below this many words, with nothing joining clauses, a question is one task.
SINGLE_TASK_WORDS = _core.SINGLE_TASK_WORDS

# The model, as far as this module cares: messages in, reply text out (None if it produced nothing usable).
Chat = Callable[[list[dict[str, str]]], "str | None"]


def needs_split(question: str) -> bool:
    return _core.needs_split(question)


def tasks_for(question: str, chat: Chat) -> list[str]:
    """The tasks in `question`, in order. Falls back to the question itself whenever the
    split is skipped or comes back empty, so there is always something to answer."""
    return _core.tasks_for(question, chat)
