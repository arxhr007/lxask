# Results

lxask was called linu from 0.3 to 0.8, and these notes use the names of their time.

Every version and training round, newest first, with what was tried and not shipped.

### The public benchmark: NL2SH-ALFA (300 tasks), the shipped model

The model shipped since 0.8 (round 4 with the restriction adapter) was run on the
public NL2SH-ALFA test set through the InterCode-ALFA harness (`icalfa` 0.3.6, every
answer executed in Docker, embedding judge at 0.75, local). It solves 187 of 300
(62%, 95% CI 57-68%) against 162 of 300 (54%, CI 48-60%) for the untuned
Qwen2.5-Coder-1.5B it was trained from when that model is given three worked examples
(paired exact McNemar, p = 0.003); on the 237 tasks with no overlap with its training
sources, 61% against 54% (p = 0.024). Keeping the first answer (no retries) scores the
same 187 and dropping retrieved notes 186, so the checks and the retrieval show no
measurable effect on this set; the difference is fine-tuning. The first published
baseline (31%) was handicapped by a reply-cleanup bug and was corrected on 2026-10-23:
see the correction in [accuracy.md](accuracy.md#the-public-benchmark-nl2sh-alfa), which
also has the full table, the difficulty breakdown, the judge's context-error caveat and
why other tools' numbers on this set are not comparable.

### v0.8.0: a restriction adapter for the requests that need it

Round 6's fine-tune (6d, below) keeps restrictions far better than round 4 but drifts
on everyday requests. It ships as a 37 MB LoRA adapter of round 4 instead of a new
model, and is applied only to requests that rule out a program the catalogue knows,
or that mention a proxy (`linu_core::route`); every other request gets round 4
unchanged.

| set | 0.7.1 | 0.8 |
|---|---|---|
| forbidden (40) passed | 60.0% | **80.0%** |
| forbidden: ruled-out program used | 30.0% | **10.0%** |
| forbidden_heldout (79) passed | 60.8% | **75.9%** |
| ambiguous (24) passed | 70.8% | **79.2%** |
| exec_tasks (46), strict | 84.8% | **87.0%** |
| exec_heldout (200), strict | 75.0% | 75.0% |
| the other sets (everyday, tldr, nl2bash, multistep, regressions) | | 1 of 745 answers changed, same scores |

(GPU for the sets, the program on the CPU in Docker for the execution sets.) Only
the restriction tasks of exec_tasks are routed: 3 more pass (`awk 'END {print NR}'`
without wc, `awk /error/` without grep, `cat > copy` without cp) and 2 fewer (`cat
/dev/null > old.txt` empties the file instead of deleting it; `perl` counting lines
for words). None of the 200 heldout tasks is routed; their commands are identical.
The adapter adds about 90 MB of memory (1.30 GB peak) and switching it costs the
prompt cache, a few percent of time.

Of those 745 questions one is routed (an ssh SOCKS proxy; same answer). The one
changed answer (`rsync -an` for `rsync -av` on a dry run) is the question after it:
switching back empties the prompt cache, and a prompt computed whole rounds slightly
differently from one that reuses a cached prefix.

Applied at run time to the quantized model, the adapter does a little worse than the
same fine-tune merged before quantizing (forbidden 80.0% against 87.5%), and the same
elsewhere. The model alone is a `--model` or `LINU_ADAPTER=off` away.

### v0.7.1: three rule fixes (same model)

- A list of ruled-out programs is read to its end: "without lsof, netstat, or ss"
  rules out all three, while "show the log without the header, and grep for ERROR"
  still rules out nothing but the header.
- A request that only asks to look ("show which files can safely be removed",
  "list", "check", "count") answered with a command that deletes (`rm`, `rmdir`,
  `unlink`, `shred`, `find -delete`) is an objection: retried, and marked if it
  stays. It flags 2 of 32,421 known-good pairs.
- "Set a proxy in .curlrc" (or `.wgetrc`) names a context and is no longer asked
  back.

Every evaluation answer is the same as 0.7.0 (0 of 809 changed on the GPU); the
fixes are for requests the sets do not have.

### Round 6: training on restrictions and proxy contexts (shipped as the 0.8 adapter)

The gaps 0.7.0 left need data: new generator jobs `forbid` (963 requests that rule a
program out, answered without it), `forbid_scenarios` (98 multi-part ones),
`contexts` (311 proxy requests for APT, `/etc/environment`, the Docker daemon, npm
and others) and `forbid_twins` (905 of the same requests with the restriction
removed and the usual command, so that the model does not learn to avoid the tool
everywhere). A short LoRA fine-tune of round 4 on them plus a replay sample of the
old training data (`scripts/mix_finetune.py`), with a held-out set of programs never
ruled out in training (`forbidden_heldout`):

| set | round 4 | 6 (no twins) | 6b (twins) | 6c (replay answered by round 4) | 6d |
|---|---|---|---|---|---|
| forbidden passed | 60.0% | 82.5% | 75.0% | 82.5% | **87.5%** |
| forbidden_heldout passed | 60.8% | 79.7% | 78.5% | 77.2% | 75.9% |
| ambiguous passed | 70.8% | 79.2% | 91.7% | 79.2% | 79.2% |
| everyday program | 98.6% | 94.3% | 97.1% | 98.6% | 97.1% |
| test_everyday program | 96.2% | 93.8% | 95.0% | 93.8% | 95.0% |
| nl2bash_test program | 80.0% | 78.0% | 81.3% | 76.0% | 77.3% |
| tldr_heldout exact | 50.0% | 55.3% | 52.0% | 54.0% | 52.7% |
| exec_tasks / exec_heldout, strict | 84.8% / 75.0% | | 82.6% / 74.0% | 87.0% / 71.0% | |

Restrictions are learned, and they carry over to programs never seen ruled out
(heldout 60.8% -> 77-80%). The price is drift elsewhere: without twins the model
avoided the usual tool even when nothing was ruled out (`watch` -> `free --seconds
2`, `lsb_release` -> `uname`); replaying the original references pulled the
RL-trained model back toward pre-RL habits (`"$HOME/work"` for a relative path);
answering the replay with round 4 itself fixed most of that, but 6c still turns
some `find` answers into `grep -r` or `ls` and fails the ship rule on
test_everyday, nl2bash_test and exec_heldout. 6c also kept its step-100 checkpoint:
`train_lora.py` keeps the lowest loss on `data/val.jsonl`, whose original references
an RL-trained model has moved away from. 6d validates on 400 examples held out of its
own mix (`mix_finetune.py --val-out`), repeats the restriction examples once instead
of twice, replays 600 more answers that run `find`, `umount` or `apt`, at half the
learning rate. It is the best on restrictions and closer elsewhere, but still drifts
on a few everyday requests (`watch -n 2` -> `free --seconds 2`, `rm -r` -> `rmdir`):
every fine-tune on this data has paid for restrictions with everyday answers. So 6d
ships as an adapter used only where it is better (v0.8.0).

### v0.7.0: restrictions kept, vague requests asked back, stricter execution checks

Same model. Three rules, measured on two new sets and the strict execution checks:

| | 0.6.1 | 0.7 |
|---|---|---|
| forbidden (40): request's ruled-out program used | 57.5% | **32.5%** |
| forbidden: right program and nothing ruled out | 37.5% | **60.0%** |
| ambiguous (24): passed (vague ones asked back, others in form) | 37.5% | **70.8%** |
| exec_tasks (46), strict / without the strict checks | 78.3% / 93.5% | **84.8%** / 95.7% |
| exec_heldout (200), strict | 75.0% | 75.0% |
| the 745 answers of the other sets | | 0 changed |

(The program on the CPU for the first rows and the execution sets; the other sets on
the GPU through the same core.) The ruled-out check flags none of about 30,000
known-good generated and reference pairs, and asking back fires on none of the
1,018 questions of the other sets or the 18,777 of the training data.

What is left: the model still reaches for the tool it was told to avoid when it
knows no other way (`tail` without tail, `find` without find, `uname` without
uname: three tries, all the same). Asked a clear proxy question it knows the shell,
Git, pip and SSH forms, not APT's `Acquire::http::Proxy`, the system-wide
`/etc/environment`, Docker's daemon settings or `npm config set proxy`. Both need
training data, not rules. The strict execution checks changed no heldout result
(every failure there was already a wrong output); on exec_tasks they catch the
ruled-out programs that print the right answer.

### Retries told what was wrong (tried, not shipped)

A retry samples the same prompt again. The alternative tried: show the model its
answer and the objection as a second turn ("That command is not right: the request
says '.log'; ..."), greedy first, then sampled. Full GPU evaluation against 0.6.1:

| set | resample (0.6.1) | told what was wrong |
|---|---|---|
| regressions passed | 7 of 7 | 6 of 7 (`pip list` for installed packages) |
| everyday program / clean | 98.6% / 100% | 97.1% / 95.7% |
| tldr_heldout program / exact | 74.7% / 50.0% | 76.0% / 49.3% |
| nl2bash_test clean | 98.0% | 96.7% |
| multistep_heldout coverage | 90.9% | 91.4% |

The model was trained on single-turn prompts; given a second turn it wrote
placeholders (`systemctl status unit.service|timer|socket`) or dropped what the
objection named, where a fresh sample of the first prompt usually found a clean
command. Used only after both samples fail it would help little: 15 of about 1,100
steps still have an objection then, and the feedback turn fixed 1 of them (`git
rename-tag` -> `git tag --delete` and `git tag`). Retries stay as they are.

### v0.6.1: verbs checked against the manual

`systemctl inspect` passed the validator in 0.6.0: options were checked, but not the
command word after the program. For the programs that take a verb first (`systemctl`,
`loginctl`, `hostnamectl`, `timedatectl`, `localectl`, `resolvectl`, `networkctl`,
`busctl`) the index now keeps the verbs their man pages list under COMMANDS, plus the
older names they still accept (`systemctl force-reload`, `hostnamectl set-hostname`),
and any other word there is an objection: retried, and marked if it stays
(`unverified: systemctl: inspect is not a documented command`). It flags none of the
1,469 known-good commands that use these programs (tldr examples, evaluation
references, training data), and of all the 0.6.0 evaluation and execution answers
only that one; the rest are unchanged. Its retries did not find a better command
there: the split had already asked to "inspect" a directory. An index built before
0.6.1 has no verbs, and nothing is checked with it.

### v0.6.0: the runtime in Rust (same model, same rules)

The whole runtime was ported to Rust, one module at a time against 0.5 as the
reference; Python keeps training and evaluation and calls the same core.

- **The core** (split, prompts, retrieval ranking, validator with an exact port of
  Python's `shlex`, task checks and repairs, explanations, safety): 1,273,962 calls
  over every evaluation question and training reference, 0 differences from 0.5. All
  346 Python tests pass on it. Evaluation through it on the GPU changed 0 of 745
  answers, and in Docker 233 of 233 execution-task commands are the same.
- **The models** (llama.cpp through `llama-cpp-2`, set up and sampled the way
  llama-cpp-python 0.3.36 does): prompt tokens identical on all 622 prompts, encoder
  vectors within cosine 0.99988, and 310 of 311 answers byte-identical to 0.5 on the
  same CPU build. The release builds target AVX2 rather than the build machine's AVX-512,
  and 270 of 311 come out byte-identical; the rest differ the way two CPUs' rounding
  does, so they are judged on the evaluation below.

The program (`linu-x86_64-v3`, CPU, through `linu batch`) against 0.5 (GPU, measured
again for this comparison):

| set | 0.5 | 0.6 program |
|---|---|---|
| regressions passed | 7 of 7 | 7 of 7 |
| everyday / test_everyday program | 98.6% / 96.3% | 98.6% / 97.5% |
| tldr_heldout program / exact | 74.7% / 50.0% | 74.0% / 50.0% |
| nl2bash_test program / exact | 80.0% / 8.0% | 80.7% / 6.7% |
| multistep (10) coverage | 100% | 97.5% |
| multistep_heldout complete / coverage | 99.4% / 90.9% | 99.4% / 90.7% |
| multistep3_heldout complete | 94.4% | 94.3% |
| exec_tasks (33) / exec_heldout (200) | 93.9% / 75.5% | 93.9% / 75.0% |

Two sets fall by more than a point, each by one or two answers whose numbers came
out differently: the nl2bash exact matches lost are `alias m='...'` for `"..."` and
`find / -user admin` without `-print`, and the multistep loss is the systemd request,
where the answer now has an invented `systemctl inspect` (0.6.1 marks it) and
`journalctl --follow` in a pipeline. The execution loss is `uniq -d` for `uniq -u`.

- **Install**: one 9 MB program and the model files. Tested in fresh containers with
  no Python and no network: Debian 12 and Ubuntu 20.04 install and answer; CentOS 7
  (glibc 2.17) and Alpine (musl) are refused with a message. `linu-x86_64` answers on
  an emulated Nehalem CPU (SSE4.2, no AVX), where `linu-x86_64-v3` stops with an
  illegal instruction, as it should; `linu-aarch64` installs and answers under
  emulation.
- **Memory**: in a 2 GB container (no swap) all 10 multistep requests, 5 single
  questions and 5 more through `--serve`, no OOM; capped at 1.5 GB, the same with a
  container peak of 1,430 MB, page cache included. The process itself peaks at
  1,280 MB (0.5: 1,347 MB).
- **Time**: start-up is the model load either way (3.5 to 6 s cold); answers take what
  llama.cpp takes, the same as 0.5.

### v0.5.0: rules for round 4's real losses (same model)

Two of round 4's losses had one right fix, so they are rules, not training: the
package manager of this machine (`yum list installed` -> `dpkg -l`, `yum search` ->
`apt search`, any other foreign manager a slip that is retried) and `sudo` for
commands that always need root. Round 4 through the pipeline, before and after (GPU
evaluation on WSL Ubuntu as a normal user; execution sets in Docker as root, where
no `sudo` is added):

| set | before | after |
|---|---|---|
| regressions passed | 6 of 7 | **7 of 7** |
| everyday / test_everyday program | 97.1% / 95.0% | **98.6%** / 95.0% |
| tldr_heldout program / exact | 74.0% / 50.7% | 74.7% / 50.0% |
| nl2bash_test program / exact | 80.0% / 8.7% | 80.0% / 8.0% |
| multistep coverage, multistep_heldout / multistep3 complete | 100%, 99.4% / 94.4% | same |
| exec_tasks (33) / exec_heldout (200) | 93.9% / 75.5% | 93.9% / 75.5% (no task changed) |

The two exact-match losses are one case each, where the reference leaves out the
`sudo` a normal user needs (`systemctl restart unit1 unit2`, `mount --bind`).
Memory: 1,645 MB peak in a 2 GB container, 1,300 MB at 1.5 GB, no OOM; the server
and its clients 1,319 MB.

### Round 5 (a second RL run, not shipped)

Aimed at round 4's side effects. 1,200 more execution tasks on its weak spots
(`exec2`: windows of minutes and hours, only directories or files, counts, `cat` versus
`paste`; 1,118 proved, $0.10), and 3 of every 8 tasks per step taken from the
supervised training set and scored against their reference (right program 0.6, exact
form 0.4, less 0.2 when a needed `sudo` is dropped), with twice the KL weight (0.1),
200 steps from round 4. The held-out checks barely moved (execution 74% -> 76%,
right program on held-out ordinary questions 87% -> 88%), and the full evaluation
of the best checkpoint (step 40) matches round 4 instead of repairing it:

| set | round 4 | round 5 |
|---|---|---|
| exec_heldout (200) | 75.5% | 76.0% |
| exec_tasks (33) | 93.9% | 93.9% |
| everyday / test_everyday program | 97.1% / 95.0% | 97.1% / 95.0% |
| tldr_heldout program / exact | 74.0% / 50.7% | 72.0% / 50.7% |
| nl2bash_test program | 80.0% | 80.7% |
| multistep (10) coverage | 100% | 95% |
| regressions passed | 6 of 7 | 6 of 7 |

Round 4 stays installed. Most ordinary questions already earned full reward, so
they gave little signal, and the stronger KL held the model in place; recovering the
flag style or the `yum` regression would need examples aimed at those, not a broader
reward.

### Round 4 (reinforcement learning; the installed model since v0.4.0)

Reinforcement learning on executed commands. `generate_data.py exec` wrote 3,600
file and text tasks, each with a fixture, an expected output or check, and a
reference command ($0.27); `prove_exec_tasks.py` kept the 3,353 whose reference passes
and whose check fails `true` and an empty command; `prepare_rl.py` removed
near-duplicates and anything close to a test question, froze 200 as
`evalsets/exec_heldout.jsonl`, and kept 1,546 for training with the CLI's own prompts.
`train_grpo.py` (GRPO, LoRA on round 3, laptop GPU, 300 steps x 8 tasks x 6 samples,
about 1.7 h, 6.5 GB GPU, 4 GB RAM) rewards a reply 1 when its command does the job in
the sandbox (`exec_server.py`: Docker, no network, `nobody`), less 0.3 for an
unverified option or a dropped literal and 0.5 for unasked-for destruction, with a
KL penalty towards round 3. Held-out greedy success, raw (no repairs, no retries),
went 52% -> 78%.

Through the full pipeline (checks, retries, repairs), against round 3 measured again
with the same code:

| set | round 3 | round 4, step 200 | round 4, step 300 |
|---|---|---|---|
| exec_heldout (200), task done | 60.0% | 72.0% | **75.5%** |
| ... of which the 100 never used to pick a checkpoint | 65 | 71 | **75** |
| exec_tasks (33), task done | 87.9% | **100%** | 93.9% |
| everyday (70) program | 94.3% | 95.7% | **97.1%** |
| test_everyday (80) program | **96.2%** | **96.2%** | 95.0% |
| tldr_heldout (150) program / exact | **74.0% / 54.0%** | 72.7% / 51.3% | 74.0% / 50.7% |
| nl2bash_test (150) program / exact | **81.3% / 12.7%** | 81.3% / 8.7% | 80.0% / 8.7% |
| multistep (10) coverage / violations | 95% / 0 | 95% / 1 of 10 | **100% / 0** |
| multistep_heldout complete / coverage | 98.2% / 91.2% | 98.8% / 91.8% | **99.4%** / 90.9% |
| multistep3_heldout complete | 94.5% | 94.4% | 94.4% |
| regressions passed | 7 of 7 | 7 of 7 | 6 of 7 |

The rule set before training was: ship only if execution success rises by 3 points
and no other set falls by more than 1. Both checkpoints fail it. Step 300 was shipped
anyway, by the owner's decision: 15 more points of commands that do the job against
losses that are mostly style. Round 3 stays installed beside it
(`models/linu-round3-Q4_K_M.gguf`; point `model.gguf` at it to go back). Most of the
exact-match loss is style: the reinforced model
writes short flags (`tail -n 20`, `grep -i`, `cp -p`) where tldr's references spell
them out (`--lines 20`). Some changes are real losses: `find . -mtime 0` for "changed
in the last hour", `find . -type d` for "only the directories here", `sudo` dropped
from `fsck` and `apt update`, `yum list installed` for installed packages (step 300),
`chmod -R` on a single file (step 200). Some are real gains: `watch --interval 2`,
`setfacl -m u:appuser:r--` as the smallest permission change, the biggest files
rather than directories.

### Round 3

Round 2 dropped literals: a third of its training scenarios described paths and
counts in words while their commands used literal ones, so the model learned to
leave `.log` or `/var/log/myapp` out and guess. Round 3 adds four generation jobs
aimed at that: `scenarios3` (1,416 new multi-part requests where every literal
appears in the steps), `retask` (1,012 round-2 scenarios rewritten the same way),
`constraints` (1,727 single tasks built around a size, duration, count, port,
extension or path) and `fix` (877 corrected answers to tasks the reviewer rejected).
38,873 training examples, one epoch on a Colab T4 (2,430 steps, about 4 hours);
validation loss fell the whole way, to 0.235 (on round 3's validation set, so not
comparable with round 2's 0.290).

The 873 corrected answers were also tried as DPO pairs (`scripts/train_dpo.py`, on
the laptop GPU: 18 minutes, 5.2 GB peak). It worked on its own terms (the model
preferred the corrected answer on 79% of held-back pairs, up from a tie) but cost
tldr exact match 53% → 30% and multistep coverage 95% → 87.5%, so the shipped model
is the supervised one without it.

All three with checks and retries, retrieval on, GPU:

| set | round 2 | **round 3** | round 3 + DPO |
|---|---|---|---|
| everyday (70, dev) program | 94.3% | 92.9% | 92.9% |
| test_everyday (80) program | 95.0% | **96.2%** | 96.2% |
| tldr_heldout (150) program / exact | 74.0% / 52.0% | **74.0% / 53.3%** | 72.7% / 30.0% |
| nl2bash_test (150) program / exact | 77.3% / 11.3% | **81.3% / 12.7%** | 81.3% / 6.0% |
| multistep (10) coverage | 95.0% | **95.0%** | 87.5% |
| multistep_heldout (205) complete / coverage | 92.4% / 89.6% | **98.2% / 91.2%** | 98.2% / 91.1% |
| multistep3_heldout (73) complete / coverage | 81.5% / 92.7% | **94.5% / 94.3%** | 96.3% / 94.5% |
| regressions passed | 6 of 7 | 6 of 7 | 6 of 7 |

"Complete" is the share of answers that keep every literal of the request (size,
duration, extension, path, ...). `multistep3_heldout` was set aside from the
round-3 scenarios before training. The everyday difference is one question.

Read by hand, the 10 multistep requests come out about 5 right, 3 partly right and
2 wrong (round 2: 3, 4, 3). The old-`.log`-files delete now keeps `-name '*.log'`
on the first try. Still wrong: a `grep` on `/var/log` without `-r` and without the
24-hour limit, and the CSV request (wrong columns). Through the CLI, offline on the
CPU: peak memory 1.34 GB, 7-12 s per question.

#### Slip checks and repairs, and the first execution tests

The slip rules and repairs described under "How it works" were added after
reading round 3's failures (same model, GPU):

| set | before | after |
|---|---|---|
| everyday (70) program | 92.9% | 94.3% |
| regressions passed | 6 of 7 | **7 of 7** |
| every other set | | unchanged on the main metrics |

What changed, case by case: `grep -r` for the `/var/log` search, the 3 am schedule
installed with `crontab` instead of printed as a crontab line, `apt list
--upgradable` instead of `pip list --outdated`. Not all good: two `pip list`
answers became `rpm --query --list --all`, which lists every package's files.
The `$HOME` check adds a flag to four guessed paths and fixes none of them.

Execution tests (`exec_eval.py`, 33 file and text tasks, each command run as
`nobody` in a Docker container with no network; HOME is an empty directory, so a
guessed `$HOME/data` for "the data directory" fails), same pipeline and rules:

| model | commands that did the job |
|---|---|
| stock Qwen2.5-Coder-1.5B | 63.6% (21 of 33) |
| round 2 | 84.8% (28) |
| **round 3** | **87.9% (29)** |

Round 3 still fails: two `$HOME/...` guesses (flagged, but no retry did better),
`wc -l < <(find docs)` (counts the directory itself), and `paste` where `cat` was
needed. Before the head/tail, `path/to/` and relative-`grep` repairs, round 3
passed 27 of the same 33 under the looser harness.

On a 2 GB machine: the CLI in a container capped at 2 GB of memory with no swap,
CPU only, answered all 10 multistep requests and 5 single questions with no
out-of-memory kill; the container's peak, page cache of the model included, was
1,707 MB. Capped at 1.5 GB it still ran (peak 1,287 MB). 7-24 s per answer.

### Round 2

Qwen2.5-Coder-1.5B with LoRA on 33,167 examples, trained on a Colab T4 (fp16,
batch 16). Planned for two epochs; stopped at step 1,900 of 4,146 because the
validation loss stopped improving once the second epoch began (0.290 at 1,900,
0.292-0.297 up to 2,400 while training loss kept falling). Q4_K_M, 986 MB. All
three models go through the same round-2 pipeline (split, retrieve, answer,
validate), with retrieval on. "Program" is the share of answers using the right
program; "coverage" the share of a request's parts handled with an accepted program.

| set | stock | round 1 | **round 2** |
|---|---|---|---|
| everyday (70, dev) program | 84.3% | 94.3% | **95.7%** |
| test_everyday (80, unseen) program | 91.2% | 92.5% | **95.0%** |
| tldr_heldout (150) program / exact | 68.7% / 26.0% | 68.7% | **74.0% / 52.7%** |
| nl2bash_test (150) program / exact | 71.3% / 2.7% | **85.3%** | 76.7% / 11.3% |
| multistep (the 10 hand-written requests) coverage | 80.8% | 62.5% | **95.0%** |
| multistep_heldout (205) coverage / step count off by | 70.9% / 0.65 | ~55%* | **89.6% / 0.05** |
| regressions passed | 5 of 7 | | **6 of 7** |

\* round 1 was stopped after 49 of the 205; it cannot split, so most of its answers
are one command. The no-retrieval runs have not been run; for the execution tests
see round 3.

Round 1 is better on NL2Bash: it was trained on 14,578 mostly-NL2Bash pairs, round 2
on at most 4,000 of them (macOS forms removed) next to generated questions phrased
the way people ask. Peak memory 1.2-1.5 GB; 4.3 s per question on the laptop CPU,
0.7 s on its GPU, with the same answers to within a couple of cases.

With the task checks, retries and the `head -n 1` repair (run `new-rag-checks`,
same model, GPU):

| set | valid without → with | right program without → with | steps retried |
|---|---|---|---|
| everyday | 98.6% → 100% | 95.7% → 94.3% | 1.4% |
| test_everyday | 97.5% → 100% | 95.0% → 95.0% | 2.5% |
| tldr_heldout | 93.7% → 95.7% | 74.0% → 74.0% | 7.0% |
| nl2bash_test | 98.7% → 99.3% | 76.7% → 77.3% | 2.7% |
| multistep_heldout | 99.6% → 100% | coverage 89.6% → 89.6% | 0.6% |

Retries swap invented options for documented ones (a `ps` report for a nonexistent
`sysstat --processes`), which is what "valid" measures; whether the replacement does
the job is another matter (`kill -0 PID` replaced an invalid `kill -TERM -p PID`, and
signal 0 stops nothing). 7.6% of the held-out multi-step answers lose a literal of the
request in every command and get a `note:`. Most of those come from the split: the
training scenarios often name a path or count in words ("the compressed nginx log"),
the model learned to do the same, and its steps then guess paths.

Read by hand, the 10 multistep requests come out about 3 right, 4 partly right and
3 wrong. Coverage counts the right programs, not right commands, so it reads higher
than that. Mistakes seen: `ps ... | head -n 1` (shows only the header), a dropped
`-name '*.log'` that widens a delete to every old file, a backup split into four
separate `tar` runs, and an invented `systemd-env` that the validator flagged.

### Round 1

Qwen2.5-Coder-1.5B, stock against fine-tuned (LoRA, one epoch, 14,578 examples),
Q4_K_M, CPU, answering with a command plus a model-written sentence. "Command" is
the share of answers using the right program; "exact" is the share matching the
reference up to whitespace.

| | everyday (70) command | tldr held-out (150) command / exact | NL2Bash held-out (100) command / exact |
|---|---|---|---|
| stock, no retrieval | 95.7% | 44.7% / 1.3% | 57.0% / 9.0% |
| stock + retrieval | 84.3% | 73.3% / 37.3% | 67.0% / 7.0% |
| fine-tuned, no retrieval | 91.4% | 50.0% / 6.7% | 79.0% / 13.0% |
| **fine-tuned + retrieval** | **91.4%** | **76.0% / 64.0%** | **78.0% / 13.0%** |

Peak memory for the whole pipeline was 1.4 GB (Q4_K_M).

What round 1 got wrong, and what was changed because of it:

| failure | cause | change |
|---|---|---|
| macOS command for a Linux question (`top -l 1 -s 10`) | NL2Bash contains macOS commands | validator; NL2Bash filtered by it |
| explanation repeated the question, or was wrong | 96% of NL2Bash training explanations were the question restated | model writes no prose; explanations come from the documentation |
| one command for a four-part request | every training answer was a single line | requests are split and answered step by step |
| 3 of 100 long prompts looped | no repeat penalty or loop check | both, with one retry |
| numbers hard to trust | dev set reported as a test; exact match as the main metric | frozen test sets; separate metrics; execution tests |

