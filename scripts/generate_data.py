"""Generate training questions and commands with a language model API.

    python scripts/generate_data.py tasks --limit 50        # trial
    python scripts/generate_data.py scenarios --limit 50
    python scripts/generate_data.py tasks                   # full run, resumes where it stopped
    python scripts/generate_data.py status

Jobs:
  tasks      one request per common command: its tldr page and man options go in,
             natural questions with the exact command come out.
  depth      more of the same for the most asked-about commands, each request told
             what was already asked.
  everyday   everyday needs phrased without naming the tool.
  scenarios  multi-part requests ("server is slow, find X, check Y, stop Z"), each
             split into self-contained steps with one command per step.
  long       scenarios with 4 to 6 parts.
  review     the stronger model judges the kept single tasks the small model wrote:
             does the command do what the question asks? Run it after
             filter_generated.py, then run filter_generated.py again to apply it.

Round 3, aimed at mistakes the round-2 model makes:
  fix        the right command for each task the reviewer judged wrong; the pairs
             (wrong, right) are also preference-training data.
  scenarios3 multi-part requests whose tasks repeat every exact value the commands
             need, and where one job that one command does stays one step.
  constraints  single requests with two to four conditions (pattern, size, age,
             place, owner, count ...), every one of which the command must keep.
  retask     rewrites the tasks of earlier scenarios that name a path or a file in
             words ("the application log") while the command uses the real one.

Round 4, for reinforcement learning on executed commands:
  exec       file and text tasks with a fixture (setup lines), an expected output or
             a check, and a reference command; scripts/prove_exec_tasks.py runs them
             in a container and keeps only those the reference passes and `true` fails.
  exec2      the same, aimed at what the round-4 model still gets wrong (hours and
             minutes, only directories or files, counts, cat versus paste, ...).

Round 6, aimed at what the v0.7 rules found and cannot fix:
  forbid     single requests that rule a program out ("without tail", "lsof isn't
             installed") with a command that does the job without it. A few
             programs are kept out of training as a held-out test of the habit.
  forbid_scenarios  multi-part requests with such a restriction, whose tasks carry it.
  contexts   proxy settings for a named context (APT, system-wide, Docker's daemon,
             npm, ...) and firewall rules for a named firewall (ufw, firewalld).
  forbid_twins  each kept `forbid` request without its restriction, with the usual
             command: without them the model learns to avoid the usual program always.

Nothing written here is trusted yet: scripts/filter_generated.py checks every
command against the documentation and keeps only what passes.

The key is read from LXASK_GEN_API_KEY, else from ~/.config/lxask/gen_api_key. It is
never printed or written anywhere. The endpoint is LXASK_GEN_API_URL, the model is
--model or LXASK_GEN_MODEL, the prices are --price-in/--price-out; none of the three
is guessed. Spending is tracked in data/generated/ledger.json
across jobs and reruns, and a run stops before it can pass --cap.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import sources
from lxask import constraints
from lxask.index import man_option_titles, program, strip_placeholders
from lxask.validate import NOT_LINUX_PROGRAMS

OUT = Path(__file__).resolve().parents[1] / "data" / "generated"
# The endpoint that speaks the /responses protocol, and the model to write with:
# both come from the caller (env or flags), nothing is guessed -- so no price table
# goes stale here either: prices are passed too, and the spend cap uses them.
API_URL = os.environ.get("LXASK_GEN_API_URL", "").strip()
KEY_FILE = Path.home() / ".config" / "lxask" / "gen_api_key"
# The small model reasons far longer than these jobs need; "low" is three times faster and cheaper.
DEFAULT_EFFORT = {"tasks": "low", "depth": "low", "retask": "low", "exec": "low", "exec2": "low"}

TASKS_PER_PAGE = 8
TASKS_PER_COMMON_PAGE = 16  # for the commands people ask about most
COMMON_USAGE = 50
SCENARIOS_PER_REQUEST = 4
SCENARIO_REQUESTS = 750
DEPTH_PROGRAMS = 120  # the most asked-about commands get more questions ...
DEPTH_ROUNDS = 2  # ... this many extra requests of TASKS_PER_COMMON_PAGE each
EVERYDAY_REQUESTS = 200
EVERYDAY_PER_REQUEST = 12
LONG_REQUESTS = 250
REVIEW_BATCH = 10
REVIEWED_SOURCES = ("tasks", "depth", "forbid", "contexts")  # the small model's, and round 6's new kinds: the stronger one checks them
FIX_BATCH = 10
SCENARIOS3_REQUESTS = 400
CONSTRAINT_REQUESTS = 150
CONSTRAINTS_PER_REQUEST = 12
RETASK_BATCH = 8
# Shell grammar words that tldr documents as pages. Asked to write tasks "for `done`",
# the model answers ordinary questions with loops ("count the lines in a file" -> a
# while loop instead of wc), which is the wrong thing to teach.
SKIP_PAGES = frozenset("do done then fi else elif esac in { } [[ ]] ! time function select coproc".split())
MAX_OPTION_CHARS = 2500
# The models think before answering, and those hidden tokens count against
# max_output_tokens (and are billed as output). Too small a limit cuts the JSON off.
REASONING_ALLOWANCE = 4000

TASKS_SYSTEM = """You write training data for an offline Linux command-line assistant.

You are given the documentation of one command. Write {count} different things a user might ask that are answered by one command line built on this command. Piping into other standard tools is fine where a real user would.

Questions:
- Sound like real users: some "how do i ...", some bare requests, some first person. Lower-case and informal is fine.
- Do not name the command unless people normally do when asking (git, docker, apt, systemctl and similar).
- About half must contain concrete values (file names, paths, numbers, host names, user names) and the command must use exactly those values.
- The rest are general; their commands use plain placeholders such as path/to/file or username.
- Cover different uses of the command and a range of difficulty. Do not write two questions for the same use.
- Only ask things for which this command is the natural tool. Never use it for a job another standard command does more simply (counting lines is wc, not a loop).
- The question must leave no doubt about what is wanted. "Reverse the order of the lines" and "sort in descending order" are different requests; say which.

Commands:
- Linux only (GNU coreutils, util-linux, procps). Never macOS or BSD forms.
- Use only options that appear in the documentation given. If you are not sure an option exists, do not use it.
- One line each. No comments, no explanation, no sudo unless the task cannot work without it."""

SCENARIOS_SYSTEM = """You write training data for an offline Linux command-line assistant that answers multi-part requests by splitting them into steps.

Write {count} requests in the area given. Each is what a real user would type when they need help: usually a short situation followed by several things they want done. Vary how many: across your requests include some with 2 things, some with 3, and some with 4 or 5. About one request in three asks for a single thing only.

