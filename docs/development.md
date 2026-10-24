# Development

Building lxask from source, the training data, and how it is measured. For using lxask, see
[usage.md](usage.md); for the design, [how-it-works.md](how-it-works.md); for every version's
numbers, [results.md](results.md).

## Layout

```
rust/         crates/lxask-core: answer (the flow), split, retrieve, prompt, validate, constraints,
              explain, safety; crates/lxask-llm: llama.cpp; crates/lxask: the program (CLI, server,
              feedback); crates/lxask-py: the core for Python, as lxask._core
src/lxask/     Python wrappers over lxask._core for training and evaluation, llm (llama-cpp-python,
              GPU evaluation), index (builds docs.sqlite)
scripts/      sources -> index -> generated data -> dataset -> fine-tune -> GGUF -> eval
evalsets/     the test sets (see "Measuring it")
docs/         user and developer documentation
seed/         inputs that are not downloaded: paraphrases, and the filtered data a
              hosted model wrote (published since 2026-10-21)
tests/        pytest, no model needed
```

## Build it (Linux or WSL2)

```sh
source scripts/env.sh                      # uv on PATH, venv on the Linux filesystem
uv sync                                    # Python side; builds lxask._core (needs Rust: rustup.rs)
uv run pytest
cargo build --release --manifest-path rust/Cargo.toml -p lxask   # the program, for this machine

uv run python scripts/fetch_sources.py     # tldr, NL2Bash, man pages -> data/raw
uv run python scripts/fetch_models.py embed qwen2.5-coder-1.5b --use qwen2.5-coder-1.5b
uv run python scripts/build_index.py       # -> ~/.local/share/lxask/docs.sqlite
./rust/target/release/lxask "show disk usage of each folder"   # the program just built
```

The release programs are built with `scripts/build_release.sh DIR`: `cargo zigbuild`
against glibc 2.28, llama.cpp without OpenMP and without `-march=native`, one build
per CPU level. It needs `rustup target add aarch64-unknown-linux-gnu`, `cargo install
cargo-zigbuild`, `pip install ziglang maturin libclang` (bindgen needs libclang; no
root needed). Each takes 5 to 9 minutes with two jobs. A release is then:

```sh
scripts/build_release.sh DIR      # the three programs and the Python wheel
scripts/release_files.sh DIR      # adds the installed model, adapter, encoder, index,
                                  # LICENSE, NOTICE, install.sh and SHA256SUMS
gh release create vX.Y.Z -R arxhr007/lxask DIR/*
```

Training, export and evaluation are heavy and want a GPU (a free Colab T4 or a
laptop RTX card is enough):

```sh
uv run python scripts/build_dataset.py            # -> data/train.jsonl, data/val.jsonl
uv sync --group train                             # PyTorch (CUDA 12.8), transformers, peft
uv run --group train python scripts/train_lora.py # LoRA; --precision fp16 on a T4
scripts/export_gguf.sh                            # merge -> GGUF -> Q4_K_M, Q6_K
uv run python scripts/fetch_models.py --use lxask-Q4_K_M
```

## Training data

Commands in the training set come from three places, and every one has passed
`lxask.validate`:

- **tldr-pages**: the documentation's own examples.
- **NL2Bash**: human-written pairs, minus what the validator rejects. That
  removes the macOS commands (`top -l`, `pbcopy`, `sed -i ''`) round 1 learned from.
- **Generated**: `scripts/generate_data.py` asks a language model for natural
  questions with commands and for multi-part requests split into self-contained
  steps. Single tasks come from three jobs: `tasks` and `depth` (one request per
  command, grounded in its tldr page and man options; `depth` asks again for the
  most used commands) and `everyday` (questions that never name the tool). Multi-part
  requests come from `scenarios` (1 to 4 parts) and `long` (4 to 6 parts).
  Round 3 added `scenarios3`, `retask`, `constraints` and `fix`, round 6 `forbid`,
  `forbid_scenarios`, `contexts` and `forbid_twins` (see [results](results.md)).
  `scripts/filter_generated.py` keeps an item only if every command in it passes
  the validator.

"Passed the validator" means the program and its options exist. It does not mean
the command does what the question asks. For that, the `review` job has a stronger
model judge every task the small model wrote, and the filter drops what it rejects
(about one in ten; roughly two thirds of those were plainly wrong, the rest
arguable). A sample is still read by hand, and a percent or two of the remaining
answers are expected to be wrong in meaning.

