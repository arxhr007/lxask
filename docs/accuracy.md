# Accuracy and limits

Every number on this page was measured on held-out sets, with commands actually
run in a sandbox as an unprivileged user — not pattern-matched against a
reference. That is what a 1.5B-parameter model buys you: answers in about 2
seconds on a laptop CPU, offline, in roughly 1.3 GB of memory, that still beat
the untuned model they were trained from on a public benchmark.

## Held-out numbers

| test | n | result |
|---|---|---|
| **command run in a sandbox does the job**, hand-written tasks | 46 | **87%** |
| **command run in a sandbox does the job**, generated file and text tasks | 200 | **75%** |
| strict execution, the shipped precision adapter | 160 | **73.8%** (baseline: 35.0%) |
| right program, everyday questions (hand-written) | 150 | 96-99% |
| right program, NL2Bash questions | 150 | 80% |
| right program, tldr examples | 150 | 75% |
| multi-part requests: parts handled | 288 | 91-100% |
| restriction kept ("without lsof") | 40 | 80% |
| restriction kept, programs never ruled out in training | 79 | 76% |
| vague proxy request asked back, clear one answered in the right form | 24 | 79% |
| past mistakes that must not come back | 8 | 8 of 8 |

The sandbox rows are the real measure: each task builds a few files, runs
lxask's command on them as `nobody` with no network, and checks the result. To
pass, the command must exit cleanly, leave files alone when the task only asks
a question, and use nothing the task ruled out — "looks right" does not count.
"Right program" rows are the lighter check: the command uses a program a
correct answer would use, whatever the options.

How each set is made and scored: [development.md](development.md#measuring-it).
The precision-adapter row is the headline of the routing design: tasks built to
punish near-misses — wrong filenames, missing quotes, wrong flags, files
touched that should be left alone — more than doubled strict execution success
over the base pipeline (35.0% → 73.8%).

## The public benchmark: NL2SH-ALFA

The sets above are lxask's own. For a number that can be checked against other
tools, the model was also run on the public
[NL2SH-ALFA](https://huggingface.co/datasets/westenfelder/NL2SH-ALFA) test set (300
shell tasks) through the InterCode-ALFA harness (`icalfa` 0.3.6): each answer is run
in a Docker container and counts as solved only when the harness gives it a reward
of exactly 1. The harness's embedding judge decides (threshold 0.75, a local
embedding model; nothing hosted).

Answers come from the real pipeline (retrieval, checks, retries, the 0.8 adapter) and,
for comparison, from the untuned Qwen2.5-Coder-1.5B the model was trained from (the stock
Q4_K_M build), greedy, with no retrieval and no checks. The untuned model is run three
ways, because how it is asked changes its score a lot:

- **base, raw**: a plain instruction, and only code fences stripped from its reply. This
  was the baseline first published here (31%). It was handicapped: 199 of its 300 replies
  were wrapped in backticks or started with `$ `, which fail when run, and 169 of those
  scored 0 for that reason alone.
- **base, cleaned**: the same replies with the backticks and `$ ` removed.
- **base, three examples**: the plain instruction plus three worked examples (none from
  the test set) and "no backticks, no `$`". This is the strongest baseline and the one the
  README compares against.

| variant | solved | 95% CI (Wilson) |
|---|---|---|
| **lxask** | **187/300 (62%)** | 57-68% |
| untuned base, three examples | 162/300 (54%) | 48-60% |
| untuned base, cleaned | 153/300 (51%) | 45-57% |
| untuned base, raw (the handicapped first run) | 92/300 (31%) | 26-36% |
| the test set's own reference commands (harness sanity check) | 299/300 (99.7%) | |
| lxask, first answer kept (no retries) | 187/300 (62%) | 57-68% |
| lxask, without retrieved notes | 186/300 (62%) | 56-67% |

Paired, against the strongest baseline (three examples), lxask solves 46 tasks the base
model misses and misses 21 the base solves (exact McNemar, two-sided p = 0.003). Against
the cleaned base: 54 vs 20 (p = 1e-4).

63 of the 300 tasks overlap lxask's training sources (19 share the request text, 61
share a command). On the 237 that do not:

| variant | solved |
|---|---|
| **lxask** | **145/237 (61%, CI 55-67%)** |
| untuned base, three examples | 128/237 (54%, CI 48-60%) |
| untuned base, cleaned | 118/237 (50%, CI 44-56%) |

(Against the three-example base: 34 tasks lxask only, 17 base only; p = 0.024.) On the 63
overlapping tasks lxask solves 42 (67%) against 34 (54%) for the three-example base.

The advantage holds at every difficulty in the set's own field, 100 tasks each —
and it grows as tasks get harder:

| difficulty | lxask | base, three examples | base, cleaned |
|---|---|---|---|
| 0 (easiest) | 75% | 69% | 66% |
| 1 | 64% | 54% | 51% |
| 2 | 48% | 39% | 36% |

