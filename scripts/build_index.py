"""Build the docs index from the sources in data/raw (see fetch_sources.py).

The index goes to the lxask data directory (~/.local/share/lxask/docs.sqlite),
where the CLI looks for it.
"""

from __future__ import annotations

import argparse
import math
from collections import Counter
from pathlib import Path
from typing import Iterator

import sources
from lxask import config
from lxask.index import Row, add_embeddings, build_index, man_summary, program, tldr_rows
from lxask.validate import save_catalog

# How much a command's commonness lifts it over an equally well-matching obscure one.
INSTALLED_BONUS = 0.5  # it ships with a stock Ubuntu install (has a man page there)
USAGE_BONUS = 1.0  # at most, for the commands people ask about most (NL2Bash)
USAGE_SATURATION = 1000  # occurrences at which the usage bonus tops out


def man_rows(pages: list[dict], have_summary: set[str]) -> Iterator[Row]:
    """A searchable entry for each command that only a man page documents. Man option
    text is not searched (it added prompt length and no recall); it goes into the
    catalogue, where `lxask.explain` reads it."""
    for page in pages:
        if page["name"] not in have_summary and (summary := man_summary(page["text"])):
            yield Row(page["name"], summary, "", "summary")


def command_priors(programs: set[str], installed: set[str], usage: Counter[str]) -> dict[str, float]:
    priors = {}
    for name in programs:
        used = math.log1p(usage[name]) / math.log1p(USAGE_SATURATION)
        priors[name] = 1.0 + INSTALLED_BONUS * (name in installed) + USAGE_BONUS * min(1.0, used)
    return priors


def write_catalog(raw: Path, out: Path) -> None:
    catalog = sources.load_catalog(raw)
    save_catalog(out, catalog)
    options = sum(len(flags) for flags in catalog.options.values())
    described = sum(len(texts) for texts in catalog.texts.values())
    print(f"catalogue: {len(catalog.programs)} programs, {options} options ({described} described), "
          f"{len(catalog.summaries)} summaries")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=sources.RAW)
    parser.add_argument("--out", type=Path, default=config.data_home() / config.INDEX_FILENAME)
    parser.add_argument("--embed-model", type=Path, default=config.embed_model_path())
    parser.add_argument("--no-embeddings", action="store_true", help="keyword search only")
    parser.add_argument("--catalog-only", action="store_true",
                        help="only (re)write the catalogue of programs and options into an existing index")
    args = parser.parse_args()

    if args.catalog_only:
        write_catalog(args.raw, args.out)
        return

    tldr = sources.load_tldr_pages(args.raw)
    man = list(sources.load_man(args.raw))
    rows = [row for page in tldr.values() for row in tldr_rows(page)]
    rows.extend(man_rows(man, have_summary=set(tldr)))

    priors = command_priors(
        programs={program(row.cmd) for row in rows},
        installed={page["name"] for page in man},
        usage=sources.usage_counts(args.raw),
    )
    count = build_index(args.out, rows, priors)

    by_source = Counter(row.source for row in rows)
    print(f"{count} rows -> {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")
    for source, n in sorted(by_source.items()):
        print(f"  {source}: {n}")
    top = sorted(priors.items(), key=lambda kv: -kv[1])[:12]
    print("  highest priors: " + ", ".join(f"{name} {value:.2f}" for name, value in top))

    write_catalog(args.raw, args.out)

    if args.no_embeddings:
        return
    if not args.embed_model.exists():
        print(f"no embedding model at {args.embed_model}: keyword search only "
              "(run `scripts/fetch_models.py embed`, then rebuild)")
        return
    from lxask.llm import Embedder

    print("embedding the entries, this takes a few minutes on CPU ...", flush=True)
    embedded = add_embeddings(args.out, Embedder(args.embed_model), args.embed_model.name)
    print(f"{embedded} entries embedded ({args.out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
