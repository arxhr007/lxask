"""Score answers_<variant>.jsonl with the InterCode-ALFA scorer (embed judge, threshold 0.75).

Needs `pip install icalfa==0.3.6` (own venv), Docker, and an Ollama server with mxbai-embed-large on :11434.
Run from $LXASK_BENCH_DIR with that venv's python. Resumable: scores_<variant>.json is rewritten after
every item, and items already scored are skipped. The gold commands are also scored as a sanity check
(variant "gold" = column bash), which should come out near 1.0.
"""
import csv
import json
import os
import sys
import time
from pathlib import Path

from icalfa import submit_command

OUT = Path(os.environ.get("LXASK_BENCH_DIR", Path.home() / "bench-alfa"))
rows = list(csv.DictReader(open(OUT / "test.csv", encoding="utf-8")))


def commands(variant):
    if variant == "gold":
        return [r["bash"] for r in rows]
    recs = [json.loads(line) for line in open(OUT / f"answers_{variant}.jsonl", encoding="utf-8")]
    return [r["command"] for r in sorted(recs, key=lambda r: r["i"])]


for variant in sys.argv[1:]:
    path = OUT / f"scores_{variant}.json"
    scores = json.load(open(path)) if path.exists() else {}
    cmds = commands(variant)
    started = time.time()
    for i, cmd in enumerate(cmds):
        if str(i) in scores:
            continue
        scores[str(i)] = submit_command(i, cmd, eval_mode="embed", eval_param=0.75) if cmd.strip() else 0
        json.dump(scores, open(path, "w"))
        if len(scores) % 25 == 0:
            print(f"{variant}: {len(scores)}/{len(cmds)} pass so far {sum(scores.values())} "
                  f"({time.time() - started:.0f} s)", flush=True)
    print(f"{variant}: DONE {sum(scores.values())}/{len(cmds)} = {sum(scores.values()) / len(cmds):.3f}", flush=True)
