from __future__ import annotations

import hashlib
import json

TASK_FIELDS = ("id", "q", "setup", "expect", "check", "reference", "forbid", "unchanged", "exit_ok",
               "fixtures", "negatives", "family", "category", "source", "split", "seed")
FIXTURE_FIELDS = frozenset({"setup", "expect", "check", "unchanged"})
PROOF_VERSION = 1


def task_record(task: dict) -> dict:
    return {key: task[key] for key in TASK_FIELDS if key in task}


def task_variants(task: dict) -> list[dict]:
    if "fixtures" not in task:
        return [task]
    fixtures = task["fixtures"]
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("fixtures must be a nonempty list")
    base = {key: value for key, value in task.items() if key != "fixtures"}
    variants = []
    for fixture in fixtures:
        if not isinstance(fixture, dict) or set(fixture) - FIXTURE_FIELDS:
            raise ValueError("a fixture may only specify setup, expect, check and unchanged")
        variant = {**base, **fixture}
        variant["unchanged"] = list(dict.fromkeys([*base.get("unchanged", []), *fixture.get("unchanged", [])]))
        variants.append(variant)
    return variants


def task_digest(task: dict) -> str:
    content = json.dumps(task_record(task), sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def proved_record(task: dict) -> dict:
    return {**task, "proof": {"version": PROOF_VERSION, "sha256": task_digest(task)}}


def is_proved(task: dict) -> bool:
    return task.get("proof") == {"version": PROOF_VERSION, "sha256": task_digest(task)}
