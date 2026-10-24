# How it works

The same flow as diagrams, with the components and the build: [architecture.md](architecture.md).

```
question -> split into tasks -> for each task: retrieve -> answer -> check
                                (another try if a check objects) -> explain -> warn
```

Everything runs locally: a 1.5B-parameter language model and a small encoder through
llama.cpp, and a SQLite index of documentation. The program is written in Rust.

## 1. Split

A request with several parts ("find the busiest process, see its open files, stop
it") is turned into separate tasks by one model call. Short single questions skip
this. Each part is searched and answered on its own, because a long request searched
as a whole brings back notes about the wrong commands.

If the split loses a detail that decides which files are meant (an extension, a
size, an age), it is tried again.

## 2. Retrieve

Each task is embedded with bge-small (37 MB) and matched against about 38,000
entries: the examples and summaries of [tldr-pages](https://github.com/tldr-pages/tldr),
plus a summary for each command only a man page documents.
The search first picks the four commands the task is most likely about, favouring
common ones over obscure tools unless a tool is named, then takes their closest
examples. These notes go into the model's prompt. Without the encoder, the search
falls back to keywords (BM25).

## 3. Answer

A fine-tuned Qwen2.5-Coder-1.5B, quantized to 4 bits, writes one command line for
the task, and nothing else.

## 4. Check

The command is checked without running it:

- **Against the documentation.** The program exists, every option is documented for
  it, the subcommand after `systemctl` and its kin is real, the line parses as
  shell, and it is not a macOS or BSD form (`top -l`, `sed -i ''`, `pbcopy`).
- **Against the task.** The paths, extensions, sizes, ages, counts and ports the task
  names must appear in the command, in any form a command can write them: 30 days
  is `-mtime +30`, `-mmin +43200` or `--since '30 days ago'`.
- **Against known slips.** `grep` on a directory without `-r`, a reboot for "shut
  down", `pip list` for the system's packages, `$HOME/...` when the request never
  mentioned home, a command that deletes for a request that only asks to look.

When a check objects, or no command came back, the model gets up to two more tries
(sampled, so they differ), and the answer with the fewest objections is kept. What
still fails is shown as `unverified:`.

Slips with exactly one right fix are repaired instead: `head -n 1` after `ps` (that
is only the header) becomes `head -n 2`; `grep` on a directory gets `-r`; a bare
crontab line is installed with `crontab`; a command that always needs root gets
`sudo`; another distribution's package listing or search becomes this machine's.

### Restrictions

"Without lsof", "don't use sudo", "instead of curl", "netstat isn't installed": a
command that runs a ruled-out program is an objection, judged on what it actually
runs (`sudo lsof`, `xargs grep`, `find -exec rm`), not on the words ("without
deleting" rules out nothing). Documentation notes about that program are kept out of
the prompt.

Such requests, and requests about proxies, are answered with a small LoRA adapter
(37 MB) applied on top of the model. It was trained on these requests and keeps
restrictions much better (80% against 60%), but it is worse on everyday questions,
so every other request gets the model alone. `LXASK_ADAPTER=off` turns it off.

### Asking back

Some requests mean different commands in different contexts. For those ("add a
proxy" with no word on where), lxask asks which one is meant instead of guessing.

## 5. Explain

Each program and option in the command is looked up in the tldr summaries and the
man pages. Where the documentation says nothing, nothing is printed.

## 6. Warn

Commands that delete, overwrite or reformat (`rm -r`, `find -delete`, `dd` to a
device, `mkfs`, `shred`, `git reset --hard`) carry a `warning:` saying what kind of
command it is. The
warning does not say whether the command's filter is right; you check that.

## Where the model came from

The base model was fine-tuned on about 39,000 examples, from tldr-pages, NL2Bash
and generated requests checked by the same validator. It then had a round of
reinforcement learning on commands that were actually run in a sandbox, rewarded
when they produced the right result. [results.md](results.md) has every training
round, including the ones that were not shipped.
