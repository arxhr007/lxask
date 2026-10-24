"""Parse tldr and man pages and build the SQLite FTS5 docs index."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, Mapping

from . import _core

if TYPE_CHECKING:  # numpy is only needed to embed; parsing stays standard-library
    import numpy as np

# `docs` holds one entry per example or option. `pages` holds one entry per
# command, used to decide which commands a question is about; `prior` is how
# common the command is, so `find` can outrank an obscure tool with similar text.
SCHEMA = (
    """
    CREATE VIRTUAL TABLE docs USING fts5(
        cmd, title, body, source UNINDEXED,
        tokenize = 'porter unicode61'
    )
    """,
    """
    CREATE VIRTUAL TABLE pages USING fts5(
        cmd, text, prior UNINDEXED,
        tokenize = 'porter unicode61'
    )
    """,
)

EMBEDDINGS_SCHEMA = """
CREATE TABLE embeddings (
    model TEXT PRIMARY KEY, dim INTEGER NOT NULL, rowids BLOB NOT NULL, vectors BLOB NOT NULL
)
"""

# What `lxask.validate` and `lxask.explain` need at answer time, so the CLI can check
# and describe a command without the raw documentation. Written and read by the Rust
# core (lxask.validate.save_catalog / load_catalog).
CATALOG_SCHEMA = _core.CATALOG_SCHEMA

MAN_CHUNK_CHARS = 400

# Texts in, one unit-length float32 vector per text out.
Embed = Callable[[list[str]], "np.ndarray"]


@dataclass(frozen=True)
class Row:
    cmd: str
    title: str  # what the entry does, in plain language
    body: str  # the command line (tldr) or the option text (man)
    source: str  # "tldr" | "summary" | "man"


@dataclass
class TldrPage:
    name: str
    summary: str
    # (description, command with {{placeholders}} left in)
    examples: list[tuple[str, str]] = field(default_factory=list)


_MNEMONIC = re.compile(r"\[(\w)\]")
_OPTION_ALT = re.compile(r"\{\{\[([^|\]]+)\|([^\]]+)\]\}\}")
_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_SUMMARY_SKIP = ("more information", "see also")


def clean_description(text: str) -> str:
    """`[c]reate an archive:` -> `create an archive`."""
    return _MNEMONIC.sub(r"\1", text).strip().rstrip(":").strip()


def strip_placeholders(command: str) -> str:
    """`tar {{[-x|--extract]}} -f {{path/to/file}}` -> `tar --extract -f path/to/file`."""
    command = _OPTION_ALT.sub(r"\2", command)
    return _PLACEHOLDER.sub(r"\1", command)


def parse_tldr_page(text: str) -> TldrPage | None:
    name = None
    summary: list[str] = []
    examples: list[tuple[str, str]] = []
    pending = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# ") and name is None:
            name = line[2:].strip()
        elif line.startswith("> "):
            sentence = line[2:].strip()
            if not sentence.lower().startswith(_SUMMARY_SKIP):
                summary.append(sentence)
        elif line.startswith("- "):
            pending = clean_description(line[2:])
        elif pending is not None and len(line) > 2 and line[0] == "`" == line[-1]:
            examples.append((pending, line[1:-1]))
            pending = None
    if not name:
        return None
    return TldrPage(name=name, summary=" ".join(summary), examples=examples)


def tldr_rows(page: TldrPage) -> Iterator[Row]:
    if page.summary:
        yield Row(page.name, page.summary, "", "summary")
    for description, command in page.examples:
        yield Row(page.name, description, strip_placeholders(command), "tldr")


_OPTION_HEAD = re.compile(r"-{1,2}[\w?]")
# Headings usually sit 7 columns in, but nested lists (ss, ip) put them at 14.
_HEADING_INDENT = 16
_OPTION_HEADING = re.compile(r"-{1,2}[\w?]|-,\s*-")  # also su(1)'s `-, -l, --login`
_MAN_NAME = re.compile(r"^NAME\n\s+(.+?)\n\s*\n", re.M | re.S)


def man_summary(text: str) -> str | None:
    """`ls - list directory contents` from the NAME section, without the name."""
    match = _MAN_NAME.search(text)
    if not match:
        return None
    line = re.sub(r"\s+", " ", match.group(1)).strip()
    _, sep, description = line.partition(" - ")
    return description.strip() if sep else None


def man_option_titles(text: str) -> Iterator[str]:
    """Every option heading in a man page (`-a, --all`), described or not.

    Wider than `man_option_rows`, which needs a description in the same paragraph:
    sed(1) describes each option in a paragraph of its own, grep(1) puts headings
    straight under a sub-section title. Headings are told from prose by position:
    they start a line near the left margin, descriptions are indented further.
    """
    for title, _ in man_option_docs(text):
        yield title


def man_option_docs(text: str) -> Iterator[tuple[str, str]]:
    """(heading, first paragraph of its description) for every option in a man page.

    The description is whatever follows the heading at a deeper indent: on the same
    line, on the next lines, or after one blank line (sed(1)). "" when there is none.

    A wrapped line of prose can start with an option too (find(1): "-delete action in
    the case the file has disappeared since the parent directory  was"). Such a line
    continues the prose above it at the same indent, and the prose goes on below it at
    that indent or stops; a heading below prose (a nested list) has its description
    indented deeper. Such a line still yields its title, with no description: the
    validator counts a flag the prose mentions as documented (crontab(1) has `-u` only
    in prose), but the explainer must not take the rest of the sentence for its meaning.
    """
    lines = text.splitlines()

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip())

    for i, line in enumerate(lines):
        stripped = line.strip()
        indent = indent_of(line)
        if indent > _HEADING_INDENT or not _OPTION_HEADING.match(stripped):
            continue
        above = lines[i - 1] if i else ""
        below = lines[i + 1] if i + 1 < len(lines) else ""
        title, *parts = re.split(r"\s{2,}", stripped, maxsplit=1)
        if (above.strip() and indent_of(above) == indent and not _OPTION_HEADING.match(above.strip())
                and not (below.strip() and indent_of(below) > indent)):
            yield title, ""
            continue
        may_skip_blank = not parts
        for following in lines[i + 1 :]:
            if not following.strip():
                if parts or not may_skip_blank:
                    break
                may_skip_blank = False
                continue
            if len(following) - len(following.lstrip()) <= indent:
                break  # the next heading, or text that belongs to something else
            parts.append(following.strip())
        yield title, join_wrapped(parts)[:MAN_CHUNK_CHARS]


_COMMANDS_SECTION = re.compile(r"^COMMANDS\n(.*?)(?=^[A-Z][A-Z ]+$|\Z)", re.M | re.S)
_VERB = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?=\s|,|$)")
_STACKED_HEADINGS = 3  # `enable UNIT...` over `enable PATH...`, sharing one description


def man_subcommands(text: str) -> list[str]:
    """The verbs a man page's COMMANDS section lists: `start`, `list-units` from
    systemctl(1)'s `start PATTERN...`, `list-units [PATTERN...]`.

    A heading starts a line near the left margin, and its description is indented
    below it; a few headings can share one description. Prose at a heading's indent
    lets a stray word or two through, which only loosens the check (`lxask.validate`
    flags a verb that is not here), never tightens it.
    """
    match = _COMMANDS_SECTION.search(text)
    if not match:
        return []
    lines = match.group(1).splitlines()

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip())

    verbs: list[str] = []
    for i, line in enumerate(lines):
        indent = indent_of(line)
        if not line.strip() or indent > _HEADING_INDENT:
            continue
        described = False
        for following in lines[i + 1 : i + 1 + _STACKED_HEADINGS]:
            if not following.strip() or indent_of(following) < indent:
                break
            if indent_of(following) > indent:
                described = True
                break
        if not described:
            continue
        for piece in re.split(r",\s+", re.split(r"\s{2,}", line.strip())[0]):
            verb = _VERB.match(piece)
            if verb and verb.group(0) not in verbs:
                verbs.append(verb.group(0))
    return verbs


def join_wrapped(lines: list[str]) -> str:
    """Lines of man text as one line with single spaces. A word that man hyphenated at
    a line end (`lev‐` + `els`, with U+2010) is joined again; a hyphen inside a line
    (`user‐ and groupname`) is left alone."""
    text = ""
    for line in lines:
        if re.search(r"\w‐$", text):
            text = text[:-1] + line
        else:
            text = f"{text} {line}" if text else line
    return re.sub(r"\s+", " ", text)


def man_option_rows(cmd: str, text: str) -> Iterator[Row]:
    """One row per documented option: the paragraphs that start with a flag."""
    for paragraph in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in paragraph.splitlines() if ln.strip()]
        if not lines or not _OPTION_HEAD.match(lines[0]):
            continue
        head, *inline = re.split(r"\s{2,}", lines[0], maxsplit=1)
        description = " ".join(inline + lines[1:])
        if not description:
            continue
        description = re.sub(r"\s+", " ", description)[:MAN_CHUNK_CHARS]
        yield Row(cmd, head, description, "man")


def program(cmd: str) -> str:
    """`git commit` -> `git`: the executable behind a (sub)command page."""
    return _core.program(cmd)


def embedding_text(cmd: str, title: str) -> str:
    """What an entry looks like to the embedding model."""
    return f"{cmd}: {title}"


def add_embeddings(db_path: Path, embed: Embed, model: str, batch: int = 64) -> int:
    """Embed every tldr example and summary and store the matrix in the index.

    One row holds the whole matrix (float16, unit-length rows) and the docs rowid
    of each vector, so loading it is a single read.
    """
    import numpy as np

    con = sqlite3.connect(db_path)
    try:
        entries = con.execute("SELECT rowid, cmd, title FROM docs WHERE source != 'man' ORDER BY rowid").fetchall()
        texts = [embedding_text(cmd, title) for _, cmd, title in entries]
        chunks = [embed(texts[i : i + batch]) for i in range(0, len(texts), batch)]
        vectors = np.concatenate(chunks).astype(np.float16)
        rowids = np.array([rowid for rowid, _, _ in entries], dtype=np.int32)

        con.execute("DROP TABLE IF EXISTS embeddings")
        con.execute(EMBEDDINGS_SCHEMA)
        con.execute(
            "INSERT INTO embeddings (model, dim, rowids, vectors) VALUES (?, ?, ?, ?)",
            (model, vectors.shape[1], rowids.tobytes(), vectors.tobytes()),
        )
        con.commit()
    finally:
        con.close()
    return len(entries)


def build_index(db_path: Path, rows: Iterable[Row], priors: Mapping[str, float] | None = None) -> int:
    """Write the index. `priors` maps a program name to a weight >= 1 (default 1)."""
    priors = priors or {}
    values = [(r.cmd, r.title, r.body, r.source) for r in rows]

    page_text: dict[str, list[str]] = {}
    for cmd, title, _, source in values:
        if source != "man":
            page_text.setdefault(cmd, []).append(title)
    pages = [(cmd, " ".join(text), priors.get(program(cmd), 1.0)) for cmd, text in page_text.items()]

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    try:
        for statement in SCHEMA:
            con.execute(statement)
        con.executemany("INSERT INTO docs (cmd, title, body, source) VALUES (?, ?, ?, ?)", values)
        con.executemany("INSERT INTO pages (cmd, text, prior) VALUES (?, ?, ?)", pages)
        con.execute("INSERT INTO docs (docs) VALUES ('optimize')")
        con.execute("INSERT INTO pages (pages) VALUES ('optimize')")
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    return len(values)
