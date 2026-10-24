"""Run model-written commands for train_grpo.py: one JSON batch per line in, results out.

    docker run --rm -i --init --network none -e LXASK_ALLOW_EXEC=1 IMAGE python scripts/exec_server.py

(--init: something has to reap the processes the sandbox kills.)

Each input line is a list of {"task": <exec task>, "command": "..."}; each output line
is the list of {"success": bool, "ran": bool} for it, in order. The commands run as in
exec_eval.py (fixture per command, as `nobody`, timeout), so this has the same sandbox
rules and refuses to start outside Colab or a container.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor  # subprocess preexec_fn is not thread-safe

from exec_eval import attempt, sandbox_problem


def run_one(job: dict) -> dict:
    if not job.get("command"):
        return {"success": False, "ran": False}
    try:
        outcome = attempt(job["task"], job["command"])
    except Exception as error:  # a malformed command must not end the server
        return {"success": False, "ran": False, "error": str(error)[:200]}
    return {"success": bool(outcome["success"]), "ran": bool(outcome["ran"])}


def serve(lines, out, workers: int = 8) -> None:
    with ProcessPoolExecutor(workers) as pool:
        for line in lines:
            if not line.strip():
                continue
            jobs = json.loads(line)
            out.write(json.dumps(list(pool.map(run_one, jobs))) + "\n")
            out.flush()


def main() -> None:
    if problem := sandbox_problem():
        sys.exit(f"exec_server: {problem}")
    serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
