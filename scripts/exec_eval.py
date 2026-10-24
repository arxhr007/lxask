"""Run the model's commands for real and check what they did.

    LXASK_ALLOW_EXEC=1 python scripts/exec_eval.py --model ...
    LXASK_ALLOW_EXEC=1 python scripts/exec_eval.py --binary lxask   # the lxask program's answers

For each task in evalsets/exec_tasks.jsonl: build a small fixture directory, ask the
pipeline for a command, run it there as the unprivileged user `nobody` with a
timeout, then compare its output or the resulting files with what the task expects.
This is the only measure of whether a command does the job, and it only covers
file and text tasks, where running an unknown command can be contained.

Looking right is not enough to pass. The command must also exit 0 (or a status the
task allows, `exit_ok`), leave the fixture untouched when the task only asks a
question (no `check`), leave the task's `unchanged` paths as they were, write nothing
into the empty HOME, and run no program the request ruled out ("without lsof", or
the task's `forbid`). `loose_success` is the verdict without these checks.

It executes model-written shell commands. It therefore refuses to run unless
LXASK_ALLOW_EXEC=1 is set AND it is inside a throwaway environment (Google Colab or
a container) AND it is root, so that it can drop to `nobody`. It will not run on a
laptop by accident.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import pwd
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from lxask.constraints import forbidden, programs_run

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 10


def sandbox_problem(environ=os.environ, euid: int | None = None) -> str | None:
    """Why it is not safe to execute here, or None."""
    if environ.get("LXASK_ALLOW_EXEC") != "1":
        return "set LXASK_ALLOW_EXEC=1 to allow running model-written commands"
    disposable = any(environ.get(name) for name in ("COLAB_RELEASE_TAG", "COLAB_GPU", "LXASK_IN_CONTAINER"))
    if not disposable and not Path("/.dockerenv").exists():
        return "not in Google Colab or a container: refusing to run model-written commands on this machine"
    if (os.geteuid() if euid is None else euid) != 0:
        return "must be root, to drop privileges to `nobody` for each command"
    return None


def normal_lines(text: str) -> list[str]:
    """Output lines made comparable: trimmed, `./` dropped, runs of spaces collapsed, blanks gone."""
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if line:
            lines.append(line[2:] if line.startswith("./") else line)
    return lines


def output_matches(expect: dict, stdout: str) -> bool:
    lines = normal_lines(stdout)
    if "lines" in expect and lines != expect["lines"]:
        return False
    if "set" in expect and sorted(lines) != sorted(expect["set"]):
        return False
    if "number" in expect and str(expect["number"]) not in re.findall(r"\d+", stdout):
        return False
    if any(wanted not in lines for wanted in expect.get("contains", [])):
        return False
    return not any(unwanted in lines for unwanted in expect.get("absent", []))


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT, TIMEOUT))
    resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 2**20, 50 * 2**20))
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    os.setsid()


def run(command: str, cwd: Path, user: str | None, home: Path | None = None) -> subprocess.CompletedProcess:
    # HOME is an empty directory beside the fixture, not the fixture: the tasks name
    # directories relative to where the user is, and a command that guesses
    # "$HOME/data" for "the data directory" should not pass by luck.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home or cwd), "LC_ALL": "C.UTF-8"}
    # Output goes to files, not pipes: a process the command leaves in the background
    # (`cmd &`) would hold a pipe open and make a finished command look hung.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            ["bash", "-c", command], cwd=cwd, env=env, user=user, stdout=out, stderr=err,
            preexec_fn=_limits, stdin=subprocess.DEVNULL,
        )
        try:
            process.wait(timeout=TIMEOUT)
        finally:
            # Whatever the command left running (`tail -f`, `cmd &`) dies with it: left
            # alone, such processes pile up under `nobody` until it cannot fork at all.
            # (In a container, run with --init so the killed ones are reaped.)
            _kill_group(process.pid)
            if process.returncode is None:
                process.wait()
        out.seek(0)
        err.seek(0)
        read = lambda file: file.read(1 << 20).decode("utf-8", "replace")  # noqa: E731
        return subprocess.CompletedProcess(process.args, process.returncode, read(out), read(err))


def _kill_group(pid: int) -> None:
    """Kill the session the command ran in (_limits made it a group leader)."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def snapshot(root: Path) -> dict[str, tuple]:
    """Every entry under `root`, by relative path: its type, mode and content (a file's
    sha256, a link's target). Links are not followed."""
    entries: dict[str, tuple] = {}
    for directory, subdirectories, files in os.walk(root):
        for name in subdirectories + files:
            path = Path(directory, name)
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                entry: tuple = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                entry = ("dir", mode)
            elif stat.S_ISREG(info.st_mode):
                entry = ("file", mode, hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                entry = ("other", mode)
            entries[str(path.relative_to(root))] = entry
    return entries


def changed_paths(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    """Paths added, removed or changed between two snapshots."""
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def violations(task: dict, command: str, returncode: int, changed: list[str], home_entries: list[str]) -> list[str]:
    """What the command did that the task does not allow, however right its output."""
    found = []
    if returncode not in task.get("exit_ok", [0]):
        found.append(f"exit status {returncode}")
    if "check" not in task and changed:  # a question: nothing may change
        found.append(f"changed {', '.join(changed[:5])}")
    kept = [path for path in changed if any(fnmatch.fnmatch(path, pattern) for pattern in task.get("unchanged", []))]
    if kept:
        found.append(f"changed {', '.join(kept[:5])}, which the task keeps")
    if home_entries:
        found.append(f"wrote {', '.join(home_entries[:5])} into the home directory")
    ruled_out = set(forbidden(task["q"])) | set(task.get("forbid", []))
    used = [program for program in programs_run(command) if program.lower() in ruled_out]
    if used:
        found.append(f"runs {', '.join(used)}, which the task rules out")
    return found


def attempt(task: dict, command: str) -> dict:
    """Build the fixture, run `command` in it as nobody, and judge the outcome."""
    fixture = Path(tempfile.mkdtemp(prefix="lxask-exec-"))
    home = Path(tempfile.mkdtemp(prefix="lxask-home-"))
    try:
        nobody = pwd.getpwnam("nobody")
        for directory in (fixture, home):
            os.chown(directory, nobody.pw_uid, nobody.pw_gid)
        try:
            for line in task.get("setup", []):  # written by us, still run unprivileged
                run(line, fixture, "nobody")
        except subprocess.TimeoutExpired:
            return {"ran": False, "success": False, "loose_success": False, "error": "setup timed out"}
        before = snapshot(fixture)
        try:
            result = run(command, fixture, "nobody", home)
        except subprocess.TimeoutExpired:
            return {"ran": False, "success": False, "loose_success": False, "error": "timed out"}
        changed = changed_paths(before, snapshot(fixture))
        home_entries = sorted(str(path.relative_to(home)) for path in home.rglob("*"))
        loose = output_matches(task.get("expect", {}), result.stdout)
        if loose and "check" in task:
            try:
                loose = run(task["check"], fixture, "nobody").returncode == 0
            except subprocess.TimeoutExpired:
                loose = False
        violated = violations(task, command, result.returncode, changed, home_entries)
        return {"ran": result.returncode == 0, "success": loose and not violated, "loose_success": loose,
                "exit": result.returncode, "changed": changed[:20], "violated": violated,
                "stdout": result.stdout[:500], "stderr": result.stderr[:300]}
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def binary_commands(tasks: list[dict], binary: str, model: Path, index: Path, args) -> list[str]:
    """Each task's steps from `lxask batch`, joined as the Python pipeline's are."""
    with tempfile.TemporaryDirectory() as work:
        questions, answers = Path(work) / "questions.jsonl", Path(work) / "answers.jsonl"
        questions.write_text("".join(json.dumps({"q": task["q"]}) + "\n" for task in tasks), encoding="utf-8")
        command = [binary, "batch", "--model", str(model), "--index", str(index)]
        command += ["--no-retry"] * args.no_retry + ["--no-rag"] * args.no_rag
        command += ["--adapter", str(args.adapter)] if args.adapter else ["--no-adapter"]
        subprocess.run([*command, str(questions), str(answers)], check=True)
        rows = [json.loads(line) for line in answers.read_text(encoding="utf-8").splitlines()]
    if len(rows) != len(tasks) or any("error" in row for row in rows):
        sys.exit(f"exec_eval: lxask batch answered {len(rows)} of {len(tasks)}")
    return [" && ".join(step["command"] for step in row["steps"] if step["command"]) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--no-rag", action="store_true")
    parser.add_argument("--adapter", type=Path, help="the restriction adapter, applied to the requests lxask.route sends it")
    parser.add_argument("--name")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-retry", action="store_true", help="keep the first answer, as `lxask --no-retry` does")
    parser.add_argument("--tasks", type=Path, default=ROOT / "evalsets" / "exec_tasks.jsonl",
                        help="task file (default: evalsets/exec_tasks.jsonl; also evalsets/exec_heldout.jsonl)")
    parser.add_argument("--binary", help="answer with this lxask program (`lxask batch`) instead of the Python pipeline")
    args = parser.parse_args()

    if problem := sandbox_problem():
        sys.exit(f"exec_eval: {problem}")

    from lxask import config
    from lxask.answer import answer, clarification
    from lxask.llm import LLM, Embedder
    from lxask.retrieve import Retriever
    from lxask.route import specialist
    from lxask.validate import load_catalog

    model = args.model or config.model_path()
    name = args.name or f"{model.stem}-{'norag' if args.no_rag else 'rag'}-exec"
    tasks = [json.loads(line) for line in args.tasks.read_text(encoding="utf-8").splitlines() if line.strip()]
    tasks = tasks[: args.limit] if args.limit else tasks

    if args.binary:
        commands = binary_commands(tasks, args.binary, model, config.index_path(), args)
    else:
        catalog = load_catalog(config.index_path())
        search = lambda task: []  # noqa: E731
        if not args.no_rag:
            embed = Embedder(config.embed_model_path()) if config.embed_model_path().exists() else None
            search = Retriever(config.index_path(), embed=embed).search
        llm = LLM(model, adapter=args.adapter)

    records = []
    for i, task in enumerate(tasks):
        if args.binary:
            command = commands[i]
        else:  # a question the CLI would ask back gets no command, as there
            llm.specialize(specialist(task["q"], catalog))
            steps = [] if clarification(task["q"]) else answer(task["q"], llm.ask, search, catalog,
                                                                resample=None if args.no_retry else llm.sample)
            command = " && ".join(step.command for step in steps if step.command)
        outcome = attempt(task, command) if command else {"ran": False, "success": False, "loose_success": False,
                                                          "error": "no command"}
        records.append({"id": task["id"], "q": task["q"], "command": command, **outcome})
        why = f"  [{'; '.join(outcome['violated'])}]" if outcome.get("violated") else ""
        print(f"  {'PASS' if outcome['success'] else 'FAIL'} {task['id']:18} $ {command[:90]}{why}", flush=True)

    out_dir = ROOT / "runs"
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / f"{name}.jsonl", "w", encoding="utf-8") as out:
        for record in records:
            out.write(json.dumps(record) + "\n")
    summary = {
        "name": name,
        "n": len(records),
        "task_success": sum(r["success"] for r in records) / len(records),
        "loose_task_success": sum(r["loose_success"] for r in records) / len(records),
        "ran_without_error": sum(r["ran"] for r in records) / len(records),
    }
    (out_dir / f"{name}.summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"{name}: task success {summary['task_success']:.1%} (without the strict checks {summary['loose_task_success']:.1%}), "
          f"ran without error {summary['ran_without_error']:.1%} (n={summary['n']})")


if __name__ == "__main__":
    main()
