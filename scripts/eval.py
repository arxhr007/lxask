"""Score a model end to end, through the same pipeline the CLI runs.

    python scripts/eval.py --model ~/.local/share/lxask/model.gguf
    python scripts/eval.py --model ... --no-rag --only test_everyday multistep
    python scripts/eval.py --engine rust --binary rust/target/release/lxask   # the binary itself

Sets (evalsets/*.jsonl). `everyday` was used to tune retrieval and is a dev set;
the rest are not tuned against.

  everyday, test_everyday   single questions with the accepted programs
  tldr_heldout              tldr examples kept out of round-1 training, with a reference
                            command (round 2's generator saw these pages: a weaker test now)
  nl2bash_test              held-out NL2Bash with Linux-valid reference commands
  multistep                 multi-part requests with the programs accepted for each part
                            and patterns that must not appear
  multistep_heldout         generated scenarios set aside before training, with reference steps
  multistep3_heldout        the same for round 3's scenarios, whose tasks keep every value
  regressions               earlier failures; each must pass
  forbidden                 requests that rule a program out ("without lsof"); pass = an
                            accepted program and nothing ruled out
  forbidden_heldout         generated requests ruling out programs no training example
                            rules out (sort, cut, ping, ...); pass = nothing ruled out
  ambiguous                "add a proxy": the vague forms must be asked back, the forms
                            naming a context (Git, APT, ...) answered in its form

Reported per set, each on its own because they answer different questions:

  answered     a command was produced for every step
  program      the right program is used (single-question sets)
  valid        share of commands the validator finds nothing wrong with
               (known Linux program, documented options)
  clean        ... and that also keep every literal of their task (lxask.constraints)
  retried      share of steps that needed another try because a check objected
               (none with --no-retry)
  complete     multi-step answers whose commands, together, keep every literal of
               the request (the `note:` lines lxask prints otherwise)
  coverage     share of the request's parts that some step handles (multi-step sets)
  steps_off    mean difference between steps produced and steps expected
  violations   share of answers containing something the request ruled out
  pass         regressions, forbidden(_heldout): accepted program used and nothing ruled out;
               ambiguous: asked back when it should be, else program and form right
  asked        share of questions asked back instead of answered (outside the
               ambiguous set, every one is a false ask)
  form         the command has the expected form (`git config ... http.proxy`)
  flags, exact against the reference command; both undercount, since one reference
               is rarely the only correct answer

None of these says a command does what was asked. scripts/exec_eval.py measures
that, for the tasks where running the command is safe.
"""

from __future__ import annotations

import argparse
import json
import re
import resource
import subprocess
import tempfile
import time
from pathlib import Path
from statistics import mean

import nl2bash
from lxask import config
from lxask.answer import Step, answer, clarification, unhandled
from lxask.constraints import programs_run
from lxask.llm import LLM, Embedder
from lxask.retrieve import Retriever
from lxask.route import specialist
from lxask.validate import Catalog, load_catalog, uses

ROOT = Path(__file__).resolve().parents[1]
SETS = ("everyday", "test_everyday", "tldr_heldout", "nl2bash_test", "multistep", "multistep_heldout",
        "multistep3_heldout", "regressions", "forbidden", "forbidden_heldout", "ambiguous")
METRICS = ("answered", "program", "valid", "clean", "retried", "complete", "coverage", "steps_off", "violations", "pass",
           "asked", "form", "flags", "exact")
_WRAPPERS = {"sudo", "doas", "time", "nohup", "env", "command", "exec"}
RUST_BATCH = 40  # questions per `lxask batch` run with --engine rust


def load_cases(only: list[str] | None) -> list[dict]:
    cases = []
    for name in only or SETS:
        path = ROOT / "evalsets" / f"{name}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                cases.append({"set": name, **json.loads(line)})
    return cases


def programs_in(command: str, catalog: Catalog) -> list[str]:
    """Every program a command line runs, wrappers and what they wrap included."""
    try:
        return [use.program for use in uses(command, catalog)]
    except ValueError:
        return nl2bash.utilities(command)


def flag_f1(predicted: str, reference: str) -> float:
    got, want = nl2bash.flags(predicted), nl2bash.flags(reference)
    if not got and not want:
        return 1.0
    overlap = len(got & want)
    if not overlap:
        return 0.0
    precision, recall = overlap / len(got), overlap / len(want)
    return 2 * precision * recall / (precision + recall)