For each request give its steps:
- "task": one thing to do, written as a short instruction that makes sense on its own. Never refer to earlier steps with "it", "that process" or "those files"; say what is meant ("list the files a process has open").
- "command": one Linux command line for that task. Where a value comes from an earlier step, use an upper-case placeholder such as PID or FILE.
- Steps are in the order a careful administrator would run them: look first, change last.
- Every step must be something a command can do. Leave out parts of the request that are advice or judgement.
- Nothing destructive unless the request asks for it, and then a step that shows what would be affected comes first.

Requests may include constraints ("without installing anything", "don't delete anything yet") and may use pronouns; the steps must still stand alone.

Commands: Linux only (GNU coreutils, util-linux, procps, systemd), never macOS or BSD forms, only options you are sure exist, one line each, no sudo unless needed. Prefer the commands suggested with the area, and use others where they fit better."""

EVERYDAY_SYSTEM = """You write training data for an offline Linux command-line assistant.

Write {count} things an ordinary Linux user asks in the area given, each answered by one command line. These are everyday needs, asked by people who do not know which tool does the job.

Questions:
- Never name the command or tool. Describe the need ("what is eating my disk space", "see who is logged in").
- Sound like real users: some "how do i ...", some bare requests, some first person, some terse like a search query. Lower-case and informal is fine.
- About half must contain concrete values (file names, paths, numbers, host names, user names) and the command must use exactly those values. The rest are general; their commands use plain placeholders such as path/to/file or username.
- Each question has one clear meaning and one standard answer. A different need each time.

Commands:
- The standard tool a competent administrator reaches for first, one that a stock Debian or Ubuntu system has where such a tool exists.
- Linux only (GNU coreutils, util-linux, procps, systemd). Never macOS or BSD forms.
- Only options you are sure exist. One line each. No comments, no sudo unless the task cannot work without it.
- Prefer the commands suggested with the area, and use others where they fit better."""

REVIEW_SYSTEM = """You check training data for a Linux command-line assistant. Each item is a question and the command given as its answer.

Judge every item:
- "correct": on a normal Linux system the command does what the question asks. Other equally good commands may exist; that does not matter. Placeholders such as path/to/file, username or PID are fine.
- "wrong": the command does something materially different from what was asked, misses a stated requirement, relies on an option that does not do what the answer needs, would fail as written, or the question is too vague for this to be its answer.

