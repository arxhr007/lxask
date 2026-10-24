"""Measure whether retrieval surfaces the right command.

hit@1   the top-ranked command is an accepted one
recall  an accepted command is among the notes that would reach the prompt

Two sets: evalsets/everyday.jsonl (hand-written, independent of the index) and the
held-out NL2Bash commands (same distribution the usage prior was built from, so it
flatters the prior; read it as an upper bound).
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import nl2bash
from lxask import config
from lxask.index import program
from lxask.retrieve import Retriever

ROOT = Path(__file__).resolve().parents[1]


def load_everyday() -> list[tuple[str, set[str]]]:
    lines = (ROOT / "evalsets" / "everyday.jsonl").read_text(encoding="utf-8").splitlines()
    return [(item["q"], set(item["cmds"])) for item in map(json.loads, lines)]


def load_nl2bash(sample: int, seed: int = 0) -> list[tuple[str, set[str]]]:
    held_out = [(q, {nl2bash.utility(c)}) for q, c in nl2bash.load_pairs() if nl2bash.is_eval(c)]
    random.Random(seed).shuffle(held_out)
    return held_out[:sample]


def evaluate(retriever: Retriever, cases: list[tuple[str, set[str]]], verbose: bool) -> tuple[float, float]:
    hits = recalled = 0
    for question, accepted in cases:
        found = [program(doc.cmd) for doc in retriever.search(question)]
        top = found[0] if found else None
        hits += top in accepted
        ok = bool(accepted & set(found))
        recalled += ok
        if verbose and not ok:
            print(f"  MISS {question!r} want {sorted(accepted)} got {list(dict.fromkeys(found))}")
    return hits / len(cases), recalled / len(cases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=config.index_path())
    parser.add_argument("--nl2bash-sample", type=int, default=500)
    parser.add_argument("--keyword", action="store_true", help="BM25 only, as when no embedding model is installed")
    parser.add_argument("--no-synonyms", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true", help="list the misses")
    args = parser.parse_args()

    embed = None
    if not args.keyword and config.embed_model_path().exists():
        from lxask.llm import Embedder

        embed = Embedder(config.embed_model_path())
    retriever = Retriever(args.index, embed=embed, synonyms=not args.no_synonyms)
    print("semantic search" if retriever.semantic else "keyword search")
    for name, cases in (("everyday", load_everyday()), ("nl2bash", load_nl2bash(args.nl2bash_sample))):
        hit, recall = evaluate(retriever, cases, args.verbose and name == "everyday")
        print(f"{name:9} n={len(cases):4}  hit@1 {hit:.1%}  recall {recall:.1%}")


if __name__ == "__main__":
    main()
