from __future__ import annotations

import argparse
import json
import random
import shlex
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SOURCE = "precision-v1"
SPLITS = ("train", "train", "dev", "test")


def write_file(path: str, text: str) -> str:
    return f"printf %s {shlex.quote(text)} > {shlex.quote(path)}"


def file_equals(path: str, text: str) -> str:
    return f"cmp -s {shlex.quote(path)} <(printf %s {shlex.quote(text)})"


@dataclass(frozen=True)
class Family:
    category: str
    template: int
    variant: int
    seed: int

    def path(self, stem: str, suffix: str = ".txt") -> str:
        separator = " " if self.variant % 2 else "_"
        return f"{stem}{separator}{self.category}_{self.template}_{self.variant}{suffix}"

    def task(self, operation: str, prompts: tuple[str, ...], reference: str, fixtures: list[dict],
             negatives: list[str], *, forbid: tuple[str, ...] = (), **values) -> dict:
        return {
            "id": f"{SOURCE}-{self.category}-{self.template}-{self.variant}-{operation}",
            "family": f"{SOURCE}/{self.category}/{self.template}",
            "category": self.category,
            "source": SOURCE,
            "seed": self.seed,
            "split": SPLITS[self.template],
            "q": prompts[self.template].format(**values),
            "reference": reference,
            "fixtures": fixtures,
            "negatives": negatives,
            **({"forbid": list(forbid)} if forbid else {}),
        }


def joins(family: Family, rng: random.Random) -> list[dict]:
    first, second, out = (family.path(stem) for stem in ("first", "second", "combined"))
    commands = {
        "concatenate": f"cat {shlex.quote(first)} {shlex.quote(second)} > {shlex.quote(out)}",
        "columns": f"paste {shlex.quote(first)} {shlex.quote(second)} > {shlex.quote(out)}",
    }
    prompts = {
        "concatenate": (
            "Concatenate `{a}` followed by `{b}` into `{out}`. Keep both source files unchanged.",
            "Write all lines of `{a}`, then all lines of `{b}`, to `{out}` without changing either input.",
            "Create `{out}` by placing the entire contents of `{b}` after `{a}`; preserve the originals.",
            "Combine `{a}` and `{b}` vertically, first file first, saving to `{out}` and leaving inputs alone.",
        ),
        "columns": (
            "Combine matching lines of `{a}` and `{b}` side by side, separated by a tab, into `{out}`. Preserve both inputs.",
            "Write `{out}` with each line from `{a}` followed by a tab and the corresponding line from `{b}`. Keep the inputs.",
            "Create two tab-separated columns in `{out}`, using `{a}` on the left and `{b}` on the right; do not modify them.",
            "Join `{a}` to `{b}` horizontally by line position, not by key, with tabs in `{out}`. Leave source files unchanged.",
        ),
    }
    fixtures = {operation: [] for operation in commands}
    for count in (2, 4, 7):
        left = [f"left_{rng.randrange(100, 999)}" for _ in range(count)]
        right = [f"right_{rng.randrange(100, 999)}" for _ in range(count)]
        setup = [write_file(first, "\n".join(left) + "\n"), write_file(second, "\n".join(right) + "\n")]
        for operation in commands:
            lines = left + right if operation == "concatenate" else [f"{a}\t{b}" for a, b in zip(left, right)]
            fixtures[operation].append({
                "setup": setup, "check": file_equals(out, "\n".join(lines) + "\n"), "unchanged": [first, second],
            })
    return [family.task(operation, prompts[operation], command, fixtures[operation],
                        [commands["columns" if operation == "concatenate" else "concatenate"]],
                        a=first, b=second, out=out) for operation, command in commands.items()]