Be strict about meaning and lenient about style. Give a reason of at most 15 words for each "wrong", and an empty reason for each "correct"."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["correct", "wrong"]},
                    "reason": {"type": "string"},
                },
                "required": ["n", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

TASKS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "command": {"type": "string"}},
                "required": ["question", "command"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SCENARIOS_SCHEMA = {
    "type": "object",
    "properties": {
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "request": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"task": {"type": "string"}, "command": {"type": "string"}},
                            "required": ["task", "command"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["request", "steps"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scenarios"],
    "additionalProperties": False,
}

LONG_SYSTEM = SCENARIOS_SYSTEM.replace(
    "Vary how many: across your requests include some with 2 things, some with 3, and some with 4 or 5. About one request in three asks for a single thing only.",
    "Every request asks for 4 to 6 things.",
)
assert LONG_SYSTEM != SCENARIOS_SYSTEM

SCENARIOS3_SYSTEM = """You write training data for an offline Linux command-line assistant that answers multi-part requests by splitting them into steps. A small model learns from this data to write the steps and then a command for each step without seeing the request again, so each step must carry everything its command needs.

Write {count} requests in the area given. Each is what a real user would type: usually a short situation followed by what they want done. Across your requests vary the length: one request with a single thing, one or two with 2 or 3 things, one with 4 to 6. Many requests should put several conditions on the same files or entries (a name pattern and a size and an age and a place; a service and a time window and a priority).

For each request give its steps:
- "task": one thing to do, a short instruction that makes sense on its own. It repeats, exactly as the request gives them, every value its command needs: paths, file names, patterns such as *.log, sizes, ages, counts, ports, user names, service names, host names. Never replace a value with a description: write "/var/log/myapp", not "the log directory"; "files ending in .log", not "the log files"; "the 20 most frequent", not "the most frequent". Never refer to earlier steps with "it", "that process" or "those files".
- "command": one Linux command line for that task, using exactly those values and nothing the task does not say. Where a value can only come from an earlier step, use an upper-case placeholder such as PID or FILE.
- One step for each command a careful administrator would actually run. When one command line does the whole job (one archive with several exclusions, one find with several conditions), that is one step; never split one job into several commands.
- Steps are in the order a careful administrator would run them: look first, change last. Leave out parts of the request that are advice or judgement.
- Nothing destructive unless the request asks for it, and then a step that shows what would be affected comes first, with the same conditions.

Commands: Linux only (GNU coreutils, util-linux, procps, systemd), never macOS or BSD forms, only options you are sure exist, one line each, no sudo unless needed. Prefer the commands suggested with the area, and use others where they fit better."""

CONSTRAINTS_SYSTEM = """You write training data for an offline Linux command-line assistant.

Write {count} requests in the area given, each answered by one command line, and each with two to four conditions the command must meet: for example a name pattern, a size, an age or time window, a place, an owner or permission, a number of results, a port, a user, a priority. Real users phrase these loosely ("big log files from the last week in /srv/app", "the 5 biggest .mp4 files under ~/Videos"); write them that way, varied, some as questions and some as bare requests.

- Every condition uses a concrete value, and the command must use exactly those values and keep every condition. A command that drops or changes one is wrong.
- Mostly do not name the tool. One clear meaning per request; a different need each time.
- Commands: the standard tool a competent administrator reaches for first, on a stock Debian or Ubuntu system. Linux only (GNU coreutils, util-linux, procps, systemd), never macOS or BSD forms, only options you are sure exist, one line each, no sudo unless needed."""

FIX_SYSTEM = """You correct training data for a Linux command-line assistant. Each item is a question, a wrong command given as its answer, and a reviewer's note on what is wrong.

For each item write the correct command: one Linux command line (GNU coreutils, util-linux, procps, systemd; never macOS or BSD forms) that does exactly what the question asks, keeping every value the question gives, using only options you are sure exist. Placeholders such as path/to/file, username or PID are fine where the question gives no value. The reviewer can be wrong: if the given command was in fact right, give it again unchanged. If the question is too vague to have one right command, set "skip" to true and leave "command" empty."""

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "skip": {"type": "boolean"}, "command": {"type": "string"}},
                "required": ["n", "skip", "command"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fixes"],
    "additionalProperties": False,
}

RETASK_SYSTEM = """You edit training data for a Linux command-line assistant. Each item is a user's request and its steps, each a task and a command. A model learns to write the commands from the tasks alone, so a task must state every exact value its command uses.

Rewrite each task so that it states, exactly as written in its command or the request, every path, file name, pattern, size, age, count, port, user name and service name the command uses. Keep the rest of its wording, keep it short, keep it standing on its own (no "it" or "that file"). Do not change the commands. Placeholders in upper case (PID, FILE) stay placeholders and need no value. Return the tasks of every item, in order, as many as it has."""

RETASK_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "tasks": {"type": "array", "items": {"type": "string"}}},
                "required": ["n", "tasks"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

EXEC_SYSTEM = """You write tasks for testing a Linux command-line assistant by running its answers.

Write {count} tasks in the area given. Each task is checked automatically:
1. An empty directory is created and the "setup" lines run in it with bash, as an unprivileged user, with no network. They create the files the task is about (printf, echo, seq, mkdir, touch, head -c ... /dev/zero, ln, tar, gzip, chmod). Ages are set with touch -d (for example touch -d '10 days ago' old.log).
2. The assistant's command runs in that directory, as the same user.
3. It passes when its output matches "expect" and, if given, the shell test in "check" (run afterwards in the same directory) exits 0.

Output comparison: each output line is trimmed, runs of spaces become one, a leading ./ is dropped, empty lines are ignored. expect_kind is one of:
- "lines": the output must be exactly expect_values, in order.
- "set": the output lines must be exactly expect_values, in any order.
- "number": the output must contain the number in expect_values[0] (so "3" and "3 data.txt" both pass).
- "contains": every line in expect_values must appear in the output; no line in "absent" may.
- "none": the output is not checked; "check" decides (for tasks that create, change, move or delete files).

Rules:
- The question sounds like a real user: informal, varied, mostly without naming the tool. It names the files and directories by the relative names setup created ("app.log", "the logs directory", "everything under src"), never absolute paths, never the home directory.
- One clear meaning. The expected result must be what any correct command gives, not what one particular command prints: no ls -l listings, no exact column spacing, no sizes in human units, no order unless the question asks for one. Prefer "number", "set" or "contains" when several outputs are equally right, and "check" for changes to files.
- "check" tests the result and that nothing else was harmed (for example, the files that should stay still exist). Use test, cmp, grep -q, [ "$(cat f)" = "..." ], stat -c %a. Empty string if not needed.
- Setup makes the task meaningful: files that should match and files that should not (a .txt next to the .log files, a small file next to the big one, a recent file next to the old one).
- "reference" is one command line, using only standard GNU tools (coreutils, findutils, grep, sed, gawk, tar, gzip, bzip2, xz, zip, unzip, diff), that passes. No sudo, no network, no interactive programs.
- Different tasks each time, a range of difficulty: some one-word commands, some pipelines with two or three conditions."""

EXEC_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "q": {"type": "string"},
                    "setup": {"type": "array", "items": {"type": "string"}},
                    "expect_kind": {"type": "string", "enum": ["lines", "set", "number", "contains", "none"]},
                    "expect_values": {"type": "array", "items": {"type": "string"}},
                    "absent": {"type": "array", "items": {"type": "string"}},
                    "check": {"type": "string"},
                    "reference": {"type": "string"},
                },
                "required": ["q", "setup", "expect_kind", "expect_values", "absent", "check", "reference"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tasks"],
    "additionalProperties": False,
}

FORBID_SYSTEM = """You write training data for an offline Linux command-line assistant.

The user rules out one program: `{program}`. Write {count} different requests for which `{program}` would be the natural tool but the request says not to use it, and give for each the one command line that does the job without it.

Requests:
- Say it in varied ways, as real users do: "without {program}", "don't use {program}", "{program} isn't installed", "I can't use {program} here", "instead of {program}", "no {program}", "use X rather than {program}", "avoid {program}". Put the restriction at the start, in the middle or at the end.
- A different job each time, among those `{program}` is usually used for. About half with concrete values (file names, paths, ports, hosts, user names) that the command must use exactly; the rest general, with placeholders such as path/to/file.
- Sound like real users: informal, some questions, some bare requests. Keep them short (most under 15 words) and plain; no technical caveats (locale, encoding, edge cases) a user would not think to add.

Commands:
- As short and plain as the job allows.
- Never run `{program}`: not directly, not through sudo, xargs, find -exec, sh -c, $(...) or an alias, and not a renamed copy of it (no egrep for grep, no vi for vim).
- The way a competent administrator does this job without `{program}`, with tools a stock Debian or Ubuntu system has. Prefer the simplest widely known alternative.
- It must do the same job and give the result the user wanted from `{program}`, keeping every value and condition of the request.
- Linux only (GNU coreutils, util-linux, procps, iproute2, systemd), never macOS or BSD forms, only options you are sure exist, one line, no sudo unless the job needs it.
- If a job has no reasonable way to be done without `{program}`, do not write a request for it."""

FORBID_SCENARIOS_SYSTEM = """You write training data for an offline Linux command-line assistant that answers multi-part requests by splitting them into steps. A small model learns from this data to write the steps and then a command for each step without seeing the request again, so each step must carry everything its command needs, restrictions included.

Write {count} requests in the area given. Each is what a real user would type: a short situation and two to five things they want done, with the programs given ruled out ("without lsof", "netstat isn't installed", "don't use sudo, I'm root", "no curl for any of it"). Vary it: in some requests the restriction covers the whole request, in others one part.

For each request give its steps:
- "task": one thing to do, a short instruction that makes sense on its own. It repeats, exactly as the request gives them, every value its command needs (paths, file names, patterns, sizes, ages, counts, ports, user names, service names, hosts). Where a ruled-out program would be the natural tool for the step, the task says so in the request's words ("count the lines in app.log without wc"); a step whose job does not involve it need not mention it. Never refer to earlier steps with "it", "that process" or "those files".
- "command": one Linux command line for that task that never runs a ruled-out program in any way (not through sudo, xargs, find -exec, sh -c or $(...)). Where a value can only come from an earlier step, use an upper-case placeholder such as PID or FILE.
- One step for each command a careful administrator would run, in the order they would run them: look first, change last. Leave out parts of the request that are advice or judgement.

Commands: Linux only (GNU coreutils, util-linux, procps, iproute2, systemd), never macOS or BSD forms, only options you are sure exist, one line each, no sudo unless needed."""

CONTEXTS_SYSTEM = """You write training data for an offline Linux command-line assistant.

Write {count} different requests about {context}, each answered by one command line. Each request names its context clearly (the program, the file or the distribution), as a user who knows what they want to configure would.

The right forms in this context: {forms}

Requests:
- Varied jobs: set up, change, check, remove; with and without a user name and password; http and https; a list of hosts that bypass the proxy where it fits. For a firewall: open and close ports and services, allow or deny addresses and networks, list the rules, delete one, enable it.
- About half with concrete values (host names, ports, addresses, user names) that the command must use exactly. In the rest the request gives no value ("set a proxy for git") and the command uses one such as http://proxy.example.com:3128; the request never says "placeholder".
- Sound like real users: informal, some questions, some bare requests. Keep them short (most under 15 words) and plain.

Commands: as short and plain as the job allows, one line (joined with && where the job takes several commands, such as writing a file and then reloading a service), the standard form for this context, Linux only, only options you are sure exist, sudo where the job needs root."""

TWINS_SYSTEM = """You edit training data for a Linux command-line assistant. Each item is a request that rules out a program, the program ruled out, and the command given for it.

For each item write its twin: the same request with the restriction taken out (no "without X", "X isn't installed", "instead of X", "use Y rather than X" ...), otherwise worded as close to the original as reads naturally, and the command a competent administrator reaches for first on a current Debian or Ubuntu system. That is often the ruled-out program; where the given command is already the usual choice (ss rather than netstat, ip rather than ifconfig), give the same command again. Keep every value of the request. Linux only (GNU coreutils, util-linux, procps, iproute2, systemd), only options you are sure exist, one line, no sudo unless the job needs it. If the request makes no sense without its restriction (it is about the other tool itself), set "skip" to true and leave the rest empty."""

TWINS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "skip": {"type": "boolean"},
                    "question": {"type": "string"},
                    "command": {"type": "string"},
                },
                "required": ["n", "skip", "question", "command"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}
