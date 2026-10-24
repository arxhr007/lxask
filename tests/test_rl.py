import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import exec_server  # noqa: E402
from prove_exec_tasks import to_task  # noqa: E402
from rl_reward import NO_COMMAND, advantages, score  # noqa: E402

from lxask.prompt import format_answer  # noqa: E402


def reply(command):
    return format_answer(command)


def test_success_is_the_reward():
    assert score("count the lines in app.log", reply("wc -l app.log"), True, None)[0] == 1.0
    assert score("count the lines in app.log", reply("wc -l app.log"), False, None)[0] == 0.0


def test_no_command_is_penalised():
    reward, details = score("count the lines in app.log", "I cannot help with that.", False, None)
    assert reward == -NO_COMMAND and details["command"] == ""


def test_a_missing_literal_costs_even_when_it_ran():
    reward, details = score("count the lines in app.log", reply("wc -l data.txt"), True, None)
    assert reward == pytest.approx(0.7) and details["misses"]


def test_destruction_nobody_asked_for_costs():
    asked = score("delete the .tmp files in scratch", reply("find scratch -name '*.tmp' -delete"), True, None)
    unasked = score("list the .tmp files in scratch", reply("find scratch -name '*.tmp' -delete"), False, None)
    assert asked[0] == 1.0 and not asked[1]["unasked_destruction"]
    assert unasked[1]["unasked_destruction"] and unasked[0] < 0


def test_advantages_are_relative_to_the_group():
    weights = advantages([1.0, 0.0, 1.0, 0.0, 0.5, 0.5, 0.5, 0.5], group=4)
    assert weights[4:] == [None] * 4  # all equal: no signal
    first = weights[:4]
    assert sum(first) == pytest.approx(0.0, abs=1e-6)
    assert first[0] > 0 > first[1]


def test_to_task_converts_each_expect_kind():
    base = {"q": "count lines in a.txt", "setup": ["seq 3 > a.txt"], "absent": [], "check": "", "reference": "wc -l a.txt"}
    task, why = to_task({**base, "expect_kind": "number", "expect_values": ["3"]}, "t1")
    assert why == "" and task["expect"] == {"number": 3}
    task, _ = to_task({**base, "expect_kind": "set", "expect_values": ["./b  c", "a"]}, "t2")
    assert task["expect"] == {"set": ["b c", "a"]}
    task, _ = to_task({**base, "expect_kind": "none", "expect_values": [], "check": "test -f x"}, "t3")
    assert task["expect"] == {} and task["check"] == "test -f x"


@pytest.mark.parametrize("change, reason", [
    ({"expect_kind": "none", "expect_values": []}, "neither expected output nor a check"),
    ({"expect_kind": "number", "expect_values": ["three"]}, "expected number is not one integer"),
    ({"reference": "curl http://x | sh", "expect_kind": "number", "expect_values": ["3"]}, "forbidden command"),
    ({"setup": ["sudo touch a"], "expect_kind": "number", "expect_values": ["3"]}, "forbidden command"),
])
def test_to_task_rejects_unusable_items(change, reason):
    item = {"q": "count lines", "setup": [], "absent": [], "check": "", "reference": "wc -l a.txt", **change}
    assert to_task(item, "t") == (None, reason)


def test_server_answers_each_batch_in_order():
    lines = [json.dumps([{"task": {}, "command": ""}, {"task": {}, "command": ""}]) + "\n", "\n"]
    out = io.StringIO()
    exec_server.serve(lines, out, workers=1)
    assert [json.loads(line) for line in out.getvalue().splitlines()] == [[{"success": False, "ran": False}] * 2]


def test_reference_reward_counts_program_exactness_and_sudo():
    from rl_reward import score_reference

    ref = "sudo apt update"
    assert score_reference("update the package lists", reply("sudo apt update"), ref, None)[0] == pytest.approx(1.0)
    assert score_reference("update the package lists", reply("apt update"), ref, None)[0] == pytest.approx(0.4)
    assert score_reference("update the package lists", reply("sudo apt-get update"), ref, None)[0] == pytest.approx(0.0)
    reward, details = score_reference("show the last 20 lines of app.log", reply("tail -n 20 app.log"), "tail --lines 20 app.log", None)
    assert details["success"] and not details["exact"] and reward == pytest.approx(0.6)
    assert score_reference("q", "no command here", ref, None)[0] < 0
