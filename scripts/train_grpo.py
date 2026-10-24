"""Reinforcement learning on executed commands: GRPO with a LoRA adapter.

    python scripts/train_grpo.py --base ~/lxask-resume/round3/lora/merged --out runs/grpo

For each training task (data/rl_tasks.jsonl, prompts exactly as the CLI builds them)
the model samples --group replies. Every command runs in the sandbox (exec_server.py
in a Docker container with no network) and is scored by rl_reward.score. Each reply's
advantage is its reward against the others for the same task; the update raises the
log-probability of the better replies and lowers that of the worse, with a per-token
KL penalty (the k3 estimator) towards the starting model, which is the same weights
with the adapter switched off (one model in memory).

Every --eval-every steps the adapter answers --eval-n held-out tasks greedily
(data/rl_heldout.jsonl) and the best adapter so far is kept. Writes <out>/best,
<out>/last (resumable with --resume), <out>/log.jsonl, and with --merge <out>/merged.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import time
from pathlib import Path

import torch
from peft import LoraConfig, get_peft_model, set_peft_model_state_dict
from safetensors.torch import load_file
from transformers import AutoModelForCausalLM, AutoTokenizer

from lxask import config
from lxask.prompt import parse_answer
from lxask.validate import load_catalog
from rl_reward import advantages, score, score_reference
from train_lora import DTYPES, IGNORE, TARGET_MODULES, Collator, merge

ROOT = Path(__file__).resolve().parents[1]


class Sandbox:
    """exec_server.py in a throwaway container; batches of (task, command) in, results out."""

    def __init__(self, image: str):
        self.process = subprocess.Popen(
            # --init reaps the processes the sandbox kills; without it they stay as zombies
            # and count against the process limit of `nobody`.
            ["docker", "run", "--rm", "-i", "--init", "--network", "none", "--memory", "1g", "-e", "LXASK_ALLOW_EXEC=1",
             image, "python", "scripts/exec_server.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    def run(self, jobs: list[dict]) -> list[dict]:
        self.process.stdin.write(json.dumps(jobs) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("the sandbox container stopped")
        return json.loads(line)

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=60)


def load_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def prompt_ids(tokenizer, messages: list[dict]) -> list[int]:
    """Tokenized as train_lora.encode and the runtime do: the prompt on its own."""
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return tokenizer(text, add_special_tokens=False)["input_ids"]


@torch.no_grad()
def sample(model, tokenizer, prompts: list[list[int]], count: int, temperature: float, max_new: int,
           eos: set[int]) -> list[list[int]]:
    """`count` replies per prompt (greedy when temperature is 0), each cut after its end token."""
    model.eval()
    width = max(len(p) for p in prompts)
    pad = tokenizer.pad_token_id
    ids = torch.tensor([[pad] * (width - len(p)) + p for p in prompts], device="cuda")
    mask = torch.tensor([[0] * (width - len(p)) + [1] * len(p) for p in prompts], device="cuda")
    out = model.generate(
        input_ids=ids, attention_mask=mask, max_new_tokens=max_new, num_return_sequences=count,
        do_sample=temperature > 0, temperature=temperature if temperature > 0 else None, top_p=1.0, top_k=0,
        pad_token_id=pad, eos_token_id=sorted(eos), use_cache=True)
    replies = []
    for row in out[:, width:].tolist():
        cut = next((i + 1 for i, token in enumerate(row) if token in eos), len(row))
        replies.append(row[:cut])
    # The generation cache is not needed again; on an 8 GB card, holding it pushes the
    # update into shared system memory, which is many times slower.
    del out, ids, mask
    torch.cuda.empty_cache()
    model.train()
    return replies


def token_logps(lm, batch: dict) -> tuple[torch.Tensor, torch.Tensor]:
    """Log-probability of each reply token, and the row it belongs to."""
    hidden = lm.model(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]).last_hidden_state
    hidden, targets = hidden[:, :-1], batch["labels"][:, 1:]
    keep = targets != IGNORE
    logits = lm.lm_head(hidden[keep]).float()
    token = torch.log_softmax(logits, dim=-1).gather(1, targets[keep].unsqueeze(1)).squeeze(1)
    return token, keep.nonzero()[:, 0]


def per_row_mean(values: torch.Tensor, rows: torch.Tensor, n: int) -> torch.Tensor:
    total = torch.zeros(n, device=values.device).index_add(0, rows, values)
    count = torch.zeros(n, device=values.device).index_add(0, rows, torch.ones_like(values))
    return total / count.clamp(min=1)


def update(model, sequences: list[dict], weights: list[float], beta: float, micro: int, dtype) -> tuple[float, float]:
    """One policy-gradient step over `sequences` (accumulated in micro-batches).
    Returns (mean policy term, mean KL)."""
    lm = model.get_base_model()
    collate = Collator(model.config.pad_token_id if model.config.pad_token_id is not None else 0)
    total_pg, total_kl, n = 0.0, 0.0, len(sequences)
    for start in range(0, n, micro):
        chunk = sequences[start : start + micro]
        batch = {k: v.to("cuda") for k, v in collate(chunk).items()}
        advantage = torch.tensor(weights[start : start + micro], device="cuda")
        with torch.autocast("cuda", dtype=dtype):
            policy, rows = token_logps(lm, batch)
            with torch.no_grad(), model.disable_adapter():
                reference, _ = token_logps(lm, batch)
        log_ratio = reference - policy  # k3: exp(r) - r - 1 >= 0, an unbiased KL estimate
        kl = per_row_mean(torch.exp(log_ratio) - log_ratio - 1, rows, len(chunk))
        mean_logp = per_row_mean(policy, rows, len(chunk))
        loss = (-(advantage * mean_logp) + beta * kl).sum() / n
        loss.backward()
        total_pg += float((advantage * mean_logp).detach().sum())
        total_kl += float(kl.detach().sum())
    return total_pg / n, total_kl / n


def evaluate(model, tokenizer, records: list[dict], sandbox: Sandbox, max_new: int, eos: set[int], batch: int = 16) -> float:
    """Share of held-out tasks whose greedy reply's command passes."""
    jobs = []
    for start in range(0, len(records), batch):
        chunk = records[start : start + batch]
        replies = sample(model, tokenizer, [prompt_ids(tokenizer, r["messages"]) for r in chunk], 1, 0.0, max_new, eos)
        for record, reply in zip(chunk, replies):
            jobs.append({"task": record["task"], "command": parse_answer(tokenizer.decode(reply, skip_special_tokens=True))})
    results = sandbox.run(jobs)
    return sum(r["success"] for r in results) / len(results)


