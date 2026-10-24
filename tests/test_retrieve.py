import re
import zlib

import numpy as np
import pytest

from lxask.index import Row, add_embeddings, build_index
from lxask.retrieve import QUERY_PREFIX, STOPWORDS, Retriever, fts_query

ROWS = [
    Row("tar", "Archiving utility.", "", "summary"),
    Row("tar", "extract a compressed archive into the current directory", "tar xf path/to/source.tar.gz", "tldr"),
    Row("tar", "create a gzipped archive from files", "tar czf path/to/target.tar.gz path/to/file", "tldr"),
    Row("tar", "list the contents of an archive", "tar tvf path/to/source.tar", "tldr"),
    Row("tar", "-C, --directory=DIR", "change to DIR before extracting the archive", "man"),
    Row("atool", "extract an archive", "atool --extract archive.tar.gz", "tldr"),
    Row("find", "find files larger than a given size", "find path/to/dir -size +500k", "tldr"),
    Row("find", "find files by extension", "find path/to/dir -name '*.ext'", "tldr"),
    Row("rmlint", "find duplicate files bigger than a size", "rmlint --size 1MB path", "tldr"),
    Row("du", "show the disk usage of each directory in human-readable form", "du -h --max-depth=1", "tldr"),
    Row("git commit", "commit staged files with a message", "git commit -m message", "tldr"),
    Row("git-commit", "-m <msg>, --message=<msg>", "use the given message as the commit message", "man"),
    Row("lsblk", "list block devices", "", "summary"),
    Row("ls", "-h, --human-readable", "with -l and -s, print sizes like 1K 234M 2G etc.", "man"),
]

PRIORS = {"tar": 2.5, "find": 2.5, "du": 2.0, "git": 2.0}


@pytest.fixture
def retriever(tmp_path):
    db = tmp_path / "docs.sqlite"
    assert build_index(db, ROWS, PRIORS) == len(ROWS)
    r = Retriever(db)
    yield r
    r.close()


def test_fts_query_drops_stopwords_and_duplicates():
    assert fts_query("How do I find the files, files newer than 2 days?") == (
        '"find" OR "files" OR "newer" OR "days"'
    )


def test_fts_query_adds_documentation_wording():
    assert fts_query("delete big folders") == (
        '"delete" OR "remove" OR "big" OR "large" OR "folders" OR "directory"'
    )
    assert fts_query("delete big folders", synonyms=False) == '"delete" OR "big" OR "folders"'


def test_fts_query_survives_fts5_syntax_characters():
    assert fts_query('what does "tar -xzf" AND (NOT x*) do') == '"tar" OR "xzf" OR "not"'


def test_empty_question_returns_nothing(retriever):
    assert retriever.commands("how do I") == []
    assert retriever.search("how do I") == []


def test_common_command_outranks_obscure_one_with_similar_text(retriever):
    assert retriever.commands("extract an archive")[0] == "tar"


def test_synonym_bridges_question_and_documentation_wording(retriever):
    docs = retriever.search("find files bigger than 1GB")
    assert docs[0].cmd == "find"
    assert docs[0].body == "find path/to/dir -size +500k"


def test_best_command_gets_more_examples_than_the_others(retriever):
    docs = retriever.search("tar archive extract create list contents", n_cmds=2, top=2, per_cmd=1)
    assert [d.cmd for d in docs] == ["tar", "tar", "atool"]


def test_summary_is_used_only_when_no_example_matches(retriever):
    docs = retriever.search("list block devices", n_cmds=1)
    assert [(d.cmd, d.source) for d in docs] == [("lsblk", "summary")]
    assert all(d.source != "summary" for d in retriever.search("extract tar archive", n_cmds=1))


def test_man_option_rows_never_reach_the_notes(retriever):
    # Option text is for lxask.explain; in the prompt it only cost length.
    docs = retriever.search("extract a tar archive into another directory", n_cmds=4, top=4, per_cmd=4)
    assert docs and all(d.source != "man" for d in docs)


def test_index_is_opened_read_only(retriever, tmp_path):
    assert retriever.read_only
    with pytest.raises(RuntimeError):
        Retriever(tmp_path / "missing.sqlite")  # opening never creates an index
    assert not (tmp_path / "missing.sqlite").exists()


# Semantic search, with a stand-in for the embedding model.


def fake_embed(texts):
    """Bag of words hashed into 512 dimensions: ranks by shared wording, needs no model."""
    out = np.zeros((len(texts), 512), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in re.findall(r"[a-z0-9]+", text.removeprefix(QUERY_PREFIX).lower()):
            if word not in STOPWORDS:
                out[i, zlib.crc32(word.encode()) % 512] += 1.0
        out[i] /= np.linalg.norm(out[i]) or 1.0
    return out


@pytest.fixture
def semantic(tmp_path):
    db = tmp_path / "docs.sqlite"
    build_index(db, ROWS, PRIORS)
    embedded = add_embeddings(db, fake_embed, "fake", batch=4)
    assert embedded == sum(row.source != "man" for row in ROWS)
    r = Retriever(db, embed=fake_embed)
    yield r
    r.close()


def test_semantic_only_with_both_a_model_and_embeddings(retriever, semantic, tmp_path):
    assert semantic.semantic
    assert not retriever.semantic  # no model given

    db = tmp_path / "plain.sqlite"
    build_index(db, ROWS, PRIORS)
    plain = Retriever(db, embed=fake_embed)  # model given, index has no embeddings
    assert not plain.semantic
    assert plain.commands("extract an archive")[0] == "tar"
    plain.close()


def test_semantic_prior_lets_the_common_command_beat_a_closer_obscure_one(semantic):
    # rmlint's entry shares more of the wording, but find is far more common.
    assert semantic.commands("find files bigger than a size", n=2) == ["find", "rmlint"]


def test_semantic_uncommon_tool_is_held_back_unless_named(semantic):
    # atool's entry is the closer match, but nothing says atool is common.
    assert semantic.commands("extract an archive")[0] == "tar"
    assert semantic.commands("extract an archive with atool.")[0] == "atool"


def test_semantic_search_returns_best_examples_per_command(semantic):
    docs = semantic.search("extract a compressed archive", n_cmds=2, top=2, per_cmd=1)
    assert [d.cmd for d in docs] == ["tar", "tar", "atool"]
    assert docs[0].body == "tar xf path/to/source.tar.gz"
    assert all(d.source == "tldr" for d in docs)
    assert docs[0].score < docs[1].score < 0


def test_semantic_search_uses_summary_for_commands_without_examples(semantic):
    docs = semantic.search("list block devices", n_cmds=1)
    assert [(d.cmd, d.source) for d in docs] == [("lsblk", "summary")]


def test_semantic_blank_question_returns_nothing(semantic):
    assert semantic.commands("  ") == []
    assert semantic.search("  ") == []
