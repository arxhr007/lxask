"""The execution harness, and the task file itself: every task must be solvable by a
known-good command and must tell that command from a wrong one.

Only commands written here are run, in pytest's temporary directory. Model-written
commands are never executed by the tests.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import exec_eval  # noqa: E402

TASKS = {
    task["id"]: task
    for task in map(json.loads, (Path(__file__).resolve().parents[1] / "evalsets" / "exec_tasks.jsonl").read_text(encoding="utf-8").splitlines())
}

REFERENCE = {
    "count-lines": "wc -l data.txt",
    "head-3": "head -n 3 data.txt",
    "tail-2": "tail -n 2 data.txt",
    "lines-5-7": "sed -n '5,7p' data.txt",
    "find-log": "find logs -name '*.log'",
    "find-big": "find data -type f -size +1M",
    "find-empty": "find work -type f -empty",
    "count-matches": "grep -c error app.log",
    "grep-nocase": "grep -i error app.log",
    "grep-invert": "grep -v backup hosts.txt",
    "files-with-todo": "grep -l TODO docs/*",
    "sort-alpha": "sort names.txt",
    "sort-numeric-desc": "sort -nr numbers.txt",
    "unique-sorted": "sort -u dupes.txt",
    "count-distinct": "sort words.txt | uniq -c | sort -nr",
    "csv-column": "cut -d, -f2 data.csv",
    "tsv-first-unique": "cut -f1 users.tsv | sort -u",
    "uppercase": "tr a-z A-Z < shout.txt",
    "word-count": "wc -w essay.txt",
    "count-files": "ls docs | wc -l",
    "sed-in-place": "sed -i 's/foo/bar/g' config.txt",
    "mkdir-parents": "mkdir -p backup/2024/jan",
    "copy": "cp report.txt report.bak",
    "rename": "mv old.txt new.txt",
    "delete-tmp": "rm ./*.tmp",
    "delete-tree": "rm -r build",
    "tar-create": "tar czf site.tar.gz site",
    "tar-extract": "tar xzf archive.tar.gz -C out",
    "chmod-x": "chmod +x script.sh",
    "touch": "touch marker",
    "symlink": "ln -s releases/v2 current",
    "append": "echo done >> notes.txt",
    "concatenate": "cat a.txt b.txt > all.txt",
    "no-wc-lines": "awk 'END {print NR}' data.txt",
    "no-head": "sed -n '1,4p' data.txt",
    "no-tail": "awk '{line[NR] = $0} END {for (i = NR - 2; i <= NR; i++) print line[i]}' data.txt",
    "no-grep": "awk '/error/' app.log",
    "no-find": "shopt -s globstar; printf '%s\\n' logs/**/*.log",
    "no-rm": "unlink old.txt",
    "no-cp": "cat report.txt > report.bak",
    "no-wc-words": "awk '{n += NF} END {print n}' essay.txt",
    "question-only": "ls docs/*.txt | wc -l",
    "gzip-keep": "gzip -k big.log",
    "delete-tmp-only": "rm scratch/*.tmp",
    "cat-txt-only": "cat notes/*.txt",
    "chmod-one": "chmod u+x run.sh",
}

WRONG = {
    "head-3": "head -n 4 data.txt",
    "find-log": "find logs -name '*.txt'",
    "find-big": "find data -type f",
    "grep-invert": "grep backup hosts.txt",
    "sort-numeric-desc": "sort numbers.txt",
    "count-distinct": "sort words.txt | uniq -c",
    "csv-column": "cut -d, -f1 data.csv",
    "sed-in-place": "sed 's/foo/bar/g' config.txt",
    "rename": "cp old.txt new.txt",
    "delete-tmp": "rm ./*.txt",
    "tar-extract": "tar tzf archive.tar.gz",
    "symlink": "ln -s releases current",
    "append": "echo done > notes.txt",
    # right-looking output, wrong otherwise: what the strict checks catch
    "count-lines": "wc -l data.txt; false",                        # exit status 1
    "count-matches": "grep -c error app.log | tee count.txt",      # a question that writes a file
    "no-wc-lines": "wc -l data.txt",                               # ruled out by the task
    "no-head": "head -n 4 data.txt",
    "no-grep": "grep error app.log",
    "no-find": "find logs -name '*.log'",
    "no-rm": "rm old.txt",
    "no-cp": "cp report.txt report.bak",
    "no-wc-words": "wc -w essay.txt",
    "question-only": "ls docs/*.txt > list; wc -l < list",
    "gzip-keep": "gzip -c big.log > big.log.gz; truncate -s 100 big.log",  # "keeps" it, damaged
    "delete-tmp-only": "find scratch -name '*.tmp' -delete",        # also the one in sub/
    "cat-txt-only": "cat notes/*",                                 # cat: notes/sub: Is a directory
    "chmod-one": "chmod u+x run.sh data.txt",
}

needs_bash = pytest.mark.skipif(shutil.which("bash") is None or sys.platform == "win32", reason="needs a Linux shell")


def solve(task: dict, command: str, directory: Path) -> bool:
    """What `exec_eval.attempt` does, minus the switch to `nobody` and the separate HOME."""
    for line in task.get("setup", []):
        exec_eval.run(line, directory, None)
    before = exec_eval.snapshot(directory)
    result = exec_eval.run(command, directory, None)
    changed = exec_eval.changed_paths(before, exec_eval.snapshot(directory))
    if exec_eval.violations(task, command, result.returncode, changed, []):
        return False
    if not exec_eval.output_matches(task.get("expect", {}), result.stdout):
        return False
    return "check" not in task or exec_eval.run(task["check"], directory, None).returncode == 0


def test_violations():
    task = {"q": "count the lines in data.txt without wc", "expect": {"number": 3}}
    assert exec_eval.violations(task, "awk 'END {print NR}' data.txt", 0, [], []) == []
    assert exec_eval.violations(task, "wc -l data.txt", 0, [], []) == ["runs wc, which the task rules out"]
    assert exec_eval.violations(task, "awk 'END {print NR}' data.txt", 2, ["out.txt"], [".lesshst"]) == [
        "exit status 2", "changed out.txt", "wrote .lesshst into the home directory"]
    keep = {"q": "empty a.bin, keep b.bin", "check": "true", "unchanged": ["b.bin", "dir/*"]}
    assert exec_eval.violations(keep, "x", 0, ["a.bin"], []) == []
    assert exec_eval.violations(keep, "x", 0, ["a.bin", "dir/c"], []) == ["changed dir/c, which the task keeps"]
    assert exec_eval.violations({"q": "grep", "exit_ok": [0, 1]}, "grep x f", 1, [], []) == []


def test_every_task_has_a_reference_solution():
    assert set(REFERENCE) == set(TASKS)


@needs_bash
@pytest.mark.parametrize("task_id", sorted(REFERENCE))
def test_reference_solution_passes(task_id, tmp_path):
    assert solve(TASKS[task_id], REFERENCE[task_id], tmp_path)


@needs_bash
@pytest.mark.parametrize("task_id", sorted(WRONG))
def test_wrong_command_fails(task_id, tmp_path):
    assert not solve(TASKS[task_id], WRONG[task_id], tmp_path)


def test_output_matching():
    match = exec_eval.output_matches
    assert match({"lines": ["3 apple", "2 pear"]}, "      3 apple\n      2 pear\n\n")
    assert not match({"lines": ["2 pear", "3 apple"]}, "3 apple\n2 pear\n")
    assert match({"set": ["logs/a.log", "logs/b.log"]}, "./logs/b.log\n./logs/a.log\n")
    assert not match({"set": ["logs/a.log"]}, "logs/a.log\nlogs/b.log\n")
    assert match({"number": 137}, "137 data.txt\n") and not match({"number": 137}, "1370\n")
    assert match({"contains": ["a"], "absent": ["b"]}, "a\nc\n") and not match({"contains": ["a"], "absent": ["b"]}, "a\nb\n")
    assert match({}, "anything")  # tasks judged by their `check` alone


def test_refuses_to_execute_outside_a_throwaway_environment(monkeypatch):
    monkeypatch.setattr(exec_eval.Path, "exists", lambda self: False)  # no /.dockerenv
    assert "LXASK_ALLOW_EXEC" in exec_eval.sandbox_problem({}, euid=0)
    assert "refusing" in exec_eval.sandbox_problem({"LXASK_ALLOW_EXEC": "1"}, euid=0)
    assert "root" in exec_eval.sandbox_problem({"LXASK_ALLOW_EXEC": "1", "COLAB_RELEASE_TAG": "x"}, euid=1000)
    assert exec_eval.sandbox_problem({"LXASK_ALLOW_EXEC": "1", "COLAB_RELEASE_TAG": "x"}, euid=0) is None
