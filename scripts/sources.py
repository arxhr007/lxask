"""Read the raw sources in data/raw (see fetch_sources.py). Standard library only."""

from __future__ import annotations

import gzip
import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterator

import nl2bash
from lxask.index import TldrPage, man_option_docs, man_subcommands, man_summary, parse_tldr_page, strip_placeholders
from lxask.validate import VERB_PROGRAMS, Catalog, build_catalog

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"

# Later platforms win when a command has a page in both.
TLDR_PLATFORMS = ("common", "linux")

NOT_A_COMMAND = re.compile(r"@|\.(service|socket|target|timer|mount|path|slice|scope|py|pl|sh)$")


def load_tldr_pages(raw: Path = RAW) -> dict[str, TldrPage]:
    pages: dict[str, TldrPage] = {}
    for platform in TLDR_PLATFORMS:
        for path in sorted((raw / "tldr" / platform).glob("*.md")):
            page = parse_tldr_page(path.read_text(encoding="utf-8"))
            if page and page.examples:
                pages[page.name] = page
    return pages


def load_man(raw: Path = RAW) -> Iterator[dict]:
    """Man pages of commands; unit files and scripts (`foo.service`, `x.py`) are left out."""
    path = raw / "man.jsonl.gz"
    if not path.exists():
        return
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            page = json.loads(line)
            if not NOT_A_COMMAND.search(page["name"]):
                yield page


def usage_counts(raw: Path = RAW) -> Counter[str]:
    """How often each program appears in NL2Bash, leaving out the held-out commands."""
    counts: Counter[str] = Counter()
    if not (raw / "nl2bash" / "all.cm").exists():
        return counts
    for _, command in nl2bash.load_pairs(raw / "nl2bash"):
        if not nl2bash.is_eval(command):
            counts.update(nl2bash.utilities(command))
    return counts


def load_catalog(raw: Path = RAW) -> Catalog:
    """What the documentation on disk knows, for `lxask.validate` and `lxask.explain`.

    Programs: every tldr page and man page. Options: the man pages' option headings
    with their descriptions, widened by the options the tldr examples use (documented
    usage too). Summaries: the tldr page's, else the man page's NAME line. Verbs: the
    COMMANDS section of the programs that take one first (systemctl, loginctl, ...).
    """
    pages = load_tldr_pages(raw)
    man = list(load_man(raw))
    options = ((page["name"], title, text) for page in man for title, text in man_option_docs(page["text"]))
    examples = (strip_placeholders(command) for page in pages.values() for _, command in page.examples)
    summaries = {page["name"]: summary for page in man if (summary := man_summary(page["text"]))}
    summaries.update({name: page.summary for name, page in pages.items() if page.summary})
    catalog = build_catalog([*pages, *(page["name"] for page in man)], options, examples, summaries)
    for page in man:
        if page["name"] in VERB_PROGRAMS:
            for verb in man_subcommands(page["text"]):
                catalog.add_subcommand(page["name"], verb)
    return catalog


def core_programs(raw: Path = RAW, min_usage: int = 3) -> set[str]:
    """Programs worth extra training data: on a stock install, or commonly asked about."""
    installed = {page["name"] for page in load_man(raw)}
    used = {name for name, count in usage_counts(raw).items() if count >= min_usage}
    return installed | used