The data written with hosted language models is published in this repository, and has
been since 2026-10-21: the filtered output (`seed/generated_tasks.jsonl`,
`generated_scenarios.jsonl`, `generated_exec.jsonl`, `generated_fixes.jsonl`) and the
evaluation sets made from it (`multistep_heldout`, `multistep3_heldout`, `exec_heldout`,
`forbidden_heldout`) were used to train and measure the released model. They were kept
out of the public repository when it first opened on 2026-10-19, and added on 2026-10-21,
so anyone can now retrain the model and re-measure every number in `accuracy.md` from a
clone. The rephrasings of tldr examples (`seed/paraphrases.jsonl`) were written with
Qwen2.5-7B-Instruct run locally (NOTICE says the same). `build_dataset.py` builds from
tldr, NL2Bash and these files; `eval.py` finds every set. To make more of it,
`generate_data.py`
needs an API key in `LXASK_GEN_API_KEY` or `~/.config/lxask/gen_api_key`, an endpoint in
`LXASK_GEN_API_URL`, a model in `LXASK_GEN_MODEL` or `--model`, and prices in
`--price-in`/`--price-out` (it keeps a spend ledger and stops at `--cap`, default $50),
then `filter_generated.py`; read your model
provider's terms on using its output for training first.

The man pages come from the machine that builds the index (`fetch_sources.py man`
renders `/usr/share/man`), so build it on a full install: a container or Colab has
almost none.

## Measuring it

```sh
uv run python scripts/eval_retrieval.py -v   # does retrieval surface the right command?
uv run python scripts/eval.py                # end to end, through the real pipeline
uv run python scripts/eval.py --no-rag       # the model without retrieved notes
uv run python scripts/eval.py --engine rust --binary lxask   # the program itself (`lxask batch`, CPU)
LXASK_ALLOW_EXEC=1 python scripts/exec_eval.py   # runs the commands; Colab or a container only
```

| set | what | role |
|---|---|---|
| `everyday` | 70 hand-written single questions | dev: retrieval was tuned on it |
| `test_everyday` | 80 hand-written single questions | test |
| `multistep` | 10 multi-part requests, with the programs accepted for each part and patterns that must not appear | test |
| `multistep_heldout` | 205 generated scenarios set aside before training | test |
| `multistep3_heldout` | 73 round-3 scenarios (literals kept in every step) set aside before training | test |
| `nl2bash_test` | 150 held-out NL2Bash pairs with Linux-valid references (GPL-3.0, see its NOTICE) | test |
| `tldr_heldout` | 150 tldr examples kept out of round-1 training | weaker test: round 2's generator saw these pages |
| `exec_tasks` | 46 file and text tasks with fixtures and expected results, 13 of them with a restriction | test, by execution |
| `exec_heldout` | 200 generated execution tasks set aside before RL training | test, by execution |
| `regressions` | earlier failures | each must pass |
| `forbidden` | 40 requests that rule a program out ("without lsof"), single and multi-part | test |
| `forbidden_heldout` | 79 generated requests ruling out programs no training request rules out | test |
| `ambiguous` | 24 proxy requests: 8 too vague (must be asked back), 16 naming a context with the form a right command has | test |

`eval.py` reports each of these on its own, because they answer different
questions: a command was produced; the right program is used; the validator finds
nothing wrong; how many parts of a multi-part request were handled; whether
anything the request ruled out appears; exact match with a reference (which
undercounts, since one reference is rarely the only correct command).

`exec_eval.py` is the only measure of whether a command does the job. It builds a
small fixture, runs the command as `nobody` with a timeout, and checks the output
or the resulting files. Looking right is not enough: the command must also exit 0,
leave the fixture untouched when the task only asks a question, leave the files the
task says to keep (`unchanged`) as they were, write nothing into HOME, and run no
program the task rules out. The same judge scores the RL reward (`exec_server.py`)
and proves new tasks (`prove_exec_tasks.py`). It refuses to run outside Colab or a
container. In Docker,
with no network and the model files mounted read-only:

```sh
docker run --rm --init --network none --memory 2g -v ~/.local/share/lxask:/data:ro \
    -e LXASK_HOME=/data -e LXASK_MODEL=/data/models/lxask-round3-Q4_K_M.gguf \
    -e LXASK_ALLOW_EXEC=1 IMAGE python scripts/exec_eval.py
```

where IMAGE is any Python 3.12 image with the repository's scripts and evalsets and the
release wheel installed (`pip install lxask-1.0.0-cp310-abi3-manylinux_2_28_x86_64.whl
llama-cpp-python`). `--binary lxask` takes the answers from the program instead.

Anything too close to a test case (same command as a reference, or a question
within 0.92 cosine of one) is removed from the training set, and the count is
printed by `build_dataset.py`.