TWINS_BATCH = 10

EXEC_AREAS = (
    "printing parts of a text file (first, last, middle lines, lines matching a position)",
    "searching text in files and directories (case, whole words, inverted, counts, file names only)",
    "sorting, removing duplicates and counting repeated lines",
    "columns and fields of CSV, TSV and space-separated data (select, filter by value, sum, distinct)",
    "finding files by name, extension, type or size",
    "finding files by age (modified more or less than some days ago), combined with name or size",
    "copying, moving, renaming, linking and deleting files and directories",
    "permissions and file attributes (making executable, mode bits, empty files, symlinks)",
    "archives and compression (tar, gzip, zip: create, list, extract, into a directory)",
    "transforming text (replace words, change case, number lines, join or merge files, squeeze blanks)",
    "counting things (lines, words, files in a directory tree, matching lines, distinct values)",
    "comparing files and lists (lines only in one file, common lines, whether two files differ)",
)
EXEC_REQUESTS = 360
EXEC_PER_REQUEST = 10
# Round 5: where the round-4 model still slips.
EXEC2_AREAS = (
    "files modified, accessed or changed within the last few minutes or hours (not days), set up with touch -d '20 minutes ago' and similar",
    "listing only directories, only regular files, only hidden entries, or only symlinks in a directory, not recursing unless asked",
    "counting entries in a directory or tree without counting the directory itself; counting lines, words or matches exactly",
    "joining files one after another into a new file versus pasting them side by side; appending versus overwriting",
    "the biggest or smallest files (not directories) by size, the newest or oldest by modification time, top N",
    "deleting, moving or copying only what matches several conditions (name, size, age, type), leaving everything else",
)
EXEC2_REQUESTS = 120

# Round 6. Programs a request may rule out. Each gets one request, the ones people use
# most a second.
FORBID_PROGRAMS = (
    "lsof netstat wc curl wget ifconfig route grep head tail tar gzip ls find whoami free top htop systemctl "
    "service apt apt-get dpkg vim nano sed awk du rm cp mv df lsblk crontab sudo uname dig nslookup cat uniq "
    "sort cut tr ps less seq basename dirname realpath stat file diff tee touch mkdir chmod chown id who "
    "journalctl ip ss scp rsync unzip zip bzip2 xz python3 perl nc ping traceroute pgrep pkill kill killall "
    "printf tac nl paste comm md5sum sha256sum base64 hexdump strings xargs date hostname"
).split()
FORBID_TWICE = frozenset("lsof netstat wc curl ifconfig grep head tail tar find ls sed awk du rm cp systemctl sudo cat".split())
# Never trained on: evalsets/forbidden_heldout.jsonl rules these out, to see whether
# the habit carries over to programs the model never saw ruled out.
FORBID_HELDOUT = frozenset("sort cut ping kill mv hostname xargs date".split())
FORBID_PER_REQUEST = 10
FORBID_SCENARIO_REQUESTS = 40
FORBID_SCENARIOS_PER_REQUEST = 3

# A context a vague request leaves open, and the forms a right command takes in it.
CONTEXTS = {
    "an http proxy for this shell session only": "export http_proxy, https_proxy, no_proxy (and the upper-case forms); unset to remove",
    "an http proxy system-wide for every user": "lines such as http_proxy=\"http://proxy:3128\" appended to /etc/environment with sudo tee -a (or removed with sudo sed -i)",
    "a proxy for git": "git config --global http.proxy (it covers https too; git has no https.proxy), per repository without --global, git config --global --unset http.proxy",
    "a proxy for apt": "a file in /etc/apt/apt.conf.d/ with Acquire::http::Proxy \"http://...\"; and Acquire::https::Proxy, written with sudo tee; or for one run: sudo apt-get -o Acquire::http::Proxy=\"http://...\" update",
    "a proxy for dnf or yum": "a proxy=http://... line in /etc/dnf/dnf.conf (or /etc/yum.conf), with proxy_username and proxy_password if needed",
    "a proxy for pip": "pip install --proxy http://... package, or pip config set global.proxy http://...",
    "a proxy for npm": "npm config set proxy http://... and npm config set https-proxy http://..., npm config delete proxy",
    "a proxy for the docker daemon (pulling images)": "a systemd drop-in /etc/systemd/system/docker.service.d/http-proxy.conf with [Service] Environment=\"HTTP_PROXY=...\" lines (sudo mkdir -p, printf | sudo tee), then sudo systemctl daemon-reload && sudo systemctl restart docker",
    "a proxy for docker containers and builds": "~/.docker/config.json proxies.default httpProxy/httpsProxy/noProxy, or docker build --build-arg HTTP_PROXY=..., docker run -e HTTP_PROXY=...",
    "curl or wget through a proxy": "curl -x http://proxy:port URL (--proxy-user user:pass), wget -e use_proxy=yes -e https_proxy=http://... URL",
    "ssh through a jump host or a socks proxy": "ssh -J user@jump user@target, ProxyJump in ~/.ssh/config, ssh -D 1080 -N for a socks proxy, ProxyCommand with nc -X 5 -x",
    "a proxy for snap": "sudo snap set system proxy.http=\"http://...\" proxy.https=\"http://...\"",
    "the ufw firewall (Ubuntu)": "sudo ufw allow 8080/tcp, sudo ufw allow from 10.0.0.0/24 to any port 22, ufw deny, ufw status numbered, ufw delete NUMBER, ufw enable, ufw limit ssh",
    "the firewalld firewall (Fedora, RHEL)": "sudo firewall-cmd --permanent --add-port=8080/tcp && sudo firewall-cmd --reload, --add-service=http, --list-all, --zone=public, --remove-port, --get-active-zones",
}
CONTEXT_REQUESTS_EACH = 2
CONTEXTS_PER_REQUEST = 12