def part_handled(part: dict, command: str, programs: set[str]) -> bool:
    """Whether one command does one part of a request, as far as its spec can say."""
    if not set(part["cmds"]) & programs:
        return False
    if "pattern" in part and not re.search(part["pattern"], command):
        return False
    return not ("not_pattern" in part and re.search(part["not_pattern"], command))


def score(case: dict, steps: list[Step], catalog: Catalog, asked: bool = False) -> dict:
    commands = [step.command for step in steps if step.command]
    per_command = [set(programs_in(command, catalog)) for command in commands]
    used = set().union(*per_command) if per_command else set()
    result: dict = {
        "asked": asked,
        "answered": len(commands) == len(steps) and bool(commands),
        "valid": mean(not step.issues for step in steps if step.command) if commands else 0.0,
        "clean": mean(not (step.issues or step.misses) for step in steps if step.command) if commands else 0.0,
        "retried": mean(step.tries > 1 for step in steps) if steps else 0.0,
    }
    accepted = case.get("cmds") or case.get("must_cmds")
    if accepted:
        result["program"] = bool(set(accepted) & used)
    if "reference" in case:
        joined = " ; ".join(commands)
        result["flags"] = flag_f1(joined, case["reference"])
        result["exact"] = len(commands) == 1 and commands[0].split() == case["reference"].split()
    if len(steps) > 1:
        result["complete"] = not unhandled(case["q"], steps)
    if "steps" in case:  # multistep: a spec per part of the request
        handled = [any(part_handled(part, c, p) for c, p in zip(commands, per_command)) for part in case["steps"]]
        result["coverage"] = mean(handled)
        result["steps_off"] = abs(len(steps) - len(case["steps"]))
    if "gold_steps" in case:  # multistep_heldout: a reference command per step
        wanted = [next((p for p in programs_in(gold["command"], catalog) if p not in _WRAPPERS), "") for gold in case["gold_steps"]]
        result["coverage"] = mean(program in used for program in wanted)
        result["steps_off"] = abs(len(steps) - len(wanted))
    if "must_not" in case:
        result["violations"] = any(re.search(pattern, command) for pattern in case["must_not"] for command in commands)
    if "must" in case:  # ambiguous, with a context named: the form a right command has
        result["form"] = any(re.search(pattern, " ; ".join(commands)) for pattern in case["must"])
    if "forbid" in case:  # programs ruled out: judged by what runs, not by matching text (`ip route`)
        result["violations"] = any(p.lower() in case["forbid"] for c in commands for p in programs_run(c))
    if case["set"] in ("regressions", "forbidden", "forbidden_heldout"):
        result["pass"] = result.get("program", True) and not result.get("violations", False)
    if case["set"] == "ambiguous":
        result["pass"] = asked if case.get("clarify") else (
            not asked and result.get("program", True) and result.get("form", True) and not result.get("violations", False))
    return result


def load_records(path: Path) -> list[dict]:
    """Answers saved by an interrupted run; a line cut off mid-write is dropped."""
    records = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
    return records


def summarise(records: list[dict]) -> dict:
    summary = {}
    for name in SETS:
        subset = [r for r in records if r["set"] == name]
        if subset:
            summary[name] = {"n": len(subset), **{m: mean(r[m] for r in subset if m in r) for m in METRICS if any(m in r for r in subset)}}
    return summary