def evaluate_reference(model, tokenizer, records: list[dict], catalog, max_new: int, eos: set[int], batch: int = 16) -> float:
    """Share of held-out ordinary questions whose greedy reply uses the reference's program."""
    right = 0
    for start in range(0, len(records), batch):
        chunk = records[start : start + batch]
        replies = sample(model, tokenizer, [prompt_ids(tokenizer, r["messages"]) for r in chunk], 1, 0.0, max_new, eos)
        for record, reply in zip(chunk, replies):
            _, details = score_reference(record["q"], tokenizer.decode(reply, skip_special_tokens=True), record["reference"], catalog)
            right += details["success"]
    return right / len(records)


def checks(model, tokenizer, heldout, ref_heldout, sandbox, catalog, max_new, eos) -> dict:
    """The held-out checks; "score" picks the best checkpoint: execution success, averaged
    with the right-program rate on ordinary questions when those are trained too."""
    result = {"heldout": evaluate(model, tokenizer, heldout, sandbox, max_new, eos)}
    if ref_heldout:
        result["ref"] = evaluate_reference(model, tokenizer, ref_heldout, catalog, max_new, eos)
    result["score"] = sum(result.values()) / len(result)
    return result


def save(model, path: Path, state: dict) -> None:
    model.save_pretrained(path)
    (path / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path, required=True, help="the merged round-3 model")
    parser.add_argument("--tasks", type=Path, default=ROOT / "data" / "rl_tasks.jsonl")
    parser.add_argument("--heldout", type=Path, default=ROOT / "data" / "rl_heldout.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / "grpo")
    parser.add_argument("--ref-tasks", type=Path, help="ordinary questions scored against a reference (round 5's prepare_rl_ref.py, not published)")
    parser.add_argument("--ref-heldout", type=Path, help="held-out ordinary questions for the checks")
    parser.add_argument("--ref-per-step", type=int, default=0, help="how many of the --prompts tasks per step are ordinary questions")
    parser.add_argument("--image", default="lxask-test", help="Docker image with the repo (exec_server.py)")
    parser.add_argument("--max-steps", type=int, default=300)
    parser.add_argument("--prompts", type=int, default=8, help="tasks per step")
    parser.add_argument("--group", type=int, default=6, help="replies sampled per task")
    parser.add_argument("--temperature", type=float, default=0.9)
    parser.add_argument("--max-new", type=int, default=96)
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--beta", type=float, default=0.05, help="weight of the KL penalty")
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--micro", type=int, default=4, help="sequences per forward pass")
    parser.add_argument("--eval-every", type=int, default=50)
    parser.add_argument("--eval-n", type=int, default=100)
    parser.add_argument("--precision", choices=sorted(DTYPES), default="bf16")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--resume", action="store_true", help="continue from <out>/last")
    parser.add_argument("--merge", action="store_true", help="merge <out>/best into <out>/merged at the end")
    args = parser.parse_args()
    dtype = DTYPES[args.precision]
    torch.manual_seed(args.seed)

    tokenizer = AutoTokenizer.from_pretrained(args.base)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    eos = {tokenizer.convert_tokens_to_ids("<|im_end|>"), tokenizer.eos_token_id}
    records = load_records(args.tasks)
    heldout = load_records(args.heldout)[: args.eval_n]
    ref_records = load_records(args.ref_tasks) if args.ref_tasks and args.ref_per_step else []
    ref_heldout = load_records(args.ref_heldout)[: args.eval_n] if args.ref_heldout and ref_records else []
    catalog = load_catalog(config.index_path())
    print(f"{len(records)} training tasks, {len(heldout)} held-out for checks"
          + (f"; {len(ref_records)} ordinary questions, {args.ref_per_step} per step, {len(ref_heldout)} held out" if ref_records else ""),
          flush=True)

    model = AutoModelForCausalLM.from_pretrained(args.base, dtype=dtype, device_map={"": 0})
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    model.config.pad_token_id = tokenizer.pad_token_id
    model = get_peft_model(model, LoraConfig(r=args.rank, lora_alpha=2 * args.rank, lora_dropout=0.0,
                                             target_modules=TARGET_MODULES, task_type="CAUSAL_LM"))
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=args.lr)

    args.out.mkdir(parents=True, exist_ok=True)
    state = {"step": 0, "best": -1.0, "best_step": 0}
    if args.resume and (args.out / "last" / "state.json").exists():
        set_peft_model_state_dict(model, load_file(str(args.out / "last" / "adapter_model.safetensors")))
        state = json.loads((args.out / "last" / "state.json").read_text(encoding="utf-8"))
        print(f"resumed at step {state['step']} (best {state['best']:.3f} at {state['best_step']})", flush=True)

    rng = random.Random(args.seed + state["step"])
    pools = [(records, args.prompts - (args.ref_per_step if ref_records else 0))]
    if ref_records:
        pools.append((ref_records, args.ref_per_step))
    orders = [rng.sample(range(len(pool)), len(pool)) for pool, _ in pools]
    cursors = [0] * len(pools)
    sandbox = Sandbox(args.image)
    log = open(args.out / "log.jsonl", "a", encoding="utf-8")
    try:
        if state["step"] == 0:
            first = checks(model, tokenizer, heldout, ref_heldout, sandbox, catalog, args.max_new, eos)
            state["best"] = first["score"]
            print(f"step 0: {first} (the starting model)", flush=True)
            log.write(json.dumps({"step": 0, **first}) + "\n")
        while state["step"] < args.max_steps:
            started = time.time()
            chunk = []
            for n, ((pool, count), order) in enumerate(zip(pools, orders)):
                if cursors[n] + count > len(order):
                    rng.shuffle(order)
                    cursors[n] = 0
                chunk += [pool[i] for i in order[cursors[n] : cursors[n] + count]]
                cursors[n] += count
            prompts = [prompt_ids(tokenizer, record["messages"]) for record in chunk]
            replies = sample(model, tokenizer, prompts, args.group, args.temperature, args.max_new, eos)
            texts = [tokenizer.decode(reply, skip_special_tokens=True) for reply in replies]
            owners = [chunk[i // args.group] for i in range(len(replies))]
            runs = [i for i, o in enumerate(owners) if "task" in o]
            results = dict(zip(runs, sandbox.run([{"task": owners[i]["task"], "command": parse_answer(texts[i])} for i in runs])))
            scored = [score(o["task"]["q"], t, results[i]["success"], catalog) if "task" in o
                      else score_reference(o["q"], t, o["reference"], catalog)
                      for i, (o, t) in enumerate(zip(owners, texts))]
            rewards = [reward for reward, _ in scored]
            weights = advantages(rewards, args.group)

            sequences, kept = [], []
            for i, (reply, weight) in enumerate(zip(replies, weights)):
                if weight is None or not reply:
                    continue
                prompt = prompts[i // args.group]
                sequences.append({"input_ids": torch.tensor(prompt + reply),
                                  "labels": torch.tensor([IGNORE] * len(prompt) + reply)})
                kept.append(weight)
            pg, kl = 0.0, 0.0
            if sequences:
                pg, kl = update(model, sequences, kept, args.beta, args.micro, dtype)
                torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
                torch.cuda.empty_cache()
            entry_memory = round(torch.cuda.max_memory_allocated() / 2**30, 2)
            torch.cuda.reset_peak_memory_stats()
            state["step"] += 1
            entry = {"step": state["step"], "reward": sum(rewards) / len(rewards),
                     "success": sum(d["success"] for _, d in scored) / len(scored),
                     "trained": len(sequences), "kl": kl, "pg": pg, "seconds": round(time.time() - started, 1),
                     "gpu_gb": entry_memory}
            if state["step"] % args.eval_every == 0 or state["step"] == args.max_steps:
                entry.update(checks(model, tokenizer, heldout, ref_heldout, sandbox, catalog, args.max_new, eos))
                if entry["score"] > state["best"]:
                    state["best"], state["best_step"] = entry["score"], state["step"]
                    save(model, args.out / "best", state)
                save(model, args.out / "last", state)
            log.write(json.dumps(entry) + "\n")
            log.flush()
            if state["step"] % 5 == 0 or "heldout" in entry:
                print(f"step {entry['step']}: reward {entry['reward']:.3f}, success {entry['success']:.2f}, "
                      f"trained on {entry['trained']}/{len(replies)}, kl {kl:.4f}, {entry['seconds']} s, "
                      f"GPU peak {entry['gpu_gb']} GB"
                      + (f", held-out {entry['heldout']:.3f}" + (f", ref {entry['ref']:.3f}" if "ref" in entry else "")
                         + f" (best score {state['best']:.3f} at {state['best_step']})" if "heldout" in entry else ""),
                      flush=True)
    finally:
        sandbox.close()
        log.close()

    print(f"best held-out success {state['best']:.3f} at step {state['best_step']}", flush=True)
    if args.merge and (args.out / "best").exists():
        del model, optimizer
        torch.cuda.empty_cache()
        merge(str(args.base), args.out / "best", args.out / "merged", args.precision)


if __name__ == "__main__":
    main()