AREAS = {
    "process management": "ps top pgrep pkill kill lsof nice renice pstree pidof uptime free",
    "disk space": "df du find ncdu lsblk sort head stat ls findmnt",
    "log analysis": "journalctl grep awk sed sort uniq tail head zgrep wc cut",
    "networking": "ss ip ping curl dig nc traceroute iptables nft hostname nmcli",
    "backup and archives": "tar rsync gzip zip unzip cp date sha256sum xz scp",
    "finding and cleaning files": "find xargs rm ls du stat touch mv rmdir",
    "users and permissions": "chmod chown id stat ls getfacl setfacl namei groups usermod sudo",
    "services and systemd": "systemctl journalctl systemd-analyze loginctl timedatectl crontab",
    "text processing": "awk sed grep cut sort uniq tr paste join column wc head tail",
    "mixed troubleshooting": "ps free df du journalctl systemctl ss dmesg uptime vmstat iostat lsof",
}


class Budget:
    """Running spend, shared across jobs and reruns through a ledger file.

    A request reserves its worst-case cost before it is sent and settles to the
    real cost afterwards, so concurrent requests cannot overshoot the cap together.
    """

    def __init__(self, path: Path, cap: float):
        self.path, self.cap = path, cap
        self._lock = threading.Lock()
        self._reserved = 0.0
        self.ledger = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"spent_usd": 0.0, "requests": 0, "by_model": {}}

    @property
    def spent(self) -> float:
        return self.ledger["spent_usd"]

    def reserve(self, worst_case: float) -> bool:
        with self._lock:
            if self.spent + self._reserved + worst_case > self.cap:
                return False
            self._reserved += worst_case
            return True

    def settle(self, worst_case: float, actual: float, model: str) -> None:
        with self._lock:
            self._reserved -= worst_case
            self.ledger["spent_usd"] = round(self.spent + actual, 6)
            self.ledger["requests"] += 1
            self.ledger["by_model"][model] = round(self.ledger["by_model"].get(model, 0.0) + actual, 6)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.ledger, indent=2) + "\n", encoding="utf-8")


def load_key() -> str:
    key = os.environ.get("LXASK_GEN_API_KEY", "").strip()
    # ~/.config/linu: where it was kept up to 0.8, when lxask was called linu
    for path in (KEY_FILE, KEY_FILE.parent.with_name("linu") / KEY_FILE.name):
        if not key and path.exists():
            key = path.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit(f"no API key: set LXASK_GEN_API_KEY or put the key in {KEY_FILE}")
    return key