def binary_answers(cases: list[dict], args) -> list[list[Step]]:
    """(steps, asked back) as `lxask batch` gives them for each case, in order."""
    with tempfile.TemporaryDirectory() as work:
        questions, answers = Path(work) / "questions.jsonl", Path(work) / "answers.jsonl"
        questions.write_text("".join(json.dumps({"q": case["q"]}) + "\n" for case in cases), encoding="utf-8")
        command = [args.binary, "batch", "--model", str(args.model), "--index", str(args.index)]
        command += ["--no-retry"] * args.no_retry + ["--no-rag"] * args.no_rag
        command += ["--adapter", str(args.adapter)] if args.adapter else ["--no-adapter"]
        if args.threads:
            command += ["--threads", str(args.threads)]
        subprocess.run([*command, str(questions), str(answers)], check=True)
        rows = [json.loads(line) for line in answers.read_text(encoding="utf-8").splitlines()]
    if len(rows) != len(cases) or any("error" in row for row in rows):
        raise SystemExit(f"lxask batch answered {len(rows)} of {len(cases)}: {[r['error'] for r in rows if 'error' in r][:3]}")
    return [([Step(s["task"], s["command"], issues=s["issues"], misses=s["misses"], tries=s["tries"]) for s in row["steps"]],
             "clarify" in row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", type=Path, default=config.model_path())
    parser.add_argument("--index", type=Path, default=config.index_path())
    parser.add_argument("--no-rag", action="store_true", help="answer without retrieved notes")
    parser.add_argument("--adapter", type=Path, help="the restriction adapter, applied to the requests lxask.route sends it")
    parser.add_argument("--only", nargs="+", choices=SETS, help="run just these sets")
    parser.add_argument("--limit", type=int, help="only the first N cases of each set (smoke test)")
    parser.add_argument("--name", help="run name (default: model file name plus -rag or -norag)")
    parser.add_argument("--threads", type=int)
    parser.add_argument("--resume", action="store_true", help="keep the answers an interrupted run already saved")
    parser.add_argument("--no-retry", action="store_true", help="keep the first answer, as `lxask --no-retry` does")
    parser.add_argument("--engine", choices=("python", "rust"), default="python",
                        help="python: this package through llama-cpp-python (a GPU build runs on the GPU); "
                             "rust: the lxask binary, through `lxask batch` (CPU)")
    parser.add_argument("--binary", default="lxask", help="with --engine rust: the lxask binary to run")
    args = parser.parse_args()

    name = args.name or f"{args.model.stem}-{'norag' if args.no_rag else 'rag'}"
    cases = load_cases(args.only)
    if args.limit:
        cases = [c for s in SETS for c in [c for c in cases if c["set"] == s][: args.limit]]

    catalog = load_catalog(args.index)
    if catalog is None:
        raise SystemExit(f"{args.index} has no catalogue: rebuild it with scripts/build_index.py")
    search = lambda task: []  # noqa: E731
    mode = "none"
    if args.engine == "rust":
        mode = "none" if args.no_rag else "semantic" if config.embed_model_path().exists() else "keyword"
    elif not args.no_rag:
        embed = Embedder(config.embed_model_path(), args.threads) if config.embed_model_path().exists() else None
        retriever = Retriever(args.index, embed=embed)
        search, mode = retriever.search, "semantic" if retriever.semantic else "keyword"
    llm = LLM(args.model, n_threads=args.threads, adapter=args.adapter) if args.engine == "python" else None

    out_dir = ROOT / "runs"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{name}.jsonl"
    records = load_records(path) if args.resume else []
    answered = {(record["set"], record["q"]) for record in records}
    cases = [case for case in cases if (case["set"], case["q"]) not in answered]
    if records:
        print(f"resuming: {len(records)} answered, {len(cases)} to go", flush=True)

    started = time.time()
    answered_by_binary: list[tuple[list[Step], bool]] = []
    with open(path, "w", encoding="utf-8") as out:
        for record in records:
            out.write(json.dumps(record) + "\n")
        for i, case in enumerate(cases):
            if args.engine == "rust":
                # in batches, so that an interrupted run keeps what it answered (--resume)
                if i % RUST_BATCH == 0:
                    answered_by_binary = binary_answers(cases[i : i + RUST_BATCH], args)
                steps, asked = answered_by_binary[i % RUST_BATCH]
            else:
                asked = clarification(case["q"]) is not None  # the CLI asks back instead of answering
                llm.specialize(specialist(case["q"], catalog))
                steps = [] if asked else answer(case["q"], llm.ask, search, catalog,
                                                resample=None if args.no_retry else llm.sample)
            record = {
                "set": case["set"],
                "q": case["q"],
                "steps": [{"task": s.task, "command": s.command, "issues": s.issues, "misses": s.misses, "tries": s.tries}
                          for s in steps],
                **score(case, steps, catalog, asked),
            }
            records.append(record)
            out.write(json.dumps(record) + "\n")
            out.flush()
    elapsed = time.time() - started

    summary = {
        "name": name,
        "model": args.model.name,
        "adapter": args.adapter.name if args.adapter else None,
        "engine": args.engine,
        "rag": mode,
        **summarise(records),
        "seconds_per_question": elapsed / len(cases) if cases else None,
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
    }
    (out_dir / f"{name}.summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"{name}  ({mode} retrieval)")
    for key in SETS:
        if key in summary:
            stats = summary[key]
            line = "  ".join(f"{m} {stats[m]:.2f}" if m == "steps_off" else f"{m} {stats[m]:.1%}" for m in METRICS if m in stats)
            print(f"  {key:17} n={stats['n']:3}  {line}")
    if cases:
        print(f"  {summary['seconds_per_question']:.1f} s/question, peak RSS {summary['peak_rss_mb']:.0f} MB")


if __name__ == "__main__":
    main()
