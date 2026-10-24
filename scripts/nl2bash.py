"""Load the NL2Bash pairs and split them the same way everywhere."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "data" / "raw" / "nl2bash"

_ASSIGNMENT = re.compile(r"^\w+=")
_WRAPPERS = {"sudo", "time", "nohup", "env", "exec", "command", "builtin"}


def load_pairs(raw: Path = RAW) -> list[tuple[str, str]]:
    """(question, command) pairs, with exact duplicates removed."""
    questions = (raw / "all.nl").read_text(encoding="utf-8").splitlines()
    commands = (raw / "all.cm").read_text(encoding="utf-8").splitlines()
    assert len(questions) == len(commands), "all.nl and all.cm are out of step"
    return list(dict.fromkeys(zip(map(str.strip, questions), map(str.strip, commands))))


def is_eval(command: str) -> bool:
    """A stable ~10% of *commands* is held out; every phrasing of one goes the same way."""
    digest = hashlib.sha1(command.encode("utf-8")).digest()
    return digest[0] % 10 == 0


def utility(command: str) -> str:
    """The program a command line starts with: `sudo FOO=1 find . | wc -l` -> `find`."""
    for token in command.split():
        token = token.lstrip("({!")
        if not token or token in _WRAPPERS or _ASSIGNMENT.match(token):
            continue
        return token.rsplit("/", 1)[-1]
    return ""


_SEGMENTS = re.compile(r"\|\||&&|[|;`]|\$\(")


def utilities(command: str) -> list[str]:
    """Every program in a pipeline or command list, in order."""
    return [name for name in map(utility, _SEGMENTS.split(command)) if name]


def flags(command: str) -> set[str]:
    """The options a command line passes: `ls -la --color=auto` -> {-la, --color}."""
    return {token.split("=", 1)[0] for token in command.split() if re.match(r"--?[A-Za-z]", token)}
