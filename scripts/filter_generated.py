"""Keep the generated data that survives checking; report what was dropped and why.

    python scripts/filter_generated.py            # add --syntax to also run `bash -n` on every command

Reads data/generated/*.raw.jsonl (see generate_data.py) and writes
    seed/generated_tasks.jsonl       {"cmd", "question", "command", "source"}
    seed/generated_scenarios.jsonl   {"area", "request", "steps": [{"task", "command"}]}
    evalsets/forbidden_heldout.jsonl the `forbid` requests for the held-out programs
                                     (written once, then kept as a test set)
    data/generated/rejects.jsonl     everything dropped, with the reasons

An item is kept only if every command in it passes lxask.validate.check: known
Linux program, documented options, parseable. That says the command is well formed,
not that it does what the question asks. For that there is the `review` job: run
this, then `generate_data.py review`, then this again, and the tasks the reviewer
judged wrong are dropped too. Read a sample either way.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import nl2bash
import sources
from generate_data import SKIP_PAGES, review_key, scenario_literal_problems
from lxask import constraints
from lxask.answer import clarification
from lxask.index import program
from lxask.validate import Catalog, check

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "generated"
SEED = ROOT / "seed"

TASK_JOBS = ("tasks", "depth", "everyday", "constraints", "forbid", "contexts", "forbid_twins")  # generate_data.py jobs that write single tasks
SCENARIO_JOBS = ("scenarios", "long", "scenarios3", "forbid_scenarios")  # ... and those that write multi-part scenarios
FORBIDDEN_HELDOUT = ROOT / "evalsets" / "forbidden_heldout.jsonl"
MAX_STEPS = 6
# A step has to make sense alone, so it may not point back at an earlier one.
_POINTS_BACK = re.compile(
    r"\b(it|them|that (process|file|service|user|directory|one)|those (files|processes)|these (files|processes)|"
    r"the same (process|file)|found (above|earlier|previously)|from the previous step)\b",
    re.I,
)


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def text_problem(text: str, least_words: int) -> str | None:
    text = text.strip()
    if len(text.split()) < least_words:
        return "too short"
    if len(text) > 600:
        return "too long"
    return None


def command_problems(command: str, catalog: Catalog, syntax: bool) -> list[str]:
    command = command.strip()
    if not command:
        return ["empty command"]
    if "\n" in command:
        return ["more than one line"]
    return [f"{issue.kind}: {issue.detail}" for issue in check(command, catalog, syntax=syntax)]


def pair_misses(question: str, command: str) -> list[str]:
    """What constraints.check holds against a pair, minus its slips: those judge this
    machine (a yum task on an apt system) or a habit, not whether the pair agrees."""
    return [miss.detail if miss.kind == "forbidden" else f"command drops {miss.detail.split(';')[0][17:]}"
            for miss in constraints.check(question, command) if miss.kind != "slip"]


def ruled_out_run(record: dict, command: str) -> list[str]:
    """The programs the generator was told to rule out that the command runs anyway
    (the request's own wording is judged by constraints.check)."""
    ruled_out = set(record.get("seed", {}).get("forbid", []))
    return [f"runs the ruled-out {name}" for name in sorted(ruled_out & {p.lower() for p in constraints.programs_run(command)})]


def task_command_name(record: dict, command: str) -> str:
    """The page a task belongs to. `everyday` tasks were not written for a page, so they
    go by the program their command runs."""
    job = record.get("job", "tasks")
    if job == "tasks":
        return record["id"]
    if job == "depth":
        return record["seed"]["cmd"]
    return nl2bash.utility(command) or "unknown"


def load_reviews() -> dict[str, str]:
    """(question, command) -> the reviewer's reason, for the tasks it judged wrong."""
    wrong = {}
    for record in load(RAW / "review.raw.jsonl"):
        items = record["seed"]["items"]
        for verdict in record.get("verdicts", []):
            if verdict.get("verdict") == "wrong" and 1 <= verdict.get("n", 0) <= len(items):
                wrong[review_key(*items[verdict["n"] - 1])] = verdict.get("reason", "").strip() or "no reason given"
    return wrong


def filter_tasks(records: list[dict], catalog: Catalog, syntax: bool,
                 wrong: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """`wrong` holds the reviewer's objections (see `load_reviews`); those tasks are dropped."""
    kept, rejects, seen = [], [], set()
    for record in records:
        if record["id"] in SKIP_PAGES:  # generated before the page was excluded
            continue
        for item in record.get("items", []):
            if item.get("skip"):
                continue
            question, command = item.get("question", "").strip(), item.get("command", "").strip()
            reasons = command_problems(command, catalog, syntax)
            if problem := text_problem(question, 3):
                reasons.append(f"question {problem}")
            reasons += pair_misses(question, command)
            reasons += ruled_out_run(record, command)
            if record.get("job") == "forbid_twins" and constraints.forbidden(question):
                reasons.append("the twin still rules out " + ", ".join(constraints.forbidden(question)))
            if record.get("job") == "contexts" and clarification(question):
                reasons.append("would be asked back: names no context")
            key = re.sub(r"\W+", " ", question.lower()).strip()
            if key in seen:
                reasons.append("duplicate question")
            if not reasons:
                # Before the reviewer's verdict: were a rejected task to free its question
                # for a later twin, that twin would be kept without ever being reviewed.
                seen.add(key)
            if objection := (wrong or {}).get(review_key(question, command)):
                reasons.append(f"reviewer: {objection}")
            entry = {
                "cmd": task_command_name(record, command),
                "question": question,
                "command": command,
                "source": record.get("job", "tasks"),
                **({"heldout": record["seed"]["forbid"]} if record.get("seed", {}).get("heldout") else {}),
            }
            if reasons:
                rejects.append({"job": "tasks", **entry, "reasons": reasons})
            else:
                kept.append(entry)
    return kept, rejects


def load_retasks() -> dict[str, list[str]]:
    """request -> its rewritten tasks, from the `retask` job."""
    retasks = {}
    for record in load(RAW / "retask.raw.jsonl"):
        requests, counts = record["seed"]["requests"], record["seed"]["steps"]
        for item in record.get("items", []):
            n, tasks = item.get("n", 0), [task.strip() for task in item.get("tasks", [])]
            if 1 <= n <= len(requests) and len(tasks) == counts[n - 1] and all(tasks):
                retasks[requests[n - 1]] = tasks
    return retasks


def filter_scenarios(records: list[dict], catalog: Catalog, syntax: bool,
                     retasks: dict[str, list[str]] | None = None) -> tuple[list[dict], list[dict]]:
    """`retasks` replaces the tasks of the scenarios it names (see `load_retasks`)."""
    kept, rejects, seen = [], [], set()
    for record in records:
        area = record["seed"]["area"]
        for scenario in record.get("scenarios", []):
            request, steps = scenario.get("request", ""), scenario.get("steps", [])
            retasked = bool(retasks and request in retasks and len(retasks[request]) == len(steps))
            if retasked:
                steps = [{**step, "task": task} for step, task in zip(steps, retasks[request])]
            reasons = []
            if problem := text_problem(request, 5):
                reasons.append(f"request {problem}")
            if not 1 <= len(steps) <= MAX_STEPS:
                reasons.append(f"{len(steps)} steps")
            for step in steps:
                task = step.get("task", "")
                if text_problem(task, 3) or _POINTS_BACK.search(task):
                    reasons.append(f"step not self-contained: {task[:60]}")
                reasons += command_problems(step.get("command", ""), catalog, syntax)
                reasons += ruled_out_run(record, step.get("command", ""))
            if all(step.get("task") and step.get("command") for step in steps):
                reasons += scenario_literal_problems(request, steps)
            key = re.sub(r"\W+", " ", request.lower()).strip()
            if key in seen:
                reasons.append("duplicate request")
            entry = {
                "area": area,
                "request": request.strip(),
                "steps": [{"task": s.get("task", "").strip(), "command": s.get("command", "").strip()} for s in steps],
                "source": record.get("job", "scenarios"),
                **({"retasked": True} if retasked else {}),
            }
            if reasons:
                rejects.append({"job": "scenarios", **entry, "reasons": reasons})
            else:
                seen.add(key)
                kept.append(entry)
    return kept, rejects


def filter_fixes(records: list[dict], catalog: Catalog, syntax: bool) -> tuple[list[dict], list[dict]]:
    """Corrections from the `fix` job: kept if the new command passes every check and
    differs from the wrong one (a correction that repeats it means the reviewer was
    wrong, and the pair teaches nothing)."""
    kept, rejects = [], []
    for record in records:
        items = record["seed"]["items"]
        for fix in record.get("fixes", []):
            if not 1 <= fix.get("n", 0) <= len(items) or fix.get("skip"):
                continue
            question, wrong, reason, cmd = items[fix["n"] - 1]
            command = fix.get("command", "").strip()
            reasons = command_problems(command, catalog, syntax)
            reasons += pair_misses(question, command)
            if command.split() == wrong.split():
                reasons.append("same as the rejected command")
            entry = {"cmd": cmd, "question": question, "wrong": wrong, "command": command, "reason": reason}
            if reasons:
                rejects.append({"job": "fix", **entry, "reasons": reasons})
            else:
                kept.append(entry)
    return kept, rejects


def write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as out:
        for record in records:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")


def reason_kind(reason: str) -> str:
    """`undocumented-option: top: -l is not ...` -> `undocumented-option`, for counting."""
    head = reason.split(":", 1)[0]
    if head in {"unknown-program", "undocumented-option", "not-linux", "syntax"}:
        return head
    for literal_rule in ("command drops", "tasks lose", "command lacks", "task lacks"):
        if reason.startswith(literal_rule):
            return literal_rule + " a value"
    return reason.split(":")[0]


def report(name: str, kept: list[dict], rejects: list[dict]) -> None:
    total = len(kept) + len(rejects)
    if not total:
        print(f"{name}: nothing generated yet")
        return
    print(f"{name}: kept {len(kept)} of {total} ({len(kept) / total:.0%})")
    kinds = Counter(reason_kind(reason) for reject in rejects for reason in set(reject["reasons"]))
    for kind, count in kinds.most_common():
        print(f"  dropped for {kind}: {count}")
    details = Counter(r for reject in rejects for r in reject["reasons"] if r.startswith(("undocumented", "unknown", "not-linux")))
    for detail, count in details.most_common(12):
        print(f"    {count:4} {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--syntax", action="store_true", help="also parse every command with `bash -n` (slower)")
    args = parser.parse_args()

    catalog = sources.load_catalog()
    task_records = [record for job in TASK_JOBS for record in load(RAW / f"{job}.raw.jsonl")]
    scenario_records = [record for job in SCENARIO_JOBS for record in load(RAW / f"{job}.raw.jsonl")]
    wrong = load_reviews()
    retasks = load_retasks()
    tasks, task_rejects = filter_tasks(task_records, catalog, args.syntax, wrong)
    scenarios, scenario_rejects = filter_scenarios(scenario_records, catalog, args.syntax, retasks)
    fixes, fix_rejects = filter_fixes(load(RAW / "fix.raw.jsonl"), catalog, args.syntax)
    heldout = [task for task in tasks if "heldout" in task]
    tasks = [task for task in tasks if "heldout" not in task]
    if heldout and not FORBIDDEN_HELDOUT.exists():
        write(FORBIDDEN_HELDOUT, [{"q": t["question"], "forbid": t["heldout"], "reference": t["command"]} for t in heldout])
        print(f"wrote {FORBIDDEN_HELDOUT.name}: {len(heldout)} requests ruling out {', '.join(sorted({p for t in heldout for p in t['heldout']}))}")

    write(SEED / "generated_tasks.jsonl", tasks)
    write(SEED / "generated_scenarios.jsonl", scenarios)
    if fixes or (SEED / "generated_fixes.jsonl").exists():
        write(SEED / "generated_fixes.jsonl", fixes)
    write(RAW / "rejects.jsonl", task_rejects + scenario_rejects + fix_rejects)

    report("tasks", tasks, task_rejects)
    if tasks:
        per_program = Counter(program(task["cmd"]) for task in tasks)
        print(f"  {len(per_program)} programs covered; most: " + ", ".join(f"{p} {n}" for p, n in per_program.most_common(8)))
        print("  by source: " + ", ".join(f"{s} {n}" for s, n in Counter(task["source"] for task in tasks).most_common()))
        print(f"  reviewer objections on file: {len(wrong)}")
    report("scenarios", scenarios, scenario_rejects)
    if scenarios:
        lengths = Counter(len(scenario["steps"]) for scenario in scenarios)
        print("  steps per scenario: " + ", ".join(f"{k}: {v}" for k, v in sorted(lengths.items())))
        print("  by source: " + ", ".join(f"{s} {n}" for s, n in Counter(s["source"] for s in scenarios).most_common())
              + f"; tasks rewritten: {sum(bool(s.get('retasked')) for s in scenarios)} ({len(retasks)} rewrites on file)")
        print("  per area: " + ", ".join(f"{a} {n}" for a, n in Counter(s["area"] for s in scenarios).most_common()))
    report("fixes", fixes, fix_rejects)


if __name__ == "__main__":
    main()