def post(key: str, body: dict, attempts: int = 5) -> dict:
    """POST to the Responses API, retrying rate limits and server errors."""
    data = json.dumps(body).encode("utf-8")
    for attempt in range(attempts):
        request = urllib.request.Request(
            API_URL, data=data, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            if error.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise RuntimeError(f"HTTP {error.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts - 1:
                raise RuntimeError(f"network error: {error}") from None
        time.sleep(min(60, 2**attempt * 3))
    raise RuntimeError("unreachable")


def output_json(response: dict) -> dict | None:
    """The structured reply inside a Responses API result, or None (refusal, truncation)."""
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                try:
                    return json.loads(part["text"])
                except ValueError:
                    return None
    return None


def cost(usage: dict, price: tuple[float, float]) -> float:
    return (usage.get("input_tokens", 0) * price[0] + usage.get("output_tokens", 0) * price[1]) / 1e6


# What each job asks for. A request is (id, seed, system prompt, user prompt, schema name, schema, max output tokens).


def load_raw(job: str) -> list[dict]:
    path = OUT / f"{job}.raw.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def man_headings() -> dict[str, list[str]]:
    return {page["name"]: list(dict.fromkeys(man_option_titles(page["text"]))) for page in sources.load_man()}


def page_prompt(page, headings: dict[str, list[str]]) -> str:
    """One command's documentation as the model sees it: tldr page, then man option headings."""
    lines = [f"Command: {page.name}", f"Summary: {page.summary}", "", "Examples from the documentation:"]
    lines += [f"- {description}: {strip_placeholders(command)}" for description, command in page.examples]
    options = headings.get(page.name.replace(" ", "-")) or headings.get(program(page.name)) or []
    if options:
        lines += ["", "Options from the manual page:", "\n".join(options)[:MAX_OPTION_CHARS]]
    return "\n".join(lines)


def task_requests() -> list[dict]:
    pages = sources.load_tldr_pages()
    core = sources.core_programs()
    usage = sources.usage_counts()
    headings = man_headings()

    requests = []
    for page in pages.values():
        name = program(page.name)
        if name not in core or name in NOT_LINUX_PROGRAMS or page.name in SKIP_PAGES:
            continue
        count = TASKS_PER_COMMON_PAGE if usage[name] >= COMMON_USAGE and " " not in page.name else TASKS_PER_PAGE
        requests.append({
            "id": page.name,
            "seed": {"cmd": page.name, "count": count},
            "system": TASKS_SYSTEM.format(count=count),
            "user": page_prompt(page, headings),
            "schema": ("task_list", TASKS_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 130 * count,
        })
    # Most asked-about commands first, so a run cut short has covered what matters most.
    requests.sort(key=lambda r: -usage[program(r["id"])])
    return requests


def depth_requests() -> list[dict]:
    """More questions for the commands people ask about most. Each request is shown what
    was already asked about the command, so the new questions cover other uses."""
    pages = sources.load_tldr_pages()
    usage = sources.usage_counts()
    headings = man_headings()
    asked: dict[str, list[str]] = {}
    for record in load_raw("tasks"):
        asked.setdefault(record["seed"]["cmd"], []).extend(item["question"] for item in record.get("items", []))

    top = [name for name in sorted(usage, key=lambda n: (-usage[n], n))
           if name in pages and name not in NOT_LINUX_PROGRAMS and name not in SKIP_PAGES][:DEPTH_PROGRAMS]
    requests = []
    for round_number in range(DEPTH_ROUNDS):
        for name in top:
            covered = "\n".join(f"- {question}" for question in asked.get(name, [])[:40])
            requests.append({
                "id": f"{name}#{round_number}",
                "seed": {"cmd": name, "count": TASKS_PER_COMMON_PAGE, "round": round_number},
                "system": TASKS_SYSTEM.format(count=TASKS_PER_COMMON_PAGE),
                "user": (f"{page_prompt(pages[name], headings)}\n\nAlready asked about this command; cover other uses "
                         f"(set {round_number + 1} of {DEPTH_ROUNDS}):\n{covered}"),
                "schema": ("task_list", TASKS_SCHEMA),
                "max_output_tokens": REASONING_ALLOWANCE + 130 * TASKS_PER_COMMON_PAGE,
            })
    return requests


_SUGGESTION_POOL: list[str] = []


def suggested_commands(number: int, area: str) -> list[str]:
    """Five of the area's usual tools and two of the most used commands overall, varied by
    request so the model is steered to different ground each time."""
    if not _SUGGESTION_POOL:
        usage = sources.usage_counts()
        documented = {program(name) for name in sources.load_tldr_pages()} - NOT_LINUX_PROGRAMS
        _SUGGESTION_POOL.extend(sorted(documented & set(usage), key=lambda name: (-usage[name], name))[:150])
    rng = random.Random(number)
    return rng.sample(AREAS[area].split(), 5) + rng.sample(_SUGGESTION_POOL, 2)


def scenario_requests() -> list[dict]:
    areas = list(AREAS)
    requests = []
    for number in range(SCENARIO_REQUESTS):
        area = areas[number % len(areas)]
        suggested = suggested_commands(number, area)
        requests.append({
            "id": f"{area}#{number}",
            "seed": {"area": area, "suggested": suggested},
            "system": SCENARIOS_SYSTEM.format(count=SCENARIOS_PER_REQUEST),
            "user": f"Area: {area}\nSuggested commands: {', '.join(suggested)}\nVariation number: {number}",
            "schema": ("scenario_list", SCENARIOS_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 2400,
        })
    return requests


def long_requests() -> list[dict]:
    areas = list(AREAS)
    requests = []
    for number in range(LONG_REQUESTS):
        area = areas[number % len(areas)]
        suggested = suggested_commands(5000 + number, area)
        requests.append({
            "id": f"long:{area}#{number}",
            "seed": {"area": area, "suggested": suggested},
            "system": LONG_SYSTEM.format(count=SCENARIOS_PER_REQUEST),
            "user": f"Area: {area}\nSuggested commands: {', '.join(suggested)}\nVariation number: {number}",
            "schema": ("scenario_list", SCENARIOS_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 3600,
        })
    return requests


def everyday_requests() -> list[dict]:
    areas = list(AREAS)
    requests = []
    for number in range(EVERYDAY_REQUESTS):
        area = areas[number % len(areas)]
        suggested = suggested_commands(1000 + number, area)
        requests.append({
            "id": f"everyday:{area}#{number}",
            "seed": {"area": area, "suggested": suggested, "count": EVERYDAY_PER_REQUEST},
            "system": EVERYDAY_SYSTEM.format(count=EVERYDAY_PER_REQUEST),
            "user": f"Area: {area}\nSuggested commands: {', '.join(suggested)}\nVariation number: {number}",
            "schema": ("task_list", TASKS_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 130 * EVERYDAY_PER_REQUEST,
        })
    return requests


def review_key(question: str, command: str) -> str:
    return f"{question}\n{command}"


def review_requests() -> list[dict]:
    """The kept single tasks the small model wrote, in batches, minus those already reviewed."""
    seed_file = OUT.parents[1] / "seed" / "generated_tasks.jsonl"
    if not seed_file.exists():
        return []
    reviewed = {review_key(*pair) for record in load_raw("review") for pair in record["seed"]["items"]}
    pending = []
    for line in seed_file.read_text(encoding="utf-8").splitlines():
        task = json.loads(line)
        if task.get("source", "tasks") in REVIEWED_SOURCES and review_key(task["question"], task["command"]) not in reviewed:
            pending.append((task["question"], task["command"]))
    requests = []
    for start in range(0, len(pending), REVIEW_BATCH):
        batch = pending[start : start + REVIEW_BATCH]
        listing = "\n".join(f"{n}. Q: {question}\n   A: {command}" for n, (question, command) in enumerate(batch, 1))
        digest = hashlib.sha1("\n".join(review_key(*pair) for pair in batch).encode("utf-8")).hexdigest()[:16]
        requests.append({
            "id": f"review:{digest}",
            "seed": {"items": [list(pair) for pair in batch]},
            "system": REVIEW_SYSTEM,
            "user": listing,
            "schema": ("review", REVIEW_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 60 * REVIEW_BATCH,
        })
    return requests


def _digest(parts: list[str]) -> str:
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def fix_requests() -> list[dict]:
    """The tasks the reviewer judged wrong, in batches, with its reason, for a correction."""
    rejects_file = OUT / "rejects.jsonl"
    if not rejects_file.exists():
        return []
    pending, seen = [], set()
    for line in rejects_file.read_text(encoding="utf-8").splitlines():
        reject = json.loads(line)
        reasons = reject.get("reasons", [])
        if reject.get("job") != "tasks" or len(reasons) != 1 or not reasons[0].startswith("reviewer: "):
            continue  # only those the reviewer alone rejected: the rest are broken in other ways too
        key = review_key(reject["question"], reject["command"])
        if key not in seen:
            seen.add(key)
            pending.append([reject["question"], reject["command"], reasons[0][len("reviewer: "):], reject["cmd"]])
    requests = []
    for start in range(0, len(pending), FIX_BATCH):
        batch = pending[start : start + FIX_BATCH]
        listing = "\n".join(f"{n}. Q: {q}\n   wrong: {wrong}\n   reviewer: {why}" for n, (q, wrong, why, _) in enumerate(batch, 1))
        requests.append({
            "id": f"fix:{_digest([review_key(q, wrong) for q, wrong, _, _ in batch])}",
            "seed": {"items": batch},
            "system": FIX_SYSTEM,
            "user": listing,
            "schema": ("fixes", FIX_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 80 * FIX_BATCH,
        })
    return requests


def scenarios3_requests() -> list[dict]:
    areas = list(AREAS)
    requests = []
    for number in range(SCENARIOS3_REQUESTS):
        area = areas[number % len(areas)]
        suggested = suggested_commands(9000 + number, area)
        requests.append({
            "id": f"s3:{area}#{number}",
            "seed": {"area": area, "suggested": suggested},
            "system": SCENARIOS3_SYSTEM.format(count=SCENARIOS_PER_REQUEST),
            "user": f"Area: {area}\nSuggested commands: {', '.join(suggested)}\nVariation number: {number}",
            "schema": ("scenario_list", SCENARIOS_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 3600,
        })
    return requests


def constraint_requests() -> list[dict]:
    areas = list(AREAS)
    requests = []
    for number in range(CONSTRAINT_REQUESTS):
        area = areas[number % len(areas)]
        suggested = suggested_commands(12000 + number, area)
        requests.append({
            "id": f"constraints:{area}#{number}",
            "seed": {"area": area, "suggested": suggested, "count": CONSTRAINTS_PER_REQUEST},
            "system": CONSTRAINTS_SYSTEM.format(count=CONSTRAINTS_PER_REQUEST),
            "user": f"Area: {area}\nSuggested commands: {', '.join(suggested)}\nVariation number: {number}",
            "schema": ("task_list", TASKS_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 150 * CONSTRAINTS_PER_REQUEST,
        })
    return requests


def scenario_literal_problems(request: str, steps: list[dict]) -> list[str]:
    """Why a scenario would teach the model to lose or invent values: its tasks drop a
    literal of the request, a command lacks a literal of its task, or a command uses a
    path or file name its task never gives (the step model, which sees only the task,
    would have to guess it)."""
    problems = []
    if len(steps) > 1:
        tasks = " ; ".join(step["task"] for step in steps)
        # the tasks are text, not a command: nothing in them "runs" a ruled-out program
        problems += [f"tasks lose {miss.detail.split(';')[0][17:]}" for miss in constraints.check(request, tasks)
                     if miss.kind not in ("slip", "forbidden")]
    for step in steps:
        problems += [f"command lacks {miss.detail.split(';')[0][17:]}" for miss in constraints.check(step["task"], step["command"])
                     if miss.kind != "slip"]
        problems += [f"task lacks {miss.detail.split(';')[0][17:]}" for miss in constraints.check(step["command"], step["task"])
                     if miss.kind in ("path", "filename")]
    return problems


def retask_requests() -> list[dict]:
    """Earlier scenarios whose tasks leave out values their commands use, in batches.
    Read from the raw output, since filter_generated.py drops such scenarios until
    their tasks are rewritten."""
    pending, seen = [], set()
    for job in ("scenarios", "long"):
        for record in load_raw(job):
            for scenario in record.get("scenarios", []):
                steps = [step for step in scenario.get("steps", []) if step.get("task") and step.get("command")]
                if (scenario.get("request") and steps and len(steps) == len(scenario["steps"])
                        and scenario["request"] not in seen and scenario_literal_problems(scenario["request"], steps)):
                    seen.add(scenario["request"])
                    pending.append({"request": scenario["request"], "steps": steps})
    requests = []
    for start in range(0, len(pending), RETASK_BATCH):
        batch = pending[start : start + RETASK_BATCH]
        listing = "\n\n".join(
            f"{n}. Request: {s['request']}\n" + "\n".join(f"   task: {step['task']}\n   command: {step['command']}" for step in s["steps"])
            for n, s in enumerate(batch, 1))
        requests.append({
            "id": f"retask:{_digest([s['request'] for s in batch])}",
            "seed": {"requests": [s["request"] for s in batch], "steps": [len(s["steps"]) for s in batch]},
            "system": RETASK_SYSTEM,
            "user": listing,
            "schema": ("retask", RETASK_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 400 * RETASK_BATCH,
        })
    return requests


def exec_requests() -> list[dict]:
    requests = []
    for number in range(EXEC_REQUESTS):
        area = EXEC_AREAS[number % len(EXEC_AREAS)]
        requests.append({
            "id": f"exec:{number}",
            "seed": {"area": area, "count": EXEC_PER_REQUEST},
            "system": EXEC_SYSTEM.format(count=EXEC_PER_REQUEST),
            "user": f"Area: {area}\nVariation number: {number // len(EXEC_AREAS)}",
            "schema": ("exec_tasks", EXEC_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 350 * EXEC_PER_REQUEST,
        })
    return requests


def exec2_requests() -> list[dict]:
    requests = []
    for number in range(EXEC2_REQUESTS):
        area = EXEC2_AREAS[number % len(EXEC2_AREAS)]
        requests.append({
            "id": f"exec2:{number}",
            "seed": {"area": area, "count": EXEC_PER_REQUEST},
            "system": EXEC_SYSTEM.format(count=EXEC_PER_REQUEST),
            "user": f"Area: {area}\nVariation number: {number // len(EXEC2_AREAS)}",
            "schema": ("exec_tasks", EXEC_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 350 * EXEC_PER_REQUEST,
        })
    return requests


def forbid_requests() -> list[dict]:
    requests = []
    trained = [name for name in FORBID_PROGRAMS if name not in FORBID_HELDOUT]
    for round_number in (0, 1):
        for name in trained if round_number == 0 else [n for n in trained if n in FORBID_TWICE]:
            requests.append({
                "id": f"forbid:{name}#{round_number}",
                "seed": {"forbid": [name], "count": FORBID_PER_REQUEST, "round": round_number},
                "system": FORBID_SYSTEM.format(program=name, count=FORBID_PER_REQUEST),
                "user": f"Ruled out: {name}\nVariation number: {round_number}",
                "schema": ("task_list", TASKS_SCHEMA),
                "max_output_tokens": REASONING_ALLOWANCE + 150 * FORBID_PER_REQUEST,
            })
    # The held-out programs: a test set, never trained on (filter_generated.py).
    for name in sorted(FORBID_HELDOUT):
        requests.append({
            "id": f"forbid:{name}#heldout",
            "seed": {"forbid": [name], "count": FORBID_PER_REQUEST, "heldout": True},
            "system": FORBID_SYSTEM.format(program=name, count=FORBID_PER_REQUEST),
            "user": f"Ruled out: {name}\nVariation number: 0",
            "schema": ("task_list", TASKS_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 150 * FORBID_PER_REQUEST,
        })
    return requests


def forbid_scenario_requests() -> list[dict]:
    areas = list(AREAS)
    requests = []
    for number in range(FORBID_SCENARIO_REQUESTS):
        area = areas[number % len(areas)]
        rng = random.Random(15000 + number)
        candidates = [name for name in AREAS[area].split() if name in FORBID_PROGRAMS and name not in FORBID_HELDOUT]
        ruled_out = rng.sample(candidates, min(len(candidates), rng.choice((1, 1, 2))))
        suggested = suggested_commands(15000 + number, area)
        requests.append({
            "id": f"forbid_scenarios:{area}#{number}",
            "seed": {"area": area, "suggested": suggested, "forbid": ruled_out},
            "system": FORBID_SCENARIOS_SYSTEM.format(count=FORBID_SCENARIOS_PER_REQUEST),
            "user": (f"Area: {area}\nRuled out: {', '.join(ruled_out)}\nOther commands that may help: "
                     f"{', '.join(s for s in suggested if s not in ruled_out)}\nVariation number: {number}"),
            "schema": ("scenario_list", SCENARIOS_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 3000,
        })
    return requests


def context_requests() -> list[dict]:
    requests = []
    for round_number in range(CONTEXT_REQUESTS_EACH):
        for context, forms in CONTEXTS.items():
            requests.append({
                "id": f"contexts:{context}#{round_number}",
                "seed": {"context": context, "count": CONTEXTS_PER_REQUEST, "round": round_number},
                "system": CONTEXTS_SYSTEM.format(count=CONTEXTS_PER_REQUEST, context=context, forms=forms),
                "user": f"Context: {context}\nVariation number: {round_number}",
                "schema": ("task_list", TASKS_SCHEMA),
                "max_output_tokens": REASONING_ALLOWANCE + 170 * CONTEXTS_PER_REQUEST,
            })
    return requests


def twin_requests() -> list[dict]:
    """The kept `forbid` tasks, in batches, for their twins without the restriction:
    trained on side by side, they teach that the restriction makes the difference, not
    that the usual program is wrong."""
    seed_file = OUT.parents[1] / "seed" / "generated_tasks.jsonl"
    if not seed_file.exists():
        return []
    raw = {review_key(item["question"].strip(), item["command"].strip()): record["seed"]["forbid"]
           for record in load_raw("forbid") for item in record.get("items", [])}
    pending = []
    for line in seed_file.read_text(encoding="utf-8").splitlines():
        task = json.loads(line)
        if task.get("source") == "forbid" and (ruled_out := raw.get(review_key(task["question"], task["command"]))):
            pending.append([task["question"], ", ".join(ruled_out), task["command"]])
    requests = []
    for start in range(0, len(pending), TWINS_BATCH):
        batch = pending[start : start + TWINS_BATCH]
        listing = "\n".join(f"{n}. request: {q}\n   ruled out: {p}\n   command: {c}" for n, (q, p, c) in enumerate(batch, 1))
        requests.append({
            "id": f"forbid_twins:{_digest([review_key(q, c) for q, _, c in batch])}",
            "seed": {"items": batch},
            "system": TWINS_SYSTEM,
            "user": listing,
            "schema": ("twins", TWINS_SCHEMA),
            "max_output_tokens": REASONING_ALLOWANCE + 90 * TWINS_BATCH,
        })
    return requests


JOBS: dict[str, Callable[[], list[dict]]] = {
    "tasks": task_requests,
    "scenarios": scenario_requests,
    "depth": depth_requests,
    "everyday": everyday_requests,
    "long": long_requests,
    "review": review_requests,
    "fix": fix_requests,
    "scenarios3": scenarios3_requests,
    "constraints": constraint_requests,
    "retask": retask_requests,
    "exec": exec_requests,
    "exec2": exec2_requests,
    "forbid": forbid_requests,
    "forbid_scenarios": forbid_scenario_requests,
    "contexts": context_requests,
    "forbid_twins": twin_requests,
}


def run(job: str, model: str, price: tuple[float, float], budget: Budget, limit: int | None, workers: int,
        effort: str | None, send: Callable[[dict], dict]) -> None:
    out_path = OUT / f"{job}.raw.jsonl"
    done = set()
    if out_path.exists():
        done = {json.loads(line)["id"] for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    todo = [request for request in JOBS[job]() if request["id"] not in done]
    todo = todo[:limit] if limit else todo
    print(f"{job}: {len(done)} done, {len(todo)} to do, model {model}, spent ${budget.spent:.2f} of ${budget.cap:.2f}", flush=True)

    lock = threading.Lock()
    stats = {"written": 0, "failed": 0, "stopped": False}
    OUT.mkdir(parents=True, exist_ok=True)

    def work(request: dict) -> None:
        worst = (len(request["system"] + request["user"]) / 3 * price[0] + request["max_output_tokens"] * price[1]) / 1e6
        if not budget.reserve(worst):
            stats["stopped"] = True
            return
        name, schema = request["schema"]
        body = {
            "model": model,
            "input": [{"role": "system", "content": request["system"]}, {"role": "user", "content": request["user"]}],
            "text": {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
            "max_output_tokens": request["max_output_tokens"],
        }
        if effort:
            body["reasoning"] = {"effort": effort}
        actual = 0.0
        try:
            response = send(body)
            usage = response.get("usage", {})
            actual = cost(usage, price)
            content = output_json(response)
            if content is None:
                why = (response.get("incomplete_details") or {}).get("reason") or response.get("status") or "no JSON in reply"
                print(f"  {request['id']}: unusable reply ({why})", file=sys.stderr, flush=True)
        except RuntimeError as error:
            content, usage = None, {}
            print(f"  {request['id']}: {error}", file=sys.stderr, flush=True)
        finally:
            budget.settle(worst, actual, model)
        with lock:
            if content is None:
                stats["failed"] += 1
                return
            record = {"id": request["id"], "job": job, "model": model, "seed": request["seed"], **content, "usage": usage}
            with open(out_path, "a", encoding="utf-8") as out:
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
            stats["written"] += 1
            if stats["written"] % 25 == 0:
                print(f"  {stats['written']}/{len(todo)} written, ${budget.spent:.2f} spent", flush=True)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(work, todo))

    print(f"{job}: {stats['written']} written, {stats['failed']} failed, spent ${budget.spent:.2f} of ${budget.cap:.2f}")
    if stats["stopped"]:
        print(f"stopped at the ${budget.cap:.2f} cap; raise --cap to continue", file=sys.stderr)
    elif stats["failed"]:
        print("rerun to retry the failed requests", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job", choices=[*JOBS, "status"])
    parser.add_argument("--model", help="default: $LXASK_GEN_MODEL")
    parser.add_argument("--price-in", type=float, help="USD per 1M input tokens (required: no price table is shipped)")
    parser.add_argument("--price-out", type=float, help="USD per 1M output tokens")
    parser.add_argument("--cap", type=float, default=50.0, help="stop before total spend passes this many USD (default 50)")
    parser.add_argument("--limit", type=int, help="send at most this many requests (for a trial)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--reasoning-effort", help=f"reasoning.effort for the API (default per job: {DEFAULT_EFFORT})")
    args = parser.parse_args()

    budget = Budget(OUT / "ledger.json", args.cap)
    if args.job == "status":
        print(json.dumps(budget.ledger, indent=2))
        for job in JOBS:
            path = OUT / f"{job}.raw.jsonl"
            lines = path.read_text(encoding="utf-8").count("\n") if path.exists() else 0
            done = {record["id"] for record in load_raw(job)}
            left = sum(request["id"] not in done for request in JOBS[job]())
            print(f"{job}: {lines} requests done, {left} to do")
        return

    model = args.model or os.environ.get("LXASK_GEN_MODEL", "").strip()
    if not model:
        sys.exit("no model given: pass --model or set LXASK_GEN_MODEL")
    if args.price_in is not None and args.price_out is not None:
        price = (args.price_in, args.price_out)
    else:
        sys.exit("no price given: pass --price-in and --price-out so the cost cap can work")
    if not API_URL:
        sys.exit("no endpoint given: set LXASK_GEN_API_URL to the API that should write the data")
    key = load_key()
    effort = args.reasoning_effort or DEFAULT_EFFORT.get(args.job)
    run(args.job, model, price, budget, args.limit, args.workers, effort, lambda body: post(key, body))


if __name__ == "__main__":
    main()
