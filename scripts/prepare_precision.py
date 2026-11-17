from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

from execution_tasks import is_proved, task_record
from lxask import config
from lxask.constraints import forbidden
from lxask.index import program
from lxask.prompt import build_messages, format_answer

ROOT = Path(__file__).resolve().parents[1]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def question(record: dict) -> str:
    text = record.get("q") or record.get("question")
    if not text:
        text = next((message["content"] for message in reversed(record.get("messages", []))
                     if message["role"] == "user"), "")
        text = text.rsplit("Question: ", 1)[-1]
    return " ".join(text.split()).casefold()


def sample_replay(records: list[dict], count: int, rng: random.Random) -> list[dict]:
    groups = defaultdict(list)
    unique = {json.dumps(record, sort_keys=True): record for record in records}
    for serialized, record in sorted(unique.items()):
        groups[record.get("kind", "answer")].append(record)
    count = min(count, len(unique))
    if not count:
        return []
    shares = {kind: count * len(rows) / len(unique) for kind, rows in groups.items()}
    quotas = {kind: int(share) for kind, share in shares.items()}
    for kind in sorted(shares, key=lambda kind: (-(shares[kind] - quotas[kind]), kind))[:count - sum(quotas.values())]:
        quotas[kind] += 1
    return [dict(record, replay=record.get("replay", "original"))
            for kind in sorted(groups) for record in rng.sample(groups[kind], quotas[kind])]


