# Usage

```sh
lxask "what you want to do"
```

Say what you want, the way you would ask a colleague. Name the files, sizes, ages
and ports you mean: lxask checks that they appear in the command.

## Reading an answer

```
$ lxask "delete all .tmp files in this folder"
$ find . -name '*.tmp' -exec rm {} \;
  find  Find files or directories under a directory tree, recursively
    -name  Base of file name (the path with the leading directories removed) matches shell pattern pattern
    -exec  Execute command; true if 0 status is returned
  rm    Remove files or directories
  warning: deletes every file the find expression matches
```

- The line starting with `$` is the command. lxask never runs it.
- Below it, each program and option, as the documentation describes it. Where the
  documentation says nothing, nothing is printed: an explanation can be incomplete,
  but it is not invented.
- `warning:` marks commands that delete, overwrite or reformat.
- `unverified:` marks what lxask could not confirm. Treat that command with more care:

```
$ top -b -n 1 -l 1 -o %MEM | grep -v top
  unverified: top: -l is not a documented option
  closest documented example: ps --sort size
```

Common `unverified` lines:

| line | means |
|---|---|
| `top: -l is not a documented option` | the option is not in the program's manual |
| `systemctl: inspect is not a documented command` | no such subcommand in the manual |
| `foo: not a known Linux command` | no manual or tldr page knows the program |
| `pbcopy: not a Linux command` | a macOS or BSD command |
| `the request says '.log'; the command does not have it` | a detail you named is missing |
| `the request says without lsof; the command uses it` | it used a program you ruled out |

lxask has already tried up to three answers before showing one with such a line.

## Requests with several parts

```
$ lxask "find the busiest process, see what files it has open, and stop it gracefully"
1. Find the process with the highest reported CPU usage
   $ ps -eo pid,comm,%cpu --sort=-%cpu | head -n 2
   ...
2. List the files open by the selected process
   $ lsof -p PID
   ...
3. Send SIGTERM to the selected process
   $ kill -TERM PID
```

Each part becomes its own step, answered on its own. Words in capitals like `PID`
are for you to fill in from the step before. A `note:` at the end says if something
you asked for is in none of the commands.

## Ruling a program out

Say "without X", "don't use X", "instead of X" or "X isn't installed":

```
$ lxask "which process listens on port 8080, without lsof"
$ ss -ltnp 'sport = :8080'
```

