"""Spot commands that can destroy data, so the CLI can warn before the user runs them.

This is a pattern list, not a guarantee: a command that passes is not thereby safe.
The rules are in the Rust core (lxask-core/src/safety.rs).
"""

from __future__ import annotations

from . import _core


def find_warnings(command: str) -> list[str]:
    """Reasons `command` deserves a second look, in rule order, without duplicates."""
    return _core.find_warnings(command)