def prepare(tasks: list[dict], replay: list[dict], *, replay_val: list[dict] | None = None,
            replay_ratio: float = 2.0, seed: int = 0, search: Callable | None = None) -> dict[str, list[dict]]:
    if not math.isfinite(replay_ratio) or replay_ratio < 0:
        raise ValueError("replay ratio must be finite and nonnegative")
    partitions = {split: [] for split in ("train", "dev", "test")}
    families, seen_ids, seen_questions = {}, set(), set()
    for task in tasks:
        if not is_proved(task):
            raise ValueError(f"unproved or modified task: {task.get('id', '?')}; run prove_exec_tasks.py first")
        family, split = task.get("family"), task.get("split")
        if not family or split not in partitions:
            raise ValueError("every task needs a family and a train/dev/test split")
        if family in families and families[family] != split:
            raise ValueError(f"family crosses splits: {family}")
        families[family] = split
        if not task.get("id") or task["id"] in seen_ids:
            raise ValueError("task IDs must be present and unique")
        normalized = question(task)
        if not normalized or normalized in seen_questions:
            raise ValueError("task questions must be present and unique")
        seen_ids.add(task["id"])
        seen_questions.add(normalized)
        partitions[split].append(task)
    if any(not partition for partition in partitions.values()):
        raise ValueError("train, dev and test must all contain proved tasks")

    def eligible(records: list[dict], excluded_questions: set[str], excluded_families: set[str],
                 splits: tuple[str, ...] = ("train",)) -> list[dict]:
        return [record for record in records if record.get("messages") and question(record)
                and question(record) not in excluded_questions and record.get("family") not in excluded_families
                and record.get("split", "train") in splits]

    rng = random.Random(seed)
    replay_pool = eligible(replay, seen_questions, set(families))
    wanted = round(len(partitions["train"]) * replay_ratio)
    if wanted and not replay_pool:
        raise ValueError("no eligible replay examples; supply training data or explicitly use --replay-ratio 0")
    old = sample_replay(replay_pool, wanted, rng)
    excluded = seen_questions | {question(record) for record in old}
    old_families = set(families) | {record["family"] for record in old if record.get("family")}
    old_val = sample_replay(eligible(replay_val or [], excluded, old_families, ("train", "dev", "val")),
                            round(len(partitions["dev"]) * replay_ratio), rng)
    bundle = {f"{split}.tasks.jsonl": rows for split, rows in partitions.items()}
    for split, output in (("train", "train.jsonl"), ("dev", "val.jsonl")):
        records, rl = [], []
        for task in partitions[split]:
            ruled_out = set(task.get("forbid", [])) | set(forbidden(task["q"]))
            docs = [doc for doc in (search(task["q"]) if search else [])
                    if program(doc.cmd).lower() not in ruled_out]
            messages = build_messages(task["q"], docs)
            records.append({
                "id": task["id"], "kind": "answer", "source": task["source"], "family": task["family"],
                "split": split, "messages": messages + [{"role": "assistant", "content": format_answer(task["reference"])}],
            })
            rl.append({"id": task["id"], "family": task["family"], "messages": messages, "task": task_record(task)})
        records.extend(old if split == "train" else old_val)
        rng.shuffle(records)
        bundle[output] = records
        bundle[f"rl_{split}.jsonl"] = rl
    return bundle


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bundle(out: Path, bundle: dict[str, list[dict]], metadata: dict) -> None:
    out.mkdir(parents=True, exist_ok=False)
    files = {}
    for name, records in bundle.items():
        path = out / name
        with path.open("x", encoding="utf-8", newline="\n") as file:
            for record in records:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        files[name] = {"rows": len(records), "sha256": file_digest(path)}
    manifest = {**metadata, "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare proved precision tasks and replay for LoRA; never executes commands or trains.")
    parser.add_argument("--tasks", type=Path, required=True, help="JSONL output from prove_exec_tasks.py --tasks")
    parser.add_argument("--replay", type=Path, required=True, help="existing training JSONL, not an evaluation set")
    parser.add_argument("--replay-val", type=Path, help="separate existing validation examples for replay checks")
    parser.add_argument("--out", type=Path, required=True, help="new output directory; existing directories are never overwritten")
    parser.add_argument("--replay-ratio", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--index", type=Path, default=config.index_path())
    parser.add_argument("--no-rag", action="store_true", help="prepare explicitly without documentation notes")
    parser.add_argument("--teacher", type=Path, help="optionally rewrite replay with the existing model, using mix_finetune.py")
    args = parser.parse_args()
    if args.out.exists():
        parser.error("--out already exists; choose a new experiment directory")
    tasks, replay = load_jsonl(args.tasks), load_jsonl(args.replay)
    validation = load_jsonl(args.replay_val) if args.replay_val else []
    search, mode = None, "none"
    if not args.no_rag:
        from lxask.llm import Embedder
        from lxask.retrieve import Retriever

        embed = Embedder(config.embed_model_path()) if config.embed_model_path().exists() else None
        retriever = Retriever(args.index, embed=embed)
        search, mode = retriever.search, "semantic" if retriever.semantic else "keyword"
    try:
        bundle = prepare(tasks, replay, replay_val=validation, replay_ratio=args.replay_ratio, seed=args.seed, search=search)
    except ValueError as error:
        parser.error(str(error))
    if args.teacher:
        from mix_finetune import teach

        for name in ("train.jsonl", "val.jsonl"):
            positions = [i for i, record in enumerate(bundle[name]) if record.get("replay") == "original"]
            if positions:
                taught, _ = teach([json.dumps(bundle[name][i]) for i in positions], args.teacher)
                for i, record in zip(positions, taught):
                    bundle[name][i] = json.loads(record)
    inputs = {str(path): file_digest(path) for path in (args.tasks, args.replay, args.replay_val) if path}
    metadata = {
        "version": 1, "seed": args.seed, "replay_ratio": args.replay_ratio, "rag": mode, "inputs": inputs,
        "index_sha256": file_digest(args.index) if search else None,
        "teacher": str(args.teacher) if args.teacher else None,
        "families": dict(Counter(task["split"] for task in {task["family"]: task for task in tasks}.values())),
    }
    write_bundle(args.out, bundle, metadata)
    for name, records in bundle.items():
        print(f"{name}: {len(records)} -> {args.out / name}")
    print("The installed model is unchanged. Select candidate checkpoints by execution on dev.tasks.jsonl; reserve test.tasks.jsonl.")


if __name__ == "__main__":
    main()
