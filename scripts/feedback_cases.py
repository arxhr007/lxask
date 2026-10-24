"""Turn `lxask --bad` reports into draft regression cases.

    python scripts/feedback_cases.py            # reads $LXASK_HOME/feedback.jsonl

Writes evalsets/regressions_feedback.draft.jsonl: one case per reported question, in
the format of evalsets/regressions.jsonl, with the reported command as a pattern that
must not come back and `must_cmds` left empty. Fill in `must_cmds` (the programs a
right answer uses) by hand, then move the case into regressions.jsonl. Questions
already in the draft or in regressions.jsonl are skipped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from lxask import config

ROOT = Path(__file__).resolve().parents[1]
DRAFT = ROOT / "evalsets" / "regressions_feedback.draft.jsonl"


def draft_case(report: dict) -> dict:
    commands = [step["command"] for step in report.get("steps", []) if step.get("command")]
    why = f"reported wrong ({report.get('model', '?')}, {report.get('reported', '')[:10]})"
    if report.get("note"):
        why += f": {report['note']}"
    return {"q": report["question"], "why": why,
            "must_not": [re.escape(command) for command in commands], "must_cmds": []}


def main() -> None:
    source = config.data_home() / "feedback.jsonl"
    if not source.exists():
        print(f"no reports yet ({source} does not exist); `lxask --bad` writes them")
        return
    seen = set()
    for path in (ROOT / "evalsets" / "regressions.jsonl", DRAFT):
        if path.exists():
            seen |= {json.loads(line)["q"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    new = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.strip():
            report = json.loads(line)
            if report.get("question") and report["question"] not in seen:
                seen.add(report["question"])
                new.append(draft_case(report))
    with open(DRAFT, "a", encoding="utf-8", newline="\n") as out:
        for case in new:
            out.write(json.dumps(case, ensure_ascii=False) + "\n")
    print(f"{len(new)} new draft cases -> {DRAFT.relative_to(ROOT)}; fill in must_cmds, then move them to regressions.jsonl")


if __name__ == "__main__":
    main()
