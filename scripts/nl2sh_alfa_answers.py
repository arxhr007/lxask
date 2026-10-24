"""Answers for the 300 NL2SH-ALFA test requests, from lxask and from the untuned base model.

Variants (each its own answers_<name>.jsonl in ~/bench-alfa):
  lxask       the shipped pipeline: retrieval, validator, up to 2 retries, adapter routing
  no_retry    the same, keeping the first answer
  no_rag      the same, without retrieved notes
  base_fewshot  the same model with three worked examples and a no-backticks instruction
  base        Qwen2.5-Coder-1.5B-Instruct as downloaded (no fine-tune, no retrieval, no checks),
              asked for one Bash command with a plain instruction
Also writes leak.json: which test items appear in lxask's training sources (exact match of the
request or of the command after whitespace normalisation).

Needs test.csv from the NL2SH-ALFA dataset (config "test") in $LXASK_BENCH_DIR (default ~/bench-alfa).
Run: python scripts/nl2sh_alfa_answers.py [variant ...]   (LXASK_GPU_LAYERS=-1 uses a GPU)
The untuned base model is the stock Qwen2.5-Coder-1.5B-Instruct Q4_K_M GGUF, saved as
$LXASK_HOME/models/qwen2.5-coder-1.5b.gguf (default ~/.local/share/lxask/models/).
"""

import csv
import json
import os
import re
import sys
import time
from pathlib import Path

LXASK = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("LXASK_BENCH_DIR", Path.home() / "bench-alfa"))
rows = list(csv.DictReader(open(OUT / "test.csv", encoding="utf-8")))
norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())


def leak():
    """Which test items are in the training sources: NL2Bash, the generated tasks, tldr examples."""
    questions, commands = set(), set()
    nl, cm = LXASK / "data/raw/nl2bash/all.nl", LXASK / "data/raw/nl2bash/all.cm"
    if nl.exists():
        questions |= {norm(x) for x in nl.read_text(encoding="utf-8").splitlines()}
        commands |= {norm(x) for x in cm.read_text(encoding="utf-8").splitlines()}
    for name in ("generated_tasks.jsonl", "generated_scenarios.jsonl", "paraphrases.jsonl"):
        path = LXASK / "seed" / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            for key in ("question", "request", "generic", "specific"):
                if rec.get(key):
                    questions.add(norm(rec[key]))
            if rec.get("command"):
                commands.add(norm(rec["command"]))
            for step in rec.get("steps", []) or []:
                questions.add(norm(step.get("task", "")))
                commands.add(norm(step.get("command", "")))
    tldr = LXASK / "data/raw/tldr"
    for md in tldr.glob("*/*.md") if tldr.exists() else []:
        for m in re.finditer(r"^`(.+)`$", md.read_text(encoding="utf-8"), re.M):
            commands.add(norm(re.sub(r"\{\{(.*?)\}\}", r"\1", m.group(1))))
    flags = []
    for r in rows:
        flags.append({"nl": norm(r["nl"]) in questions,
                      "cmd": norm(r["bash"]) in commands or norm(r["bash2"]) in commands})
    return flags, {"questions": len(questions), "commands": len(commands)}


def lxask_variant(retry, rag):
    from lxask import config
    from lxask.answer import answer, clarification
    from lxask.llm import LLM, Embedder
    from lxask.retrieve import Retriever
    from lxask.route import specialist
    from lxask.validate import load_catalog

    catalog = load_catalog(config.index_path())
    llm = LLM(config.model_path(), adapter=config.adapter_path())
    search = lambda task: []  # noqa: E731
    if rag:
        search = Retriever(config.index_path(), embed=Embedder(config.embed_model_path())).search
    commands = []
    for r in rows:
        q = r["nl"]
        if clarification(q):
            commands.append("")
            continue
        llm.specialize(specialist(q, catalog))
        steps = answer(q, llm.ask, search, catalog, resample=llm.sample if retry else None)
        commands.append(" && ".join(s.command for s in steps if s.command))
    return commands


SYSTEM = ("You are a Linux shell expert. Reply with exactly one Bash command that does what the user asks, "
          "and nothing else: no explanation, no markdown.")


def clean_reply(text):
    """Strip what a chat model wraps around a command: code fences, single backticks, a leading $."""
    text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text.strip()).strip()
    if len(text) > 1 and text.startswith("`") and text.endswith("`") and "`" not in text[1:-1]:
        text = text[1:-1].strip()
    return re.sub(r"^\$ +", "", text)


SHOTS = [("show the size of the folder /var/log", "du -sh /var/log"),
         ("find files named *.bak under /tmp and delete them", "find /tmp -name '*.bak' -delete"),
         ("count the lines in notes.txt", "wc -l notes.txt")]


def base_variant(fewshot=False):
    """The stock model. fewshot=True adds three worked examples (none from the test set) and asks for
    no backticks, so the baseline is not penalised for its output format."""
    from llama_cpp import Llama

    llm = Llama(model_path=str(Path.home() / ".local/share/lxask/models/qwen2.5-coder-1.5b.gguf"),
                n_ctx=2048, n_gpu_layers=-1, verbose=False)
    system = SYSTEM + (" Do not use backticks and do not start with a $ prompt." if fewshot else "")
    shots = []
    for q, a in SHOTS if fewshot else []:
        shots += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    commands = []
    for r in rows:
        out = llm.create_chat_completion(
            messages=[{"role": "system", "content": system}, *shots, {"role": "user", "content": r["nl"]}],
            max_tokens=160, temperature=0.0)["choices"][0]["message"]["content"]
        commands.append(clean_reply(out))
    return commands


def main():
    wanted = sys.argv[1:] or ["lxask", "no_retry", "no_rag", "base"]
    flags, sizes = leak()
    json.dump({"flags": flags, "sources": sizes, "nl_overlap": sum(f["nl"] for f in flags),
               "cmd_overlap": sum(f["cmd"] for f in flags),
               "either": sum(f["nl"] or f["cmd"] for f in flags)},
              open(OUT / "leak.json", "w"))
    print("overlap with training sources:", sizes, "request:", sum(f["nl"] for f in flags),
          "command:", sum(f["cmd"] for f in flags), "either:", sum(f["nl"] or f["cmd"] for f in flags), flush=True)
    makers = {"lxask": lambda: lxask_variant(True, True), "no_retry": lambda: lxask_variant(False, True),
              "no_rag": lambda: lxask_variant(True, False), "base": base_variant,
              "base_fewshot": lambda: base_variant(fewshot=True)}
    for name in wanted:
        started = time.time()
        commands = makers[name]()
        with open(OUT / f"answers_{name}.jsonl", "w", encoding="utf-8") as f:
            for i, (r, c) in enumerate(zip(rows, commands)):
                f.write(json.dumps({"i": i, "nl": r["nl"], "command": c}) + "\n")
        print(f"{name}: {len(commands)} answers, {sum(1 for c in commands if not c)} empty, "
              f"{time.time() - started:.0f} s", flush=True)


if __name__ == "__main__":
    main()
