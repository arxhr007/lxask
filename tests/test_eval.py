import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval as evaluation
from lxask.answer import Step
from lxask.validate import build_catalog


@pytest.fixture
def catalog():
    return build_catalog(["ls", "find", "echo"], [])


@pytest.mark.parametrize("name", ["regressions", "forbidden", "forbidden_heldout", "ambiguous"])
@pytest.mark.parametrize("steps", [
    [],
    [Step("list files")],
    [Step("list files", " \t\n")],
    [Step("list files", "ls"), Step("show their sizes")],
])
def test_missing_or_partial_answers_cannot_pass(name, steps, catalog):
    result = evaluation.score({"set": name, "q": "list files"}, steps, catalog)
    assert result["answered"] is False
    assert result["pass"] is False
    if "complete" in result:
        assert result["complete"] is False


def test_answered_restriction_task_still_passes(catalog):
    case = {"set": "forbidden_heldout", "q": "list files without find", "forbid": ["find"]}
    assert evaluation.score(case, [Step(case["q"], "ls")], catalog)["pass"] is True
    assert evaluation.score(case, [Step(case["q"], "find .")], catalog)["pass"] is False


def test_program_restrictions_do_not_erase_other_violations(catalog):
    case = {"set": "regressions", "q": "list names", "must_not": [r"echo"], "forbid": ["find"]}
    result = evaluation.score(case, [Step(case["q"], "echo wrong")], catalog)
    assert result["violations"] is True
    assert result["pass"] is False


def test_expected_clarification_is_not_an_empty_answer_failure(catalog):
    case = {"set": "ambiguous", "q": "add a proxy", "clarify": True}
    result = evaluation.score(case, [], catalog, asked=True)
    assert result["pass"] is True
    assert result["answered"] is False
    assert evaluation.score(case, [], catalog)["pass"] is False


def test_unexpected_clarification_cannot_pass(catalog):
    case = {"set": "forbidden_heldout", "q": "list files without find", "forbid": ["find"]}
    assert evaluation.score(case, [Step(case["q"], "ls")], catalog, asked=True)["pass"] is False
