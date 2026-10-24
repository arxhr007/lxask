"""Keep the generated execution tasks whose checks work.

    LXASK_ALLOW_EXEC=1 python scripts/prove_exec_tasks.py < data/generated/exec.raw.jsonl > proved.jsonl

Reads the `exec` job's raw output, turns each item into the format of
evalsets/exec_tasks.jsonl, and runs it the way exec_eval.py runs a model's command. A
task is kept only if its reference command passes twice and both `true` and an empty
command fail, so its check can tell a right answer from doing nothing. It runs the
generated setup lines and references, so it has the same sandbox rules as exec_eval.py
(Colab or a container, as root, commands as `nobody`).
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor  # subprocess preexec_fn is not thread-safe

from exec_eval import attempt, normal_lines, sandbox_problem

# Setup and checks are written by a model too: nothing that reaches out or escalates.
_FORBIDDEN = re.compile(r"\b(?:sudo|su|curl|wget|nc|ssh|scp|apt|apt-get|pip|dd|mkfs|mount|chown|reboot|shutdown)\b|/dev/(?!zero\b|null\b|urandom\b)")


def to_task(item: dict, task_id: str) -> tuple[dict | None, str]:
    """(task in exec_tasks.jsonl form, "") or (None, why it cannot be used)."""
    question, reference = item.get("q", "").strip(), item.get("reference", "").strip()
    setup = [line for line in item.get("setup", []) if line.strip()]
    check = item.get("check", "").strip()
    kind, values = item.get("expect_kind"), [line for value in item.get("expect_values", []) for line in normal_lines(value)]
    absent = [line for value in item.get("absent", []) for line in normal_lines(value)]
    if not question or not reference or "\n" in reference:
        return None, "no question or not one reference line"
    if any(_FORBIDDEN.search(line) for line in [*setup, check, reference]):
        return None, "forbidden command"
    expect: dict = {}
    if kind in ("lines", "set"):
        if not values:
            return None, "no expected lines"
        expect[kind] = values
    elif kind == "number":
        if len(values) != 1 or not re.fullmatch(r"-?\d+", values[0]):
            return None, "expected number is not one integer"
        expect["number"] = int(values[0])
    elif kind == "contains":
        if not values:
            return None, "nothing to contain"
        expect["contains"] = values
    elif kind != "none":
        return None, f"unknown expect_kind {kind!r}"
    if absent:
        expect["absent"] = absent
    if not expect and not check:
        return None, "neither expected output nor a check"
    task = {"id": task_id, "q": question, "setup": setup, "expect": expect, "reference": reference}
    if check:
        task["check"] = check
    return task, ""


def prove(task: dict) -> str:
    """"" if the task can tell right from wrong, else why not."""
    try:
        return _prove(task)
    except Exception as error:  # one broken task must not stop the rest
        return f"error: {type(error).__name__}"


def _prove(task: dict) -> str:
    for _ in range(2):
        if not attempt(task, task["reference"])["success"]:
            return "reference fails"
    for empty in ("true", ""):
        if attempt(task, empty)["success"]:
            return "passes without doing anything"
    return ""


def main() -> None:
    if problem := sandbox_problem():
        sys.exit(f"prove_exec_tasks: {problem}")
    tasks, reasons = [], Counter()
    for line in sys.stdin:
        if not line.strip():
            continue
        record = json.loads(line)
        for n, item in enumerate(record.get("tasks", [])):
            task, why = to_task(item, f"{record['id'].replace(':', '-')}-{n}")
            if task is None:
                reasons[why] += 1
            else:
                task["area"] = record.get("seed", {}).get("area", "")
                tasks.append(task)
    with ProcessPoolExecutor(8) as pool:
        verdicts = list(pool.map(prove, tasks, chunksize=8))
    kept = 0
    for task, why in zip(tasks, verdicts):
        if why:
            reasons[why] += 1
        else:
            print(json.dumps(task, ensure_ascii=False))
            kept += 1
    total = kept + sum(reasons.values())
    print(f"{kept} of {total} tasks kept", file=sys.stderr)
    for why, count in reasons.most_common():
        print(f"  {why}: {count}", file=sys.stderr)


if __name__ == "__main__":
    main()
