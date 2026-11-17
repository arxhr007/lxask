import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_precision_tasks import build_tasks
from execution_tasks import proved_record
from lxask.prompt import build_messages, format_answer
from lxask.retrieve import Doc
from prepare_precision import prepare, question, write_bundle


@pytest.fixture
def tasks():
    return [proved_record(task) for task in build_tasks(variants=1)]


def replay_record(q, *, kind="answer", **metadata):
    return {"kind": kind, "messages": build_messages(q, []) + [{"role": "assistant", "content": format_answer("echo replay")}],
            **metadata}


def test_preparation_is_deterministic_and_never_trains_on_dev_or_test(tasks):
    replay = [replay_record(f"old question {i}", kind="split" if i % 4 == 0 else "answer") for i in range(100)]
    bundle = prepare(tasks, replay, replay_ratio=1, seed=3)
    assert bundle == prepare(tasks, replay, replay_ratio=1, seed=3)
    assert set(bundle) == {"train.jsonl", "val.jsonl", "train.tasks.jsonl", "dev.tasks.jsonl", "test.tasks.jsonl",
                           "rl_train.jsonl", "rl_dev.jsonl"}
    training = {record["id"] for record in bundle["train.jsonl"] if "id" in record}
    assert training == {task["id"] for task in tasks if task["split"] == "train"}
    assert not training & {task["id"] for task in tasks if task["split"] != "train"}
    assert len(bundle["train.jsonl"]) == len(training) * 2
    assert any(record.get("kind") == "split" for record in bundle["train.jsonl"])


def test_unproved_and_modified_tasks_cannot_become_training_data(tasks):
    unproved = copy.deepcopy(tasks)
    unproved[0].pop("proof")
    with pytest.raises(ValueError, match="unproved or modified"):
        prepare(unproved, [], replay_ratio=0)
    modified = copy.deepcopy(tasks)
    modified[0]["fixtures"][0]["check"] = "true"
    with pytest.raises(ValueError, match="unproved or modified"):
        prepare(modified, [], replay_ratio=0)


def test_family_leakage_is_rejected_even_if_individual_tasks_are_proved(tasks):
    changed = copy.deepcopy(tasks)
    changed[0]["split"] = "test"
    changed[0] = proved_record(changed[0])
    with pytest.raises(ValueError, match="family crosses"):
        prepare(changed, [], replay_ratio=0)


def test_duplicate_ids_and_questions_are_rejected(tasks):
    with pytest.raises(ValueError, match="IDs"):
        prepare(tasks + [tasks[0]], [], replay_ratio=0)
    extra = proved_record({**tasks[0], "id": "different-id"})
    with pytest.raises(ValueError, match="questions"):
        prepare(tasks + [extra], [], replay_ratio=0)


def test_replay_cannot_reintroduce_heldout_questions_or_families(tasks):
    held = next(task for task in tasks if task["split"] == "test")
    replay = [
        replay_record(held["q"]),
        replay_record("another wording", family=held["family"]),
        replay_record("a tagged test", split="test"),
        replay_record("eligible old task"),
    ]
    bundle = prepare(tasks, replay)
    old = [record for record in bundle["train.jsonl"] if record.get("replay")]
    assert [question(record) for record in old] == ["eligible old task"]


def test_validation_replay_is_disjoint_from_training_replay(tasks):
    replay = [replay_record("training question", family="old-family")]
    replay_val = [replay_record("training question"), replay_record("variant", family="old-family"),
                  replay_record("validation question", split="dev")]
    bundle = prepare(tasks, replay, replay_val=replay_val)
    old_val = [question(record) for record in bundle["val.jsonl"] if record.get("replay")]
    assert old_val == ["validation question"]


def test_retrieval_filters_ruled_out_tools_and_never_sees_fixture_contents(tasks):
    docs = [Doc("wc", "count lines", "wc -l file", "tldr", 0), Doc("awk", "count records", "awk 'END {print NR}' file", "tldr", 1)]
    seen = []

    def search(q):
        seen.append(q)
        return docs

    bundle = prepare(tasks, [], replay_ratio=0, search=search)
    assert set(seen) == {task["q"] for task in tasks if task["split"] != "test"}
    for row in bundle["rl_train.jsonl"]:
        assert row["task"]["fixtures"]
        user = row["messages"][-1]["content"]
        assert "printf %s" not in user
        if row["task"].get("forbid"):
            assert "- wc:" not in user
            assert "- awk:" in user


@pytest.mark.parametrize("ratio", [-1, float("inf"), float("nan")])
def test_invalid_replay_ratios_are_rejected(tasks, ratio):
    with pytest.raises(ValueError, match="ratio"):
        prepare(tasks, [], replay_ratio=ratio)


def test_replay_cannot_silently_disappear(tasks):
    with pytest.raises(ValueError, match="no eligible replay"):
        prepare(tasks, [])
    assert prepare(tasks, [], replay_ratio=0)["train.jsonl"]


def test_cached_teacher_replay_keeps_its_provenance(tasks):
    bundle = prepare(tasks, [replay_record("cached baseline answer", replay="teacher")])
    replay = [record for record in bundle["train.jsonl"] if record.get("replay")]
    assert len(replay) == 1 and replay[0]["replay"] == "teacher"


def test_output_is_new_and_manifest_fingerprints_each_file(tasks, tmp_path):
    bundle = prepare(tasks, [], replay_ratio=0)
    out = tmp_path / "prepared"
    write_bundle(out, bundle, {"seed": 0})
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) == set(bundle)
    assert manifest["files"]["train.jsonl"]["rows"] == len(bundle["train.jsonl"])
    assert len(manifest["files"]["train.jsonl"]["sha256"]) == 64
    before = (out / "train.jsonl").read_bytes()
    with pytest.raises(FileExistsError):
        write_bundle(out, bundle, {})
    assert (out / "train.jsonl").read_bytes() == before