These requests, and requests about proxies, are answered with a version of the
model trained for them ([how it works](how-it-works.md#restrictions)).

## When lxask asks back

A request too vague for one command gets a question instead of a guess, and exit
status 3:

```
$ lxask "add a proxy"
"add a proxy" can mean several things. Which one? Ask again naming it, e.g.:
  this shell session  lxask "set an http proxy for this shell session"
  the whole system    lxask "set an http proxy system-wide for all users"
  Git                 lxask "set a proxy for git"
  APT                 lxask "set a proxy for apt"
  ...
```

So far this covers proxies only.

## Straight into your prompt (Ctrl+G)

Add one line to your shell's startup file:

```sh
eval "$(lxask --shell-init bash)"     # in ~/.bashrc
eval "$(lxask --shell-init zsh)"      # or in ~/.zshrc
```

Then type what you want to do at the prompt, as plain words, and press **Ctrl+G**:

```
$ count the lines in data.txt          <- you type this, then Ctrl+G
$ wc -l data.txt                       <- the line is now the command
```

The command waits on your prompt, to edit or run with Enter; nothing runs by itself.
What lxask says about it (`unverified:`, `warning:`) is printed above the prompt. A
request with several steps becomes one line joined by `&&`. When lxask asks back or
answers about itself, your line stays as you typed it.

With `lxask --serve` running, Ctrl+G answers in about 2 seconds. To use another key,
bind the widget yourself: `bind -x '"\C-x\C-l": __lxask_widget'` (bash) or
`bindkey '^X^L' __lxask_widget` (zsh).

## Keep the model loaded

Most of an answer's time on a CPU goes into loading the model. The installer asks
whether to keep lxask loaded between questions (recommended, and the default):

- **On (`serve = auto`):** your first question starts lxask in the background and is
  answered by it; later questions take about 2 s instead of about 7
  ([accuracy.md](accuracy.md#speed)). It holds about 1.3 GB of memory while loaded and
  stops itself after 30 minutes without a question, so the next question starts it
  again.
- **Off (`serve = off`):** every question loads the model and frees it after.

Change it in `~/.local/share/lxask/settings`, or for one shell with `LXASK_SERVE=auto`
or `off`. Only one copy is ever started: questions asked while it is starting wait
for it.

By hand, whatever the setting:

```sh
lxask --serve &        # loads once, then waits; stops after 30 minutes without a question
lxask "show disk usage of each folder here"
lxask --stop
```

Every `lxask` call uses a running server when it can, and answers the same way. It
listens on a socket only your user can open (`$XDG_RUNTIME_DIR/lxask/`, else
`~/.local/share/lxask/run/`). `--idle MINUTES` changes the timeout (0: never stop).

To start it with your session, a systemd user unit:

```ini
# ~/.config/systemd/user/lxask.service, then: systemctl --user enable --now lxask
[Service]
ExecStart=%h/.local/bin/lxask --serve --idle 0
```

## When an answer is wrong

```sh
lxask --bad                                       # the last answer was wrong
lxask --bad "should list directories only: ls -d */"   # with a note (quote * and ?)
```

The last answer is kept in `~/.local/share/lxask/last.json`; `--bad` adds it, with
your note, to `feedback.jsonl` beside it. Nothing leaves your machine by itself.

To send reports to lxask's developers:

```sh
lxask --share          # the last report
lxask --share 3        # the last three (up to 10)
```

This prints exactly what would be sent (your question, lxask's commands, your note,
the lxask version and model file) and a link to a new GitHub issue filled in with it.
Open the link, remove anything private (the issue is public), and submit. lxask itself
sends nothing; a GitHub account is needed to submit.

Without a link: open an issue with the "Bad answer" template on
[GitHub](https://github.com/arxhr007/lxask/issues/new/choose).

## Options

| option | does |
|---|---|
| `--no-explain` | print only the commands |
| `--command-only` | print only the command line (steps joined by `&&`); notes on stderr |
| `--shell-init bash\|zsh` | print the Ctrl+G key binding, above |
| `--no-retry` | keep the first answer even if the checks object (faster) |
| `--no-rag` | answer without the documentation notes |
| `--show-context` | print the notes the model was given, to stderr |
| `--threads N` | CPU threads (default: half the logical cores) |
| `--model FILE` | another model (answers without the adapter) |
| `--index FILE` | another documentation index |
| `--serve`, `--idle MIN`, `--stop` | the server, above |
| `--bad [NOTE]` | report the last answer |
| `--share [N]` | the last N reports and a link to send them as a GitHub issue |
| `--about` | who made lxask, and its links |
| `--version`, `--help` | |

## About lxask

```
$ lxask "who made you?"
lxask 1.0.0: an offline Linux command assistant.
Made by Aaron Thomas (arxhr007).

  project  https://github.com/arxhr007/lxask
  GitHub   https://github.com/arxhr007
  website  https://arxhr007.github.io
...
```

`lxask --about` prints the same. Questions about lxask itself ("who made you", "who
created lxask", "what are you") get this answer instead of a command, without the
model; a Linux question that only sounds similar ("who created this file") is
answered as usual.

## Exit status

| status | means |
|---|---|
| 0 | an answer was printed (check it for `unverified:` lines) |
| 1 | an error, said on stderr |
| 2 | a usage error, or no model found |
| 3 | lxask asked back instead of answering |

## In scripts

```sh
cmd=$(lxask --command-only "count the lines in data.txt")
echo "$cmd"      # read it before you run it
```