def counts(family: Family, rng: random.Random) -> list[dict]:
    path = family.path("text")
    file = shlex.quote(path)
    prompts = {
        "lines": (
            "Print just the number of lines in `{file}`{restriction}.",
            "How many lines does `{file}` contain{restriction}? Output the count only.",
            "Count newline-terminated records in `{file}`{restriction}; print only that number.",
            "Return the line count of `{file}`{restriction}, with no other output.",
        ),
        "words": (
            "Print just the number of whitespace-separated words in `{file}`{restriction}.",
            "How many words does `{file}` contain{restriction}? Treat whitespace as the separator and output only the count.",
            "Count whitespace-delimited words across all lines of `{file}`{restriction}; print only that number.",
            "Return the total word count of `{file}`{restriction}, not the line count, with no other output.",
        ),
    }
    fixtures = {"lines": [], "words": []}
    for length in (3, 6, 9):
        lines = [" ".join(f"word{rng.randrange(100)}" for _ in range(2 + i % 3)) for i in range(length)]
        setup = [write_file(path, "\n".join(lines) + "\n")]
        for kind, count in (("lines", length), ("words", sum(len(line.split()) for line in lines))):
            fixtures[kind].append({"setup": setup, "expect": {"number": count}})
    tasks = []
    for kind in prompts:
        wc = f"wc -{'l' if kind == 'lines' else 'w'} < {file}"
        wrong = f"wc -{'w' if kind == 'lines' else 'l'} < {file}"
        constant = f"printf '%s\\n' {fixtures[kind][0]['expect']['number']}"
        awk = f"awk 'END {{print NR}}' {file}" if kind == "lines" else f"awk '{{n += NF}} END {{print n+0}}' {file}"
        for restricted in (False, True):
            tasks.append(family.task(
                kind + ("-no-wc" if restricted else ""), prompts[kind], awk if restricted else wc,
                fixtures[kind], [wrong, constant, *([wc] if restricted else [])],
                forbid=("wc",) if restricted else (), file=path, restriction=" without using wc" if restricted else "",
            ))
    return tasks


def positions(family: Family, rng: random.Random) -> list[dict]:
    path, n = family.path("records"), rng.randint(2, 5)
    commands = {"last": f"tail -n {n} {shlex.quote(path)}", "from": f"sed -n '{n},$p' {shlex.quote(path)}"}
    prompts = {
        "last": (
            "Show the last {n} lines of `{file}`.",
            "Print only the final {n} records from `{file}`, preserving their order.",
            "I need the bottom {n} lines in `{file}`.",
            "Display the ending {n} lines, not lines starting at {n}, from `{file}`.",
        ),
        "from": (
            "Show `{file}` from line {n} through the end, counting the first line as 1.",
            "Print line {n} and every following line of `{file}`.",
            "Skip the first {skip} lines of `{file}` and display everything left.",
            "Display all records in `{file}` beginning at its one-based line {n}, inclusive.",
        ),
    }
    fixtures = {operation: [] for operation in commands}
    for length in (n + 2, n + 6, n * 3 + 1):
        lines = [f"record_{i}_{rng.randrange(1000)}" for i in range(length)]
        for operation in commands:
            expected = lines[-n:] if operation == "last" else lines[n - 1:]
            fixtures[operation].append({"setup": [write_file(path, "\n".join(lines) + "\n")], "expect": {"lines": expected}})
    return [family.task(operation, prompts[operation], command, fixtures[operation],
                        [commands["from" if operation == "last" else "last"]], file=path, n=n, skip=n - 1)
            for operation, command in commands.items()]


