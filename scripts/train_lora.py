"""Fine-tune the base model on data/train.jsonl with LoRA, then merge the adapter.

    uv run --group train python scripts/train_lora.py

The base model is small enough to train unquantized on an 8 GB GPU (with gradient
checkpointing): bf16 by default, --precision fp16 on a T4. The loss covers only the assistant's reply. Output:
runs/lora/adapter (the LoRA weights) and runs/lora/merged (a plain model that
scripts/export_gguf.sh converts to GGUF).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import torch
from peft import LoraConfig, PeftModel, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments

ROOT = Path(__file__).resolve().parents[1]
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
IGNORE = -100


def encode(tokenizer, messages: list[dict], max_len: int) -> dict | None:
    """Token ids for the whole conversation, with labels only on the assistant's reply."""
    prompt_text = tokenizer.apply_chat_template(messages[:-1], tokenize=False, add_generation_prompt=True)
    full_text = tokenizer.apply_chat_template(messages, tokenize=False)
    if not full_text.startswith(prompt_text):
        raise ValueError("the chat template does not render the prompt as a prefix of the conversation")
    # Tokenized apart, as at inference: the runtime tokenizes the prompt, then the
    # model produces the reply's tokens from that boundary on.
    prompt = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
    full = prompt + tokenizer(full_text[len(prompt_text) :], add_special_tokens=False)["input_ids"]
    if len(full) > max_len:
        return None
    # int32 tensors, not Python lists: the whole set stays in memory for the run.
    labels = [IGNORE] * len(prompt) + full[len(prompt) :]
    return {
        "input_ids": torch.tensor(full, dtype=torch.int32),
        "labels": torch.tensor(labels, dtype=torch.int32),
    }


def load(path: Path, tokenizer, max_len: int) -> list[dict]:
    examples, skipped = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        example = encode(tokenizer, json.loads(line)["messages"], max_len)
        if example is None:
            skipped += 1
        else:
            examples.append(example)
    print(f"{path.name}: {len(examples)} examples, {skipped} over {max_len} tokens skipped")
    return examples


class Collator:
    def __init__(self, pad_id: int):
        self.pad_id = pad_id

    def __call__(self, batch: list[dict]) -> dict:
        width = max(len(example["input_ids"]) for example in batch)
        ids = torch.full((len(batch), width), self.pad_id, dtype=torch.long)
        labels = torch.full((len(batch), width), IGNORE, dtype=torch.long)
        mask = torch.zeros((len(batch), width), dtype=torch.long)
        for row, example in enumerate(batch):
            length = len(example["input_ids"])
            ids[row, :length] = example["input_ids"]
            labels[row, :length] = example["labels"]
            mask[row, :length] = 1
        return {"input_ids": ids, "labels": labels, "attention_mask": mask}


DTYPES = {"bf16": torch.bfloat16, "fp16": torch.float16}


def merge(base: str, adapter: Path, out: Path, precision: str = "bf16") -> None:
    """Fold a saved LoRA adapter into the base weights and write a plain model."""
    model = AutoModelForCausalLM.from_pretrained(base, dtype=DTYPES[precision], device_map={"": 0})
    merged = PeftModel.from_pretrained(model, str(adapter)).merge_and_unload()
    merged.save_pretrained(out, safe_serialization=True)
    AutoTokenizer.from_pretrained(base).save_pretrained(out)
    print(f"merged model -> {out}")


