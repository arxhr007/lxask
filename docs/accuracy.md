# Accuracy and limits

lxask usually picks the right tool and the right shape of command. The details (an
awk field number, `uniq -d` against `-u`, one flag) are wrong often enough that
every command deserves a read before you run it. A 1.5B-parameter model trades
accuracy for running anywhere, offline, in about 1.3 GB of memory.

## Numbers (version 0.8)

Every set below was kept out of training.

| test | n | result |
|---|---|---|
| right program, everyday questions (hand-written) | 150 | 96-99% |
| right program, tldr examples | 150 | 75% (50% exactly the reference command) |
| right program, NL2Bash questions | 150 | 80% |
| multi-part requests: parts handled | 288 | 91-100% |
| **command run in a sandbox does the job**, generated file and text tasks | 200 | **75%** |
| the same, hand-written tasks | 46 | 87% |
| restriction kept ("without lsof") | 40 | 80% |
| restriction kept, programs never ruled out in training | 79 | 76% |
| vague proxy request asked back, clear one answered in the right form | 24 | 79% |
| past mistakes that must not come back | 7 | 7 of 7 |

"Right program" means the command uses a program a correct answer would use; it
says nothing about the options. The sandbox tests are the real measure: each builds
a few files, runs lxask's command on them as an unprivileged user with no network,
and checks the result. To pass, the command must also exit cleanly, leave files
alone when the task only asks a question, and use nothing the task ruled out.

How each set is made and scored: [development.md](development.md#measuring-it).
Every version's numbers: [results.md](results.md).

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

The set's own difficulty field, 100 tasks each:

| difficulty | lxask | base, three examples | base, cleaned |
|---|---|---|---|
| 0 (easiest) | 75% | 69% | 66% |
| 1 | 64% | 54% | 51% |
| 2 | 48% | 39% | 36% |

Things to read honestly here. The gain over a fairly prompted untuned model is about 8
points, and the two confidence intervals overlap (the paired test does not, because the
same tasks are compared). The retries and the retrieved notes make no measurable
difference on this set: keeping the first answer scores the same 187, and dropping
retrieval moves one task (186). The gain is fine-tuning, not the pipeline around it. And
30 of the 1,800 scored answers (4 to 6 per variant, none of them the reference-command
rows) got 0 from an embedding-judge context error, spread evenly across variants; the
reference commands scoring 299/300 is what says the harness itself is sound.

**A correction.** Until 2026-10-23 this page and the README reported the raw baseline
(31%) and so a 31-point gap. The raw run's reply cleanup only removed code fences, so
most of its replies failed on formatting, not on content. It is kept in the table above
for the record and the cleanup is fixed in `scripts/nl2sh_alfa_answers.py`.

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

## Where it goes wrong

- **Details.** The wrong field in `awk '{print $3}'`, `sort -u` where duplicates
  were wanted, `uniq -u` for `uniq -d`. Most failed sandbox tasks are like this.
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

## Platforms

Tuned and tested on Debian and Ubuntu. On other distributions most commands are the
same (find, grep, awk, tar, ssh, systemctl, ip, ss), and package commands adapt: see
[configuration.md](configuration.md#package-managers). Other distribution-specific
advice may still lean Debian, for example `ufw` where `firewalld` runs. No numbers
have been measured on Fedora or Arch yet.

## Speed

Measured with the release program (version 0.6; the adapter added in 0.8 cost about
2% more time) on a Ryzen 9 270 laptop CPU, in a 2 GB container, median of five
everyday questions, including loading (when not served), retrieval, checks and
explanations:

| | time |
|---|---|
| one question, `lxask "..."` (loads the model each time) | 6.6 s |
| one question with `lxask --serve` running | 2.0 s |
| multi-part request (median of ten), not served | 15.6 s |

A multi-part request costs one model call per part plus one for the split, and up to
two more per part when a check objects. Peak memory while answering is about 1.3 GB
(resident, measured in the container); a slower or older CPU takes several times as
long.