def matching(family: Family, rng: random.Random) -> list[dict]:
    path, word = family.path("messages"), rng.choice(("cedar", "amber", "stone", "birch"))
    fixture_lines = [
        [word, word + "wood", "blue " + word, "unrelated"],
        ["pre" + word, word + " end", "other", word + "_tag", "a " + word + " b"],
        ["different", word.upper(), word + "suffix", word, "left " + word],
    ]
    tasks = []
    for whole in (False, True):
        for invert in (False, True):
            flags = "-F" + ("w" if whole else "") + ("v" if invert else "")
            opposite = "-F" + ("" if whole else "w") + ("v" if invert else "")
            predicate = "a whole word" if whole else "a substring"
            action = "do not contain" if invert else "contain"
            fixtures = []
            for lines in fixture_lines:
                expected = [line for line in lines if ((word in line.split() if whole else word in line) != invert)]
                fixtures.append({"setup": [write_file(path, "\n".join(lines) + "\n")], "expect": {"lines": expected}})
            tasks.append(family.task(
                ("whole" if whole else "substring") + ("-invert" if invert else ""),
                (
                    "Print lines of `{file}` that {action} `{word}` as {predicate}; match case exactly.",
                    "Filter `{file}` to lines that {action} the case-sensitive text `{word}`, matched as {predicate}.",
                    "From `{file}`, keep only lines that {action} `{word}` as {predicate}, without ignoring case.",
                    "Show only the lines in `{file}` which {action} `{word}` as {predicate}. The match is case-sensitive.",
                ),
                f"grep {flags} -- {shlex.quote(word)} {shlex.quote(path)}", fixtures,
                [f"grep {opposite} -- {shlex.quote(word)} {shlex.quote(path)}"],
                file=path, word=word, action=action, predicate=predicate,
            ))
    return tasks


def fields(family: Family, rng: random.Random) -> list[dict]:
    path = family.path("tickets", ".csv")
    columns = rng.choice([
        ["state", "amount", "priority", "id"], ["priority", "id", "amount", "state"],
        ["amount", "state", "id", "priority"], ["id", "amount", "state", "priority"],
    ])
    positions = {name: columns.index(name) + 1 for name in columns}
    file = shlex.quote(path)
    condition = f'NR>1 && ${positions["state"]} == "open"'
    commands = {
        "select": f"awk -F, '{condition} {{print ${positions['id']}, ${positions['priority']}}}' {file}",
        "sum": f"awk -F, '{condition} {{n += ${positions['amount']}}} END {{print n+0}}' {file}",
    }
    prompts = {
        "select": (
            "`{file}` is unquoted comma-separated data with header columns `{schema}`. For open rows, print ID then priority separated by a space.",
            "Read `{file}` as CSV with columns `{schema}` and a header row. Output the ID and priority of rows whose state is open, space-separated.",
            "Select rows with state open from `{file}`; its first row is the header `{schema}` and fields are unquoted CSV. Print ID, then priority, using a space.",
            "From the unquoted CSV `{file}` with header `{schema}`, show only each open ticket's ID followed by its priority, separated by one space.",
        ),
        "sum": (
            "`{file}` is unquoted comma-separated data with header columns `{schema}`. Print the sum of amount for rows whose state is open.",
            "Read `{file}` as CSV with columns `{schema}` and a header row. Total the amount column for open rows and print only the number.",
            "Using unquoted CSV `{file}` with header `{schema}`, add the amounts for state open only. Output their sum.",
            "What is the combined amount of open tickets in `{file}`? It has unquoted CSV fields and header `{schema}`. Print just the total.",
        ),
    }
    fixtures = {operation: [] for operation in commands}
    for length in (3, 6, 8):
        rows = [{"id": f"T{100+i}", "state": "open" if i % 2 == 0 else "closed",
                 "priority": "high" if i % 3 == 0 else "low", "amount": str(rng.randint(2, 50))}
                for i in range(length)]
        content = ",".join(columns) + "\n" + "".join(",".join(row[name] for name in columns) + "\n" for row in rows)
        opened = [row for row in rows if row["state"] == "open"]
        fixtures["select"].append({"setup": [write_file(path, content)],
                                   "expect": {"lines": [f"{row['id']} {row['priority']}" for row in opened]}})
        fixtures["sum"].append({"setup": [write_file(path, content)],
                                "expect": {"number": sum(int(row["amount"]) for row in opened)}})
    negatives = {
        "select": [f"awk -F, '{condition} {{print $1, $2}}' {file}"],
        "sum": [f"awk -F, 'NR>1 {{n += ${positions['amount']}}} END {{print n+0}}' {file}"],
    }
    return [family.task(operation, prompts[operation], command, fixtures[operation], negatives[operation],
                        file=path, schema=",".join(columns)) for operation, command in commands.items()]


