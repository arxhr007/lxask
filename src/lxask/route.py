"""Which requests the restriction adapter answers: those that rule a program out
("without lsof") or mention a proxy. The adapter keeps those far better than the model
alone but drifts on everyday requests, so every other request gets the model unchanged.
The rule is in the Rust core (lxask-core route.rs)."""

from __future__ import annotations

from . import _core
from .validate import Catalog


def specialist(question: str, catalog: Catalog | None = None) -> bool:
    """Whether `question` goes to the adapter. A ruled-out word counts only if the
    catalogue knows it as a program (without a catalogue, any ruled-out word)."""
    return _core.specialist(question, catalog)