Reading the comparison precisely: the paired test is the sound one (the same
tasks, answered two ways — p = 0.003), while the unpaired confidence intervals
overlap. On this particular set the retries and retrieved notes measure
neutral — the gain lives in the fine-tuned weights and adapters themselves,
which is the more durable place for it. 30 of the 1,800 scored answers (4 to 6
per variant, none of them the reference rows) got 0 from an embedding-judge
context error, spread evenly across variants; the reference commands scoring
299/300 is what says the harness itself is sound.

*Note.* An earlier revision of this page compared against the raw baseline
(31%), whose replies failed on formatting — code fences and `$ ` prompts the
harness cannot run — rather than content. The raw row is kept in the table for
the record; the comparison now uses the properly cleaned and prompted
baselines.

Numbers from other tools on this set are not directly comparable. whatisit-nl2sh
self-reports 0.62 for its tuned 1.5B and 0.54 for the untuned one, and
barbarabhb/nl2sh-qwen25-coder-1.5b-GGUF self-reports 0.657; both used their own answer
protocol (one reports `max_tokens=64`). The untuned model scoring 0.54 there and 0.54
here with three examples is consistent. The benchmark authors publish 0.73 for GPT-4o, a
hosted model; that number is theirs and was not re-run here.

To reproduce: `scripts/nl2sh_alfa_answers.py` makes the answers (variants `lxask`,
`no_retry`, `no_rag`, `base`, `base_fewshot`), `scripts/nl2sh_alfa_score.py` scores them
with `icalfa` 0.3.6 (Docker, plus an Ollama server with `mxbai-embed-large`), and
`scripts/nl2sh_alfa_stats.py` prints the tables above.

## Known limits

A small model has edges, and lxask is built around knowing where they are:

- **Details.** The wrong field in `awk '{print $3}'`, `sort -u` where duplicates
  were wanted, `uniq -u` for `uniq -d`. Most failed sandbox tasks are like this —
  the precision adapter exists to push on exactly this class.
- **Placeholders.** A path from the documentation (`path/to/directory`) can stay in
  the command instead of the one you named.
- **Invented subcommands.** Subcommands are checked for `systemctl` and its kin
  only; elsewhere an invented one (`apt-config set`) can pass unmarked.
- **Judgement.** "Which files are safe to remove" gets commands for looking, not the
  judgement.
- **Heavy one-liners.** Time-windowed aggregation over quoted CSV and similar are
  beyond a model this size.
- **Vague requests.** Asking back covers proxies only; other vague requests still
  get a guess.
- **Explanations.** Programs without a man page on the reference install are checked
  by name only, and their options are not explained.

Commands are printed for you to read and run — lxask never executes them
itself.

## Platforms

Tuned and tested on Debian and Ubuntu. On other distributions most commands are the
same (find, grep, awk, tar, ssh, systemctl, ip, ss), and package commands adapt: see
[configuration.md](configuration.md#package-managers). Other distribution-specific
advice may still lean Debian, for example `ufw` where `firewalld` runs. No numbers
have been measured on Fedora or Arch yet.

## Speed

Measured with the release program (version 0.6; the adapter added in 0.8 costs
about 2% more time) on a Ryzen 9 270 laptop CPU, in a 2 GB container, median of
five everyday questions, including loading (when not served), retrieval, checks
and explanations:

| | time |
|---|---|
| one question with `lxask --serve` running | 2.0 s |
| one question, `lxask "..."` (loads the model each time) | 6.6 s |
| multi-part request (median of ten), not served | 15.6 s |

A multi-part request costs one model call per part plus one for the split, and up to
two more per part when a check objects. Peak memory while answering is about 1.3 GB
(resident, measured in the container); a slower or older CPU takes several times as
long.

### Where the time goes

Most of an answer is the model reading its prompt, not writing the reply and not
searching. `LXASK_TIMING=1` prints the split. On the laptop above, for "count the lines
in notes.txt", served (version 1.4.0):

| | tokens | time |
|---|---|---|
| reading the prompt (the notes retrieval found, about 170 tokens) | 174 | 1.4 s |
| writing the reply | 10 | 0.4 s |
| retrieval (the encoder and the search) | | 0.1 s |

With `--no-rag` there are no notes, so only the question is read (the rest of the prompt
is kept from the last answer): 13 tokens, 0.4 s, and 0.7 s for the whole model call. A
question that needs a retry reads a longer prompt twice. Reading a prompt is
compute-bound, so a CPU gains most from instructions the build can use.

### Raspberry Pi

Reported by a user on a Compute Module 5 (Cortex-A76, 4 cores), version 1.2.0 as
installed, and the same program built for that CPU (`-C target-cpu=native`):

| | installed build | built for the CPU |
|---|---|---|
| one question, model kept loaded | 16.5 s | 7.0 s |
| one question, loading each time | 22.6 s | 13.2 s |
| `--no-rag`, kept loaded | 3.0 s | 1.7 s |
| `--no-rag`, loading each time | 8.7 s | 7.8 s |
| peak memory | 1.3 GB | 1.5 GB |

The installed `lxask-aarch64` is built for plain ARMv8, without the dot-product
instructions the A76 has. `lxask-aarch64-dotprod` is built for ARMv8.2 with them, and
`install.sh` picks it where `/proc/cpuinfo` lists `asimddp`. Its speed on a real device is
not measured yet.