def transform(family: Family, rng: random.Random) -> list[dict]:
    path, keep = family.path("message"), family.path("untouched")
    file = shlex.quote(path)
    show = f"tr '[:lower:]' '[:upper:]' < {file}"
    edit = f"sed -i 's/.*/\\U&/' {file}"
    fixtures = {"show": [], "edit": []}
    for count in (2, 4, 6):
        content = "".join(f"Mixed words {rng.randrange(1000)}\n" for _ in range(count))
        setup = [write_file(path, content), write_file(keep, "leave Me alone\n")]
        fixtures["show"].append({"setup": setup, "expect": {"lines": content.upper().splitlines()}})
        fixtures["edit"].append({"setup": setup, "check": file_equals(path, content.upper()), "unchanged": [keep]})
    return [
        family.task("show", (
            "Display `{file}` in uppercase without modifying any files.",
            "Print an uppercase version of `{file}` but leave its contents on disk unchanged.",
            "Show the text from `{file}` with ASCII lowercase letters capitalized; do not save the change.",
            "Output all of `{file}` in uppercase, preserving the original file and all other files.",
        ), show, fixtures["show"], [edit], file=path),
        family.task("edit", (
            "Convert the ASCII text in `{file}` to uppercase in place; keep `{keep}` unchanged.",
            "Save an uppercase version back into `{file}` without changing `{keep}`.",
            "Capitalize all lowercase ASCII letters in `{file}` and replace that file's contents. Leave `{keep}` alone.",
            "Modify `{file}` itself so its text is uppercase, while preserving `{keep}` exactly.",
        ), edit, fixtures["edit"], [show, f"{show} > {file}", f"sed -i 's/.*/\\U&/' {file} {shlex.quote(keep)}"],
                    file=path, keep=keep),
    ]


def permissions(family: Family, rng: random.Random) -> list[dict]:
    path, keep = family.path("launch"), family.path("helper")
    file = shlex.quote(path)
    tasks = []
    for add in (False, True):
        change = "u+x" if add else "a-x"
        modes = (0o640, 0o604, 0o660) if add else (0o755, 0o751, 0o711)
        prompts = (
            "Add execute permission for only the owner of `{file}`, preserving all other mode bits and leaving `{keep}` unchanged.",
            "Make `{file}` executable by its owner only, without altering any other permissions or `{keep}`.",
            "Set just the owner's execute bit on `{file}`; keep the other mode bits and `{keep}` as they are.",
            "Enable owner execution on `{file}`, not group or other execution, and do not touch `{keep}` or other mode bits.",
        ) if add else (
            "Remove all execute permission bits from `{file}`, preserving other bits and leaving `{keep}` unchanged.",
            "Make `{file}` non-executable for everyone without modifying other permissions or `{keep}`.",
            "Clear owner, group and other execute bits on `{file}` only; preserve the rest and `{keep}`.",
            "Disable execution of `{file}` for all users, keep its read/write bits, and do not touch `{keep}`.",
        )
        fixtures = []
        for length, mode in zip((1, 3, 5), modes):
            expected = mode | 0o100 if add else mode & ~0o111
            fixtures.append({
                "setup": [write_file(path, "content\n" * length), write_file(keep, f"keep {rng.randrange(1000)}\n"),
                          f"chmod {mode:o} {file} {shlex.quote(keep)}"],
                "check": f'test "$(stat -c %a {file})" = {expected:o} && ' + file_equals(path, "content\n" * length),
                "unchanged": [keep],
            })
        tasks.append(family.task(
            "owner-execute" if add else "no-execute", prompts, f"chmod {change} {file}", fixtures,
            [f"chmod {change} {file} {shlex.quote(keep)}", f"chmod {'a+x' if add else 'u-x'} {file}",
             f"chmod {'740' if add else '644'} {file}"],
            file=path, keep=keep,
        ))
    return tasks


