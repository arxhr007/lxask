"""Which adapter answers a request. The restriction adapter takes requests that rule a
program out ("without lsof") or mention a proxy — it keeps those far better than the
model alone. Everything else goes to the precision adapter, which beats the model on
exact filenames, flags and filters. The rule is in the Rust core
(lxask-core route.rs)."""

from __future__ import annotations

from . import _core
from .validate import Catalog


def specialist(question: str, catalog: Catalog | None = None) -> bool:
    """Whether `question` goes to the restriction adapter. A ruled-out word counts only
    if the catalogue knows it as a program (without a catalogue, any ruled-out word)."""
    return _core.specialist(question, catalog)


def adapter(question: str, catalog: Catalog | None = None) -> str:
    """Which adapter `question` goes to: "restrict" for restriction/proxy requests,
    "precision" for everything else. An adapter that is not installed means the model
    alone."""
    return _core.adapter(question, catalog)
