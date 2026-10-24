"""A short fine-tuning set on top of an already trained model: the examples of the
new generated kinds, repeated, and a replay sample of everything else so the model
keeps what it knew.

    python scripts/mix_finetune.py --sources forbid,contexts,forbid_scenarios --out data/round6.jsonl

Reads data/train.jsonl (scripts/build_dataset.py tags each example with its
`source`). The replay sample keeps the kinds in their proportions in train.jsonl.

--teacher MODEL answers the replay prompts with that model (greedy, the model being
fine-tuned): replaying the original references would pull an RL-trained model back
toward the habits RL trained out of it. A teacher answer that the validator or the
constraint checks object to is replaced by the reference. Needs llama_cpp (the GPU
venv); standard library only without it.

--repeat takes one count for every source or per source (`forbid=1,forbid_twins=2`,
others 1). --favor find,umount adds --favor-count more replay examples whose
reference runs one of those programs: the ones a fine-tune drifted away from.

--val-out holds examples of the mix out for train_lora.py's --val: data/val.jsonl has
the original references, so its loss measures the distance back to them, not how
well the mix is learned.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def teach(replay: list[str], model: Path) -> tuple[list[str], Counter]:
    """The replay examples with the teacher's answers where those pass the checks."""
    from lxask import config
    from lxask.constraints import check as constraint_check
    from lxask.llm import LLM
    from lxask.prompt import format_answer, format_tasks, parse_answer, parse_tasks
    from lxask.validate import check, load_catalog

    llm, catalog = LLM(model), load_catalog(config.index_path())
    out, kept = [], Counter()
    for number, line in enumerate(replay, 1):
        record = json.loads(line)
        prompt = record["messages"][:-1]
        reply = llm.ask(prompt) or ""
        answer = None
        if record["kind"] == "split":
            if tasks := parse_tasks(reply):
                answer = format_tasks(tasks)
        elif command := parse_answer(reply):
            question = prompt[-1]["content"].rsplit("Question: ", 1)[-1]
            if not check(command, catalog) and not constraint_check(question, command):
                answer = format_answer(command)
        if answer is not None:
            record["messages"] = prompt + [{"role": "assistant", "content": answer}]
            record["replay"] = "teacher"
        kept[(record["kind"], answer is not None)] += 1
        out.append(json.dumps(record, ensure_ascii=False))
        if number % 500 == 0:
            print(f"  {number}/{len(replay)} answered by the teacher", flush=True)
    return out, kept


def runs_any(line: str, programs: set[str]) -> bool:
    """Whether the reference answer of a record runs one of the programs."""
    from lxask.constraints import programs_run
    from lxask.prompt import parse_answer, parse_tasks

    record = json.loads(line)
    reply = record["messages"][-1]["content"]
    if record["kind"] == "split" and parse_tasks(reply):
        return False
    command = parse_answer(reply)
    return bool(command) and bool(programs & set(programs_run(command)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", required=True, help="comma-separated generate_data.py jobs whose examples are new")
    parser.add_argument("--train", type=Path, default=ROOT / "data" / "train.jsonl")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeat", default="2",
                        help="how many times each new example appears: N, or SOURCE=N,... (others 1)")
    parser.add_argument("--replay", type=float, default=2.0, help="replay examples per new example")
    parser.add_argument("--favor", default="", help="comma-separated programs to replay more of")
    parser.add_argument("--favor-count", type=int, default=600)
    parser.add_argument("--val-out", type=Path, help="hold out --val-count examples of the mix here")
    parser.add_argument("--val-count", type=int, default=400)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--teacher", type=Path, metavar="GGUF", help="answer the replay prompts with this model")
    args = parser.parse_args()
    rng = random.Random(args.seed)

    wanted = set(args.sources.split(","))
    if "=" in args.repeat:
        repeats = {source: int(n) for source, n in (part.split("=") for part in args.repeat.split(","))}
        repeat = lambda source: repeats.get(source, 1)
    else:
        repeat = lambda source, n=int(args.repeat): n
    new, old = [], defaultdict(list)
    for line in args.train.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("source") in wanted:
            new.append(line)
        else:
            old[record["kind"]].append(line)
    total_old = sum(len(lines) for lines in old.values())
    replay = []
    for kind, lines in sorted(old.items()):
        replay += rng.sample(lines, min(len(lines), round(args.replay * len(new) * len(lines) / total_old)))
    if args.favor:
        programs, taken = set(args.favor.split(",")), set(replay)
        pool = [line for lines in old.values() for line in lines if line not in taken and runs_any(line, programs)]
        replay += rng.sample(pool, min(len(pool), args.favor_count))
        print(f"  favored {args.favor}: {min(len(pool), args.favor_count)} of {len(pool)} more replayed")
    if args.teacher:
        replay, kept = teach(replay, args.teacher)
        print("  teacher answers kept: " + ", ".join(
            f"{kind} {kept[(kind, True)]}/{kept[(kind, True)] + kept[(kind, False)]}" for kind in sorted({k for k, _ in kept})))
    if args.val_out:
        held = set(rng.sample(new + replay, args.val_count))
        new, replay = [l for l in new if l not in held], [l for l in replay if l not in held]
        args.val_out.write_text("".join(line + "\n" for line in sorted(held)), encoding="utf-8", newline="\n")
        print(f"  {len(held)} held out -> {args.val_out}")
    mixed = [line for line in new for _ in range(repeat(json.loads(line)["source"]))] + replay
    rng.shuffle(mixed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(line + "\n" for line in mixed), encoding="utf-8", newline="\n")
    print(f"{len(new)} new examples x{args.repeat}, {len(replay)} replayed: {len(mixed)} -> {args.out}")
    print("  new by source and kind: " + ", ".join(
        f"{s}/{k} {n}" for (s, k), n in Counter((json.loads(l)["source"], json.loads(l)["kind"]) for l in new).most_common()))
    print("  replay by kind: " + ", ".join(f"{k} {n}" for k, n in Counter(json.loads(l)["kind"] for l in replay).most_common()))


if __name__ == "__main__":
    main()
