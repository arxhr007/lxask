"""Split the proved execution tasks into a frozen test set and the RL training tasks.

    python scripts/prepare_rl.py data/generated/exec.proved.jsonl

- Near-duplicates (question cosine >= SIMILARITY) are dropped, keeping the first.
- Anything close to a test question of any set, or with a test reference as its own
  reference, is dropped (as build_dataset.py does for supervised data).
- HELD_OUT tasks, chosen by a hash of the question, become evalsets/exec_heldout.jsonl.
  An existing file is never overwritten: it is frozen once written.
- The rest go to seed/generated_exec.jsonl and, with the prompt the CLI would build
  (the same retriever, the same notes), to data/rl_tasks.jsonl for train_grpo.py;
  the held-out tasks with their prompts to data/rl_heldout.jsonl.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from build_dataset import EVALSETS, SEED, SIMILARITY, TEST_SETS, embed_all, load_jsonl, normal, write_jsonl
from lxask import config
from lxask.llm import Embedder
from lxask.prompt import build_messages
from lxask.retrieve import Retriever

ROOT = Path(__file__).resolve().parents[1]
HELD_OUT = 200
TASK_FIELDS = ("id", "q", "setup", "expect", "check", "reference")


def order_key(task: dict) -> bytes:
    return hashlib.sha1(task["q"].encode("utf-8")).digest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("proved", type=Path)
    args = parser.parse_args()

    tasks = load_jsonl(args.proved)
    embed = Embedder(config.embed_model_path())
    removed: Counter[str] = Counter()

    vectors = embed_all(embed, [task["q"] for task in tasks])
    kept_index: list[int] = []
    for i, vector in enumerate(vectors):
        if kept_index and float((vectors[kept_index] @ vector).max()) >= SIMILARITY:
            removed["near-duplicate of another generated task"] += 1
        else:
            kept_index.append(i)

    tests = [case for name in TEST_SETS if name != "exec_heldout" for case in load_jsonl(EVALSETS / f"{name}.jsonl")]
    references = {normal(case["reference"]) for case in tests if "reference" in case}
    test_vectors = embed_all(embed, sorted({case["q"] for case in tests}))
    closest = (vectors[kept_index] @ test_vectors.T).max(axis=1)
    candidates = []
    for i, similarity in zip(kept_index, closest):
        if normal(tasks[i]["reference"]) in references:
            removed["same reference as a test case"] += 1
        elif similarity >= SIMILARITY:
            removed["question matches a test question"] += 1
        else:
            candidates.append(tasks[i])

    held_path = EVALSETS / "exec_heldout.jsonl"
    if held_path.exists():
        held_ids = {task["id"] for task in load_jsonl(held_path)}
        print(f"{held_path.name} exists ({len(held_ids)} tasks): kept as it is")
        held_questions = [task["q"] for task in load_jsonl(held_path)]
        held_vectors = embed_all(embed, held_questions)
        train = []
        for task, vector in zip(candidates, embed_all(embed, [task["q"] for task in candidates])):
            if task["id"] in held_ids or float((held_vectors @ vector).max()) >= SIMILARITY:
                removed["in or close to exec_heldout"] += 1
            else:
                train.append(task)
    else:
        ordered = sorted(candidates, key=order_key)
        held, train = ordered[:HELD_OUT], ordered[HELD_OUT:]
        write_jsonl(held_path, [{key: task[key] for key in TASK_FIELDS if key in task} for task in held])
        print(f"{held_path.name}: {len(held)} tasks written (frozen from now on)")

    write_jsonl(SEED / "generated_exec.jsonl", train)
    retriever = Retriever(config.index_path(), embed=embed)

    def records(tasks: list[dict]) -> list[dict]:
        return [{"id": task["id"], "task": {key: task[key] for key in TASK_FIELDS if key in task},
                 "messages": build_messages(task["q"], retriever.search(task["q"]))} for task in tasks]

    write_jsonl(ROOT / "data" / "rl_tasks.jsonl", records(train))
    # The held-out tasks with their prompts, for train_grpo.py's checks along the way.
    write_jsonl(ROOT / "data" / "rl_heldout.jsonl", records(load_jsonl(held_path)))

    print(f"{len(tasks)} proved tasks; {len(train)} for training -> seed/generated_exec.jsonl, data/rl_tasks.jsonl")
    for why, count in removed.most_common():
        print(f"  removed, {why}: {count}")
    print("  training tasks by area:", dict(Counter(task.get("area", "?")[:40] for task in train).most_common()))


if __name__ == "__main__":
    main()