class ReplyLossTrainer(Trainer):
    """Computes the loss from the reply's positions only.

    The stock forward pass builds logits for every position, and with a 152k-token
    vocabulary that is gigabytes per batch. Only the reply carries a loss, so the
    output layer is applied to those few positions and nothing else.
    """

    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        lm = model.get_base_model()
        hidden = lm.model(input_ids=inputs["input_ids"], attention_mask=inputs["attention_mask"]).last_hidden_state
        hidden, targets = hidden[:, :-1], inputs["labels"][:, 1:]  # position t predicts token t+1
        keep = targets != IGNORE
        loss = torch.nn.functional.cross_entropy(lm.lm_head(hidden[keep]).float(), targets[keep])
        return (loss, {}) if return_outputs else loss


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="Qwen/Qwen2.5-Coder-1.5B-Instruct")
    parser.add_argument("--train", type=Path, default=ROOT / "data" / "train.jsonl")
    parser.add_argument("--val", type=Path, default=ROOT / "data" / "val.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / "lora")
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--grad-accum", type=int, default=4)
    parser.add_argument("--max-len", type=int, default=1024)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--alpha", type=int, default=32)
    parser.add_argument("--dropout", type=float, default=0.05)
    parser.add_argument("--grad-checkpoint", action=argparse.BooleanOptionalAction, default=True,
                        help="trade speed for memory; turn off only with a small --batch-size")
    parser.add_argument("--eval-steps", type=int, default=200)
    parser.add_argument("--max-steps", type=int, default=-1, help="stop early (for a trial run)")
    parser.add_argument("--precision", choices=sorted(DTYPES), default="bf16",
                        help="bf16 for recent GPUs; fp16 for a T4, which has no native bf16 "
                             "(the base is then held in fp16 and the adapter trained in fp32)")
    parser.add_argument("--resume", action="store_true",
                        help="continue from the last checkpoint in --out, if there is one")
    parser.add_argument("--merge-checkpoint", type=Path, metavar="DIR",
                        help="skip training: merge this saved checkpoint into the base model")
    args = parser.parse_args()

    if args.merge_checkpoint:
        merge(args.base, args.merge_checkpoint, args.out / "merged", args.precision)
        return

    tokenizer = AutoTokenizer.from_pretrained(args.base)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    train = load(args.train, tokenizer, args.max_len)
    val = load(args.val, tokenizer, args.max_len)

    model = AutoModelForCausalLM.from_pretrained(args.base, dtype=DTYPES[args.precision], device_map={"": 0})
    if args.grad_checkpoint:
        # Without checkpointing the activations of a batch of 1000-token prompts do not
        # fit in 8 GB next to the weights. It recomputes each layer on the backward pass;
        # the inputs must require grad for that to reach the LoRA weights.
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()
        model.config.use_cache = False
    model = get_peft_model(
        model,
        LoraConfig(
            r=args.rank,
            lora_alpha=args.alpha,
            lora_dropout=args.dropout,
            target_modules=TARGET_MODULES,
            task_type="CAUSAL_LM",
        ),
    )
    model.print_trainable_parameters()

    steps_per_epoch = math.ceil(len(train) / (args.batch_size * args.grad_accum))
    total_steps = args.max_steps if args.max_steps > 0 else math.ceil(steps_per_epoch * args.epochs)

    trainer = ReplyLossTrainer(
        model=model,
        train_dataset=train,
        eval_dataset=val,
        data_collator=Collator(tokenizer.pad_token_id),
        args=TrainingArguments(
            output_dir=str(args.out / "checkpoints"),
            num_train_epochs=args.epochs,
            max_steps=args.max_steps,
            per_device_train_batch_size=args.batch_size,
            per_device_eval_batch_size=args.batch_size * 2,
            gradient_accumulation_steps=args.grad_accum,
            learning_rate=args.lr,
            lr_scheduler_type="cosine",
            warmup_steps=max(1, round(0.03 * total_steps)),
            bf16=args.precision == "bf16",
            fp16=args.precision == "fp16",
            train_sampling_strategy="group_by_length",
            logging_steps=20,
            eval_strategy="steps",
            eval_steps=args.eval_steps,
            save_strategy="steps",
            save_steps=args.eval_steps,
            save_total_limit=2,
            load_best_model_at_end=True,
            metric_for_best_model="eval_loss",
            greater_is_better=False,
            prediction_loss_only=True,
            report_to="none",
        ),
    )
    checkpoints = args.out / "checkpoints"
    resume = args.resume and checkpoints.is_dir() and any(checkpoints.glob("checkpoint-*"))
    trainer.train(resume_from_checkpoint=bool(resume))
    print("best eval loss:", trainer.state.best_metric)

    model.save_pretrained(args.out / "adapter")
    merged = model.merge_and_unload()
    merged.save_pretrained(args.out / "merged", safe_serialization=True)
    tokenizer.save_pretrained(args.out / "merged")
    print(f"merged model -> {args.out / 'merged'}")


if __name__ == "__main__":
    main()
