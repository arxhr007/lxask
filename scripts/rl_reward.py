"""Rewards for reinforcement learning on executed commands (scripts/train_grpo.py).

`score` is for tasks that run in the sandbox; `score_reference` for ordinary questions
that cannot be run (services, packages, networking), scored against the training
reference so that learning from the sandbox does not pull the rest of the model away.

A sampled reply is scored by what its command did when run in the sandbox
(exec_server.py), less penalties for what the CLI would mark: options the validator
cannot confirm, literals of the task the command lacks, a destructive command for a
task that destroys nothing, and no command at all. The model's raw reply is scored:
no repair is applied, so it learns the fixes itself.
"""

from __future__ import annotations

import re
from statistics import mean, pstdev

from lxask import constraints
from lxask.prompt import parse_answer
from lxask.safety import find_warnings
from lxask.validate import Catalog, check, uses

SUCCESS = 1.0
UNVERIFIED = 0.3  # the validator objects
MISSES = 0.3  # a literal of the task is missing
UNASKED_DESTRUCTION = 0.5  # rm, -delete, ... for a task that asks for none of it
NO_COMMAND = 0.5
# Tasks that ask for something to be removed, moved or overwritten may get a warning.
_CHANGES = re.compile(
    r"\b(?:delet\w*|remov\w*|eras\w*|clean\w*|purg\w*|mov\w*|renam\w*|overwrit\w*|replac\w*|truncat\w*|"
    r"empty|wip\w*|compress\w*|gzip|unpack|extract\w*|in place|change the file)\b", re.I)


def score(question: str, reply: str, success: bool, catalog: Catalog | None) -> tuple[float, dict]:
    """(reward, what went into it) for one reply to `question`."""
    command = parse_answer(reply or "")
    if not command:
        return -NO_COMMAND, {"command": "", "success": False}
    issues = [str(issue) for issue in check(command, catalog)] if catalog is not None else []
    misses = [str(miss) for miss in constraints.check(question, command)]
    unasked = bool(find_warnings(command)) and not _CHANGES.search(question)
    reward = (SUCCESS if success else 0.0) - UNVERIFIED * bool(issues) - MISSES * bool(misses) - UNASKED_DESTRUCTION * unasked
    return reward, {"command": command, "success": success, "issues": issues, "misses": misses, "unasked_destruction": unasked}


_WRAPPERS = {"sudo", "doas", "time", "nohup", "env", "command", "exec"}
RIGHT_PROGRAM = 0.6
EXACT = 0.4  # the documented form, flag spelling included
SUDO_DROPPED = 0.2


def programs(command: str, catalog: Catalog | None) -> list[str]:
    """The programs a command line runs (wrappers first), as eval.py counts them."""
    if catalog is not None:
        try:
            return [use.program for use in uses(command, catalog)]
        except ValueError:
            pass
    found = []
    for segment in re.split(r"[|;&()]+", command):
        words = [word for word in segment.split() if not word.startswith("-")]
        while words and words[0] in _WRAPPERS:
            found.append(words.pop(0))
        if words and re.fullmatch(r"[a-z][\w.+-]*", words[0]):
            found.append(words[0])
    return found


def score_reference(question: str, reply: str, reference: str, catalog: Catalog | None) -> tuple[float, dict]:
    """(reward, details) for a question that cannot be run, against the training
    reference: the reference's main program used, an exact match, sudo kept where the
    reference needs it; less the same penalties as `score`."""
    command = parse_answer(reply or "")
    if not command:
        return -NO_COMMAND, {"command": "", "success": False}
    wanted = next((p for p in programs(reference, catalog) if p not in _WRAPPERS), "")
    right = bool(wanted) and wanted in programs(command, catalog)
    exact = command.split() == reference.split()
    sudo_dropped = bool(re.search(r"(?:^|[|;&(]\s*)sudo\b", reference)) and not re.search(r"\bsudo\b", command)
    issues = [str(issue) for issue in check(command, catalog)] if catalog is not None else []
    misses = [str(miss) for miss in constraints.check(question, command)]
    unasked = bool(find_warnings(command)) and not _CHANGES.search(question) and not find_warnings(reference)
    reward = (RIGHT_PROGRAM * right + EXACT * exact - SUDO_DROPPED * sudo_dropped - UNVERIFIED * bool(issues)
              - MISSES * bool(misses) - UNASKED_DESTRUCTION * unasked)
    return reward, {"command": command, "success": right, "exact": exact, "issues": issues, "misses": misses}


def advantages(rewards: list[float], group: int) -> list[float | None]:
    """Group-relative advantages: each reward against the others for the same task,
    scaled by their spread. None for every member of a group whose rewards are all
    equal, which carries no signal."""
    out: list[float | None] = []
    for start in range(0, len(rewards), group):
        chunk = rewards[start : start + group]
        spread = pstdev(chunk)
        if spread < 1e-6:
            out += [None] * len(chunk)
        else:
            centre = mean(chunk)
            out += [(r - centre) / (spread + 1e-4) for r in chunk]
    return out
