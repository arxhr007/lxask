"""Search over the docs index.

Search is command-first: pick the few commands the question is about, weighted
by how common each command is, then pull the best-matching examples of those.

With an embedding model the match is semantic ("make a script executable" finds
chmod). Without one it falls back to BM25 over the same index, which needs the
question to share words with the documentation.

The search is in the Rust core (lxask-core/src/retrieve.rs); this module gives it a
Python embedding model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import _core
from .index import Embed

STOPWORDS = _core.STOPWORDS
# Everyday wording -> the wording the documentation uses. Lexical search cannot
# bridge these on its own.
SYNONYMS: dict[str, tuple[str, ...]] = _core.SYNONYMS
# bge models are trained with this instruction on the query side only.
QUERY_PREFIX = _core.QUERY_PREFIX
PRIOR_WEIGHT = _core.PRIOR_WEIGHT
UNNAMED_PENALTY = _core.UNNAMED_PENALTY
N_COMMANDS = _core.N_COMMANDS
TOP_EXAMPLES = _core.TOP_EXAMPLES
OTHER_EXAMPLES = _core.OTHER_EXAMPLES


@dataclass(frozen=True)
class Doc:
    cmd: str
    title: str
    body: str
    source: str
    score: float  # lower is a better match (bm25, or negated cosine similarity)


def fts_query(question: str, synonyms: bool = True) -> str:
    """Turn a question into an FTS5 query that ORs its content words."""
    return _core.fts_query(question, synonyms)


class Retriever:
    def __init__(self, db_path: Path, embed: Embed | None = None, synonyms: bool = True):
        """`embed` turns on semantic search, provided the index was built with embeddings."""
        self._core = _core.Retriever(Path(db_path), embed, synonyms)

    @property
    def semantic(self) -> bool:
        return self._core.semantic

    @property
    def read_only(self) -> bool:
        return self._core.read_only

    def close(self) -> None:
        self._core.close()

    def commands(self, question: str, n: int = 4) -> list[str]:
        """The `n` commands whose documentation best matches the question."""
        return self._core.commands(question, n)

    def search(self, question: str, n_cmds: int = N_COMMANDS, top: int = TOP_EXAMPLES, per_cmd: int = OTHER_EXAMPLES) -> list[Doc]:
        """Examples for the `n_cmds` best commands: `top` for the best one, `per_cmd` for each other."""
        return [Doc(*row) for row in self._core.search(question, n_cmds, top, per_cmd)]
