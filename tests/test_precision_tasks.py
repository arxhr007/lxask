import copy
import sys
from collections import defaultdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_precision_tasks import BUILDERS, build_tasks
from execution_tasks import is_proved, proved_record, task_record, task_variants
from prove_exec_tasks import prove


def test_generation_is_deterministic_and_has_unique_questions_and_ids():
    tasks = build_tasks(variants=2, seed=7)
    assert tasks == build_tasks(variants=2, seed=7)
    assert tasks != build_tasks(variants=2, seed=8)
    assert len({task["id"] for task in tasks}) == len(tasks)
    assert len({task["q"] for task in tasks}) == len(tasks)


def test_template_families_cannot_cross_splits():
    groups = defaultdict(set)
    categories = defaultdict(set)
    for task in build_tasks(variants=2):
        groups[task["family"]].add(task["split"])
        categories[task["split"]].add(task["category"])
    assert all(len(splits) == 1 for splits in groups.values())
    assert set(categories) == {"train", "dev", "test"}
    assert all(names == {builder.__name__ for builder in BUILDERS} for names in categories.values())


def test_all_tasks_have_multiple_fixtures_and_meaningful_negatives():
    for task in build_tasks(variants=1):
        assert len(task["fixtures"]) == 3
        assert task["negatives"] and task["reference"] not in task["negatives"]
        assert len({repr(fixture["setup"]) for fixture in task["fixtures"]}) == 3
        assert all(fixture.get("expect") or fixture.get("check") for fixture in task["fixtures"])
        assert "\n" not in task["reference"]
        assert is_proved(task) is False


def test_fixture_constraints_are_additive_and_cannot_override_the_request():
    task = {"q": "keep things", "forbid": ["wc"], "unchanged": ["first"],
            "fixtures": [{"expect": {"number": 1}, "unchanged": ["second"]}]}
    variant, = task_variants(task)
    assert variant["unchanged"] == ["first", "second"]
    assert variant["forbid"] == ["wc"]
    assert variant["q"] == task["q"]
    for field in ("q", "forbid", "exit_ok", "reference"):
        with pytest.raises(ValueError, match="fixture may only"):
            task_variants({**task, "fixtures": [{field: []}]})


@pytest.mark.parametrize("fixtures", [[], {}, "fixture"])
def test_malformed_fixture_sets_are_rejected(fixtures):
    with pytest.raises(ValueError, match="nonempty list"):
        task_variants({"fixtures": fixtures})


def test_proof_is_invalidated_by_any_training_or_checker_change():
    task = proved_record(build_tasks(variants=1)[0])
    assert is_proved(task)
    for key, value in (("q", "a different task"), ("reference", "false"), ("negatives", []), ("split", "test")):
        assert not is_proved({**task, key: value})
    changed = copy.deepcopy(task)
    changed["fixtures"][0]["check"] = "true"
    assert not is_proved(changed)
    assert "proof" not in task_record(task)
    assert task_record(task)["fixtures"] == task["fixtures"]


def test_proving_runs_references_and_rejects_wrong_answers_without_needing_a_model(monkeypatch):
    import prove_exec_tasks

    task = build_tasks(variants=1)[0]
    called = []

    def attempt(record, command):
        called.append(command)
        return {"success": command == task["reference"]}

    monkeypatch.setattr(prove_exec_tasks, "attempt", attempt)
    assert prove(task) == ""
    assert called.count(task["reference"]) == 2
    assert "true" in called and "" in called
    assert all(negative in called for negative in task["negatives"])


def test_invalid_variant_count_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        build_tasks(variants=0)
