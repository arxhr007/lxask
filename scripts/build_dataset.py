"""Build the fine-tuning set: data/train.jsonl, data/val.jsonl, evalsets/multistep_heldout.jsonl.

Two kinds of example, in the exact prompt formats the CLI uses (lxask.prompt):

  split    a multi-part request -> its tasks, one per line
  answer   one task, with the notes the real retriever returns for it -> one command

Sources
  scenarios   seed/generated_scenarios.jsonl: the request gives a split example, each
              step an answer example
  tasks       seed/generated_tasks.jsonl
  nl2bash     the training split of NL2Bash, minus what the validator rejects
  uncommon    one tldr example per uncommon tool, with the tool named in the question
  paraphrase  seed/paraphrases.jsonl: round-1 rephrasings of tldr examples

Every command has passed lxask.validate (the generated ones in filter_generated.py,
NL2Bash here), and tldr's are the documentation itself.

Kept out of training: the scenarios set aside as evalsets/multistep_heldout.jsonl,
the held-out NL2Bash and tldr examples, and anything too close to a test question
(same command as a reference, or a question within SIMILARITY of one). The generator
was shown whole tldr pages, held-out examples included, so that last filter matters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import nl2bash
import sources
from lxask import config
from lxask.constraints import forbidden
from lxask.index import program, strip_placeholders
from lxask.llm import Embedder
from lxask.prompt import build_messages, build_split_messages, format_answer, format_tasks
from lxask.retrieve import Retriever
from lxask.validate import check
from tldr_data import as_task, is_held_out, named_question

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "seed"
EVALSETS = ROOT / "evalsets"
# written by language models (generate_data.py); published since 2026-10-21
GENERATED = ("generated_scenarios.jsonl", "generated_tasks.jsonl", "generated_fixes.jsonl")

DROP_GOLD = 0.15  # share of examples whose matching note is removed, as if retrieval missed
NO_NOTES = 0.08  # share of examples given no notes at all
UNCOMMON_CAP = 2000
TASKS_PER_PROGRAM = 300
NL2BASH_CAP = 4000
NL2BASH_PER_COMMAND = 600  # find dominates NL2Bash otherwise
NL2BASH_MAX_CHARS = 200
HELD_OUT_SCENARIOS = 20  # one scenario in this many is set aside for evaluation
VAL_SIZE = 500
SIMILARITY = 0.92  # cosine above which a training question counts as a copy of a test question

NL2BASH_FORMS = ("how do I {task}?", "how to {task}", "command to {task}", "{task} in linux", "how can I {task}?")
TEST_SETS = ("everyday", "test_everyday", "tldr_heldout", "nl2bash_test", "multistep", "multistep_heldout",
             "multistep3_heldout", "regressions", "exec_tasks", "exec_heldout", "forbidden", "forbidden_heldout",
             "ambiguous")
# Round 6's generated kinds: every one is trained on, past the per-program cap.
UNCAPPED_SOURCES = frozenset({"forbid", "contexts", "forbid_twins"})


@dataclass
class Item:
    kind: str
    question: str  # for split: the whole request
    target: str  # the command, or for split the formatted task list
    gold: str | None = None  # the note body that answers it, when the index has one
    rejected: str | None = None  # fix: the wrong command the target corrects
    source: str | None = None  # the generate_data.py job it came from


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as out:
        for record in records:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")


def set_aside(request: str) -> bool:
    """A stable one in HELD_OUT_SCENARIOS of the generated scenarios never reaches training."""
    return hashlib.sha1(request.encode("utf-8")).digest()[0] % HELD_OUT_SCENARIOS == 0


def scenario_items(scenarios: list[dict]) -> tuple[list[Item], list[dict]]:
    """Training items, and the set-aside scenarios (each with its `source` job)."""
    items: list[Item] = []
    held_out: list[dict] = []
    for scenario in scenarios:
        request, steps = scenario["request"], scenario["steps"]
        if set_aside(request):
            held_out.append({"q": request, "area": scenario["area"], "gold_steps": steps,
                             "source": scenario.get("source", "scenarios")})
            continue
        source = scenario.get("source", "scenarios")
        items.append(Item("split", request, format_tasks(step["task"] for step in steps), source=source))
        items += [Item("step", step["task"], step["command"], source=source) for step in steps]
    return items, held_out


def task_items(tasks: list[dict], rng: random.Random) -> list[Item]:
    """Generated single tasks, at most TASKS_PER_PROGRAM for any one program: tldr has a
    page per git subcommand, which would otherwise make a fifth of these git."""
    tasks = list(tasks)
    rng.shuffle(tasks)
    per_program: Counter[str] = Counter()
    items = []
    for task in tasks:
        name = program(task["cmd"])
        source = task.get("source", "tasks")
        if source in UNCAPPED_SOURCES:
            items.append(Item("task", task["question"], task["command"], source=source))
        elif per_program[name] < TASKS_PER_PROGRAM:
            per_program[name] += 1
            items.append(Item("task", task["question"], task["command"], source=source))
    return items


def fix_items(fixes: list[dict]) -> list[Item]:
    """Corrected answers to tasks the reviewer rejected: trained on like any task, and
    with the wrong command kept for preference training (data/dpo.jsonl)."""
    return [Item("fix", fix["question"], fix["command"], rejected=fix["wrong"]) for fix in fixes]


def paraphrase_items(paraphrases: list[dict], pages: dict) -> list[Item]:
    commands = {(name, description): command for name, page in pages.items() for description, command in page.examples}
    items = []
    for record in paraphrases:
        raw = commands.get((record["cmd"], record["description"]))
        if raw is None or is_held_out(record["cmd"], record["description"]):
            continue
        command = strip_placeholders(raw)
        if record.get("generic"):
            items.append(Item("paraphrase", record["generic"], command, command))
        if record.get("specific"):
            items.append(Item("paraphrase", record["specific"], record["filled"], command))
    return items


def uncommon_items(pages: dict, rng: random.Random) -> list[Item]:
    core = sources.core_programs()
    items = []
    for page in pages.values():
        if program(page.name) in core:
            continue
        description, raw = rng.choice(page.examples)
        command = strip_placeholders(raw)
        items.append(Item("uncommon", named_question(description, program(page.name), rng), command, command))
    rng.shuffle(items)
    return items[:UNCOMMON_CAP]


def nl2bash_items(rng: random.Random, catalog) -> tuple[list[Item], Counter]:
    pairs = [(q, c) for q, c in nl2bash.load_pairs() if not nl2bash.is_eval(c)]
    rng.shuffle(pairs)
    per_command: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    items = []
    for question, command in pairs:
        name = nl2bash.utility(command)
        if not name or len(command) > NL2BASH_MAX_CHARS or len(question) > 300 or "\n" in command:
            dropped["too long or empty"] += 1
        elif issues := check(command, catalog):
            dropped[issues[0].kind] += 1
        elif per_command[name] >= NL2BASH_PER_COMMAND:
            dropped["per-command cap"] += 1
        else:
            per_command[name] += 1
            asked = question if rng.random() < 0.6 else rng.choice(NL2BASH_FORMS).format(task=as_task(question))
            items.append(Item("nl2bash", asked, command))
        if len(items) == NL2BASH_CAP:
            break
    return items, dropped


def normal(command: str) -> str:
    return " ".join(command.split())


def remove_leaks(items: list[Item], embed: Embedder) -> tuple[list[Item], Counter]:
    """Drop training items that give away a test case: the same command as a reference,
    or a question that is all but a test question."""
    tests = [case for name in TEST_SETS for case in load_jsonl(EVALSETS / f"{name}.jsonl")]
    # Reference commands that are the answer to one specific test question. The steps of
    # the held-out scenarios are not treated this way: they are everyday commands
    # (`df -h`, `kill -TERM PID`) that training must be free to contain, and that set
    # is scored on which programs handle each part, not on matching a command.
    references = {normal(case["reference"]) for case in tests if "reference" in case}
    questions = sorted({case["q"] for case in tests})
    test_vectors = embed_all(embed, questions)

    removed: Counter[str] = Counter()
    kept = []
    vectors = embed_all(embed, [item.question for item in items])
    closest = (vectors @ test_vectors.T).max(axis=1) if len(questions) else np.zeros(len(items))
    for item, similarity in zip(items, closest):
        if item.kind != "split" and normal(item.target) in references:
            removed[f"{item.kind}: same command as a test reference"] += 1
        elif similarity >= SIMILARITY:
            removed[f"{item.kind}: question matches a test question"] += 1
        else:
            kept.append(item)
    return kept, removed


def embed_all(embed: Embedder, texts: list[str], batch: int = 64) -> np.ndarray:
    if not texts:
        return np.zeros((0, 1), dtype=np.float32)
    return np.concatenate([embed(texts[i : i + batch]) for i in range(0, len(texts), batch)])


def to_record(item: Item, retriever: Retriever, rng: random.Random, stats: Counter) -> dict:
    tag = {"source": item.source} if item.source else {}
    if item.kind == "split":
        messages = build_split_messages(item.question)
        messages.append({"role": "assistant", "content": item.target})
        return {"kind": item.kind, **tag, "messages": messages}

    docs = retriever.search(item.question)
    # as the CLI does: notes on a program the request rules out are left out
    if ruled_out := forbidden(item.question):
        docs = [doc for doc in docs if program(doc.cmd).lower() not in ruled_out]
    if item.gold is not None:
        found = any(doc.body == item.gold for doc in docs)
        stats[f"{item.kind}: gold retrieved"] += found
        if found and rng.random() < DROP_GOLD:
            docs = [doc for doc in docs if doc.body != item.gold]
    if rng.random() < NO_NOTES:
        docs = []
    messages = build_messages(item.question, docs)
    messages.append({"role": "assistant", "content": format_answer(item.target)})
    return {"kind": item.kind, **tag, "messages": messages}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="build only this many examples (for a trial run)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--set-aside", action="store_true",
                        help="rewrite evalsets/multistep_heldout.jsonl from the seed scenarios and stop; "
                             "only for scenarios added before anything was trained on them")
    args = parser.parse_args()
    rng = random.Random(args.seed)

    if args.set_aside:
        _, held_out = scenario_items(load_jsonl(SEED / "generated_scenarios.jsonl"))
        held_out = [case for case in held_out if case["source"] != "scenarios3"]
        write_jsonl(EVALSETS / "multistep_heldout.jsonl", held_out)
        lengths = Counter(len(case["gold_steps"]) for case in held_out)
        print(f"{len(held_out)} scenarios set aside; steps: " + ", ".join(f"{k}: {v}" for k, v in sorted(lengths.items())))
        return

    if not config.embed_model_path().exists():
        sys.exit("the embedding model is missing: the notes must come from the same retriever the CLI uses")
    embed = Embedder(config.embed_model_path())
    retriever = Retriever(config.index_path(), embed=embed)
    if not retriever.semantic:
        sys.exit("the index has no embeddings: rebuild it with scripts/build_index.py")

    pages = sources.load_tldr_pages()
    catalog = sources.load_catalog()
    if missing := [name for name in GENERATED if not (SEED / name).exists()]:
        print(f"  not in seed/: {', '.join(missing)}; those sources add nothing (the published repository "
              "does not carry them: scripts/generate_data.py and filter_generated.py write them, with your "
              "own API key)")
    from_scenarios, held_out = scenario_items(load_jsonl(SEED / "generated_scenarios.jsonl"))
    from_nl2bash, nl2bash_dropped = nl2bash_items(rng, catalog)
    items = (
        from_scenarios
        + task_items(load_jsonl(SEED / "generated_tasks.jsonl"), rng)
        + paraphrase_items(load_jsonl(SEED / "paraphrases.jsonl"), pages)
        + uncommon_items(pages, rng)
        + from_nl2bash
        + fix_items(load_jsonl(SEED / "generated_fixes.jsonl"))
    )
    print(f"{len(items)} candidate examples; {len(held_out)} scenarios set aside for evaluation")
    print("  NL2Bash left out: " + ", ".join(f"{reason} {count}" for reason, count in nl2bash_dropped.most_common()))

    # The set-aside scenarios are a test set from here on: written before the leak
    # filter runs so that it guards them too, and only once so that they stay fixed.
    # Round-3 scenarios (tasks that keep every value) get a set of their own, so that
    # the round-2 one stays comparable across models.
    for path, part in ((EVALSETS / "multistep_heldout.jsonl", [c for c in held_out if c["source"] != "scenarios3"]),
                       (EVALSETS / "multistep3_heldout.jsonl", [c for c in held_out if c["source"] == "scenarios3"])):
        if part and not path.exists() and not args.limit:  # never from a trial run
            write_jsonl(path, part)
            print(f"  wrote {path.name}: {len(part)} scenarios")

    items, removed = remove_leaks(items, embed)
    for reason, count in sorted(removed.items()):
        print(f"  removed, {reason}: {count}")

    rng.shuffle(items)
    if args.limit:
        items = items[: args.limit]

    stats: Counter = Counter()
    records = []
    for number, item in enumerate(items, 1):
        records.append(to_record(item, retriever, rng, stats))
        if number % 2000 == 0:
            print(f"  {number}/{len(items)}", flush=True)

    val, train = records[:VAL_SIZE], records[VAL_SIZE:]
    write_jsonl(ROOT / "data" / "train.jsonl", train)
    write_jsonl(ROOT / "data" / "val.jsonl", val)
    # Preference pairs: the same prompt, notes included, with the corrected answer
    # preferred to the one the reviewer rejected.
    pairs = [{"messages": record["messages"][:-1], "chosen": record["messages"][-1]["content"],
              "rejected": format_answer(item.rejected)}
             for item, record in zip(items, records) if item.rejected]
    write_jsonl(ROOT / "data" / "dpo.jsonl", pairs)
    print(f"{len(pairs)} preference pairs -> data/dpo.jsonl")

    kinds = Counter(record["kind"] for record in records)
    print(f"{len(train)} train, {len(val)} val")
    for kind, count in sorted(kinds.items()):
        line = f"  {kind}: {count}"
        if (hits := stats.get(f"{kind}: gold retrieved")) is not None:
            line += f"  (matching note retrieved for {hits / count:.0%})"
        print(line)
    chars = sorted(sum(len(m["content"]) for m in record["messages"]) for record in records)
    print(f"  prompt+answer chars: median {chars[len(chars) // 2]}, 99th percentile {chars[int(len(chars) * 0.99)]}")
    multi = Counter(len(re.findall(r"^- ", r["messages"][-1]["content"], re.M)) for r in records if r["kind"] == "split")
    print("  tasks per split example: " + ", ".join(f"{k}: {v}" for k, v in sorted(multi.items())))


if __name__ == "__main__":
    main()