def filters(family: Family, rng: random.Random) -> list[dict]:
    directory, minimum = family.path("logs", ""), rng.choice((16, 32, 64))
    recursive = family.variant % 2 == 0
    root = shlex.quote(directory)
    expression = f"find {root}" + ("" if recursive else " -maxdepth 1") + f" -type f -name '*.log' -size +{minimum}c"
    broad = f"find {root}" + ("" if recursive else " -maxdepth 1") + " -type f -name '*.log'"
    prompts = {
        "list": (
            "List regular .log files in `{directory}`{scope} whose size is strictly greater than {minimum} bytes. Do not change files.",
            "Show paths of regular files ending in .log under `{directory}`{scope}, but only those larger than {minimum} bytes.",
            "Find regular .log files larger than {minimum} bytes in `{directory}`{scope}; only print their paths.",
            "Print which regular .log files in `{directory}`{scope} exceed {minimum} bytes, without modifying anything.",
        ),
        "delete": (
            "Delete only regular .log files in `{directory}`{scope} larger than {minimum} bytes. Keep all other files unchanged.",
            "Remove regular files ending in .log under `{directory}`{scope} if their size exceeds {minimum} bytes; preserve everything else.",
            "Clean up just regular .log files strictly bigger than {minimum} bytes in `{directory}`{scope}. Do not change other files.",
            "Erase regular .log files in `{directory}`{scope} with more than {minimum} bytes, leaving all remaining files untouched.",
        ),
    }
    fixtures = {operation: [] for operation in prompts}
    for extra in (1, 5, 13):
        contents = {
            f"{directory}/large_{extra}.log": "x" * (minimum + extra),
            f"{directory}/boundary_{extra}.log": "b" * minimum,
            f"{directory}/small_{extra}.log": "s",
            f"{directory}/large_{extra}.txt": "t" * (minimum + extra),
            f"{directory}/nested/large_{extra}.log": "n" * (minimum + extra),
        }
        wanted = [f"{directory}/large_{extra}.log", *([f"{directory}/nested/large_{extra}.log"] if recursive else [])]
        setup = [f"mkdir -p {shlex.quote(directory + '/nested')}",
                 *[write_file(path, text) for path, text in contents.items()]]
        fixtures["list"].append({"setup": setup, "expect": {"set": wanted}})
        fixtures["delete"].append({
            "setup": setup, "check": " && ".join(f"test ! -e {shlex.quote(path)}" for path in wanted),
            "unchanged": [path for path in contents if path not in wanted],
        })
    scope = ", including subdirectories," if recursive else ", only directly in that directory and not in subdirectories,"
    return [family.task(operation, prompts[operation], expression + (" -delete" if operation == "delete" else ""),
                        fixtures[operation], [broad + (" -delete" if operation == "delete" else "")],
                        directory=directory, scope=scope, minimum=minimum) for operation in prompts]


BUILDERS = (joins, counts, positions, matching, fields, transform, permissions, filters)


def build_tasks(variants: int = 8, seed: int = 0) -> list[dict]:
    if variants < 1:
        raise ValueError("variants must be positive")
    tasks = []
    for builder in BUILDERS:
        for template in range(len(SPLITS)):
            for variant in range(variants):
                family = Family(builder.__name__, template, variant, seed)
                rng = random.Random(f"{SOURCE}:{seed}:{family.category}:{template}:{variant}")
                tasks.extend(builder(family, rng))
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic precision tasks; does not execute commands or train a model.")
    parser.add_argument("--out", type=Path, required=True, help="new JSONL file; existing files are never overwritten")
    parser.add_argument("--variants", type=int, default=8, help="scenarios per template family, each with three fixtures")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    if args.variants < 1:
        parser.error("--variants must be positive")
    tasks = build_tasks(args.variants, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8", newline="\n") as out:
        for task in tasks:
            out.write(json.dumps(task, ensure_ascii=False) + "\n")
    print(f"{len(tasks)} unproved tasks -> {args.out}")
    print("splits:", dict(Counter(task["split"] for task in tasks)))
    print("Verify with scripts/prove_exec_tasks.py --tasks in an isolated execution container before preparing training data.")


if __name__ == "__main__":
    main()
