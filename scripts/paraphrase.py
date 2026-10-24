"""Ask a local teacher model (Ollama) to phrase tldr tasks the way people ask them.

Only the *question* comes from the teacher. Commands and explanations stay derived
from the documentation, so a teacher mistake costs a clumsy question, never a wrong
command. Results are appended to data/paraphrases.jsonl; rerun to resume.

    python scripts/paraphrase.py --model qwen2.5:7b

Standard library only, so it can run wherever Ollama does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sources
from lxask.index import clean_description, program
from tldr_data import fill, is_held_out

OUT = Path(__file__).resolve().parents[1] / "data" / "paraphrases.jsonl"

SYSTEM = (
    "You write the requests people type into a Linux command-line help tool. "
    "They are short and casual, like a search query or a quick question to a colleague: "
    "lower-case is fine, no greetings, no explanations."
)

INSTRUCTIONS = """For each task below write two requests a user might type to get that task done.

"generic": the task in your own words, as a full natural request. Do not reuse the task's phrasing. No file names, paths, numbers or other concrete values.
"specific": the same request, but with every value listed under the task included exactly as written. If the task lists no values, use null.

Rules:
- Plain English only. Never write a command, an option such as -v or --force, or the tool's syntax.
- Do not name the tool, unless it is one people name when asking (git, docker, apt, systemctl and the like).
- Vary the style: some questions ("how do i ..."), some bare requests ("show me ..."), some first person ("i need to ...").

Example
Tasks:
1. [tar] Extract a (compressed) archive file into the current directory verbosely
   values: backup.tar.gz
2. [df] Display all filesystems and their disk usage in human-readable form
3. [git branch] Rename a branch
   values: develop, release
Output:
{{"items": [{{"generic": "how do i unpack a tarball here and see each file as it comes out", "specific": "unpack backup.tar.gz into this folder and list the files as it goes"}}, {{"generic": "how much space is left on each of my drives, in sizes i can read", "specific": null}}, {{"generic": "i need to give a git branch a different name", "specific": "rename the git branch develop to release"}}]}}

Tasks:
{tasks}

Return JSON: {{"items": [{{"generic": "...", "specific": "..."}}, ...]}} with exactly {count} items, in the same order."""

# A request that is really a command: it carries an option or shell plumbing, it is
# too short to be a sentence (`gzip report.pdf`), or it has none of the small words
# every English request has (`cp ./src config.yaml app.log ~/Projects`). The tool's
# name alone proves nothing, since kill, find, sort and join are also plain English.
_COMMAND_SHAPED = re.compile(r"(^|\s)--?[A-Za-z]|[|;`$<>]|&&")
_FUNCTION_WORDS = frozenset(
    "a an the to of in on for with from into my me i is are all and or that this it its as by at how what each every".split()
)
MIN_WORDS = 4


def looks_like_command(text: str) -> bool:
    words = text.lower().split()
    return bool(_COMMAND_SHAPED.search(text)) or len(words) < MIN_WORDS or not _FUNCTION_WORDS & set(words)


def mentions(text: str, value: str) -> bool:
    """Whether `text` carries `value` in any spelling: `~/Downloads` counts in "in downloads"."""
    core = value.rstrip("/").rsplit("/", 1)[-1].lower()
    return bool(core) and re.search(rf"(?<![a-z0-9]){re.escape(core)}(?![a-z0-9])", text.lower()) is not None


def seeded(cmd: str, description: str) -> random.Random:
    digest = hashlib.sha1(f"{cmd}\n{description}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def pending_items(done: set[tuple[str, str]], limit: int | None) -> list[dict]:
    """Tasks still to phrase: the held-out ones first (the eval set needs them), then
    the most asked-about commands, so a run cut short has covered what matters most."""
    core = sources.core_programs()
    usage = sources.usage_counts()
    items = []
    for page in sources.load_tldr_pages().values():
        if program(page.name) not in core:
            continue
        for description, command in page.examples:
            if (page.name, description) in done:
                continue
            filled = fill(command, seeded(page.name, description))
            items.append({
                "cmd": page.name,
                "description": description,
                "filled": filled[0] if filled else None,
                "values": filled[1] if filled else None,
            })
    random.Random(0).shuffle(items)
    items.sort(key=lambda item: (not is_held_out(item["cmd"], item["description"]), -usage[program(item["cmd"])]))
    return items[:limit] if limit else items


def ask(host: str, model: str, batch: list[dict]) -> list[dict] | None:
    lines = []
    for number, item in enumerate(batch, 1):
        lines.append(f"{number}. [{item['cmd']}] {clean_description(item['description'])}")
        if item["values"]:
            lines.append("   values: " + ", ".join(item["values"]))
    body = {
        "model": model,
        "stream": False,
        "format": "json",
        "keep_alive": "15m",
        "options": {"temperature": 0.8, "num_predict": 120 * len(batch)},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": INSTRUCTIONS.format(tasks="\n".join(lines), count=len(batch))},
        ],
    }
    request = urllib.request.Request(
        f"{host}/api/chat", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            answer = json.load(response)["message"]["content"]
        replies = json.loads(answer)["items"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(replies, list) or len(replies) != len(batch):
        return None
    return [accept(item, reply) for item, reply in zip(batch, replies)]


def accept(item: dict, reply: object) -> dict:
    """Keep what the teacher got right.

    A generic request must be a request with no concrete values in it; a specific
    one must carry every value verbatim. Anything shaped like a command is dropped.
    """
    reply = reply if isinstance(reply, dict) else {}
    generic, specific = reply.get("generic"), reply.get("specific")
    values = item["values"] or []

    generic = generic.strip() if isinstance(generic, str) else ""
    if (
        len(generic) > 200
        or "/" in generic
        or looks_like_command(generic)
        or any(mentions(generic, value) for value in values)
    ):
        generic = None

    specific = specific.strip() if isinstance(specific, str) else ""
    usable = (
        values
        and len(specific) <= 250
        and all(value in specific for value in values)
        and not looks_like_command(specific)
        and specific != item["filled"]
    )
    return {**item, "generic": generic, "specific": specific if usable else None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--batch", type=int, default=6)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit", type=int, help="stop after this many tasks (for a trial run)")
    args = parser.parse_args()

    done = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            done.add((record["cmd"], record["description"]))
    items = pending_items(done, args.limit)
    print(f"{len(done)} done, {len(items)} to do", flush=True)

    batches = [items[i : i + args.batch] for i in range(0, len(items), args.batch)]
    started = time.time()
    written = failed = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as out, ThreadPoolExecutor(args.workers) as pool:
        for number, records in enumerate(pool.map(lambda b: ask(args.host, args.model, b), batches), 1):
            if records is None:
                failed += 1
            else:
                for record in records:
                    out.write(json.dumps(record) + "\n")
                out.flush()
                written += len(records)
            if number % 20 == 0 or number == len(batches):
                rate = written / max(time.time() - started, 1e-9)
                print(f"{written}/{len(items)} written, {failed} batches failed, {rate:.2f} tasks/s", flush=True)
    if failed:
        print("rerun to retry the failed batches", file=sys.stderr)


if __name__ == "__main__":
    main()
