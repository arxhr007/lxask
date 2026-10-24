import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import filter_generated  # noqa: E402
import generate_data as gen  # noqa: E402
from lxask.validate import build_catalog  # noqa: E402


def api_response(payload: dict, input_tokens: int = 1000, output_tokens: int = 500) -> dict:
    return {
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload)}]},
        ],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def fake_requests(count: int):
    return lambda: [
        {"id": f"cmd{i}", "seed": {"cmd": f"cmd{i}"}, "system": "s", "user": "u",
         "schema": ("task_list", gen.TASKS_SCHEMA), "max_output_tokens": 1000}
        for i in range(count)
    ]


@pytest.fixture
def job(tmp_path, monkeypatch):
    """A `tasks` job of 6 requests writing into a temporary directory."""
    monkeypatch.setattr(gen, "OUT", tmp_path)
    monkeypatch.setitem(gen.JOBS, "tasks", fake_requests(6))
    return tmp_path


def run(job_dir, send, cap=50.0, limit=None, price=(1.0, 2.0)):
    budget = gen.Budget(job_dir / "ledger.json", cap)
    gen.run("tasks", "test-model", price, budget, limit, workers=1, effort=None, send=send)
    return budget


def written(job_dir):
    path = job_dir / "tasks.raw.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_output_json_reads_the_message_and_ignores_other_items():
    assert gen.output_json(api_response({"items": []})) == {"items": []}
    assert gen.output_json({"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}) is None
    assert gen.output_json({"output": [{"type": "message", "content": [{"type": "output_text", "text": "{cut"}]}]}) is None


def test_cost_uses_input_and_output_prices():
    assert gen.cost({"input_tokens": 1_000_000, "output_tokens": 500_000}, (2.0, 10.0)) == pytest.approx(7.0)


def test_run_writes_one_record_per_request_and_tracks_spend(job):
    sent = []

    def send(body):
        sent.append(body)
        return api_response({"items": [{"question": "q", "command": "c"}]})

    budget = run(job, send)
    records = written(job)
    assert [r["id"] for r in records] == [f"cmd{i}" for i in range(6)]
    assert records[0]["items"] == [{"question": "q", "command": "c"}]
    assert sent[0]["text"]["format"]["type"] == "json_schema" and sent[0]["text"]["format"]["strict"] is True
    # 6 requests x (1000 in x $1 + 500 out x $2) per million
    assert budget.spent == pytest.approx(6 * 0.002)
    assert json.loads((job / "ledger.json").read_text())["requests"] == 6


def test_rerun_skips_what_is_already_done(job):
    calls = []

    def send(body):
        calls.append(1)
        return api_response({"items": []})

    run(job, send, limit=2)
    assert len(calls) == 2
    run(job, send)
    assert len(calls) == 6  # only the 4 that were missing
    assert len(written(job)) == 6


def test_run_stops_before_passing_the_cap(job):
    calls = []

    def send(body):
        calls.append(1)
        return api_response({"items": []}, input_tokens=0, output_tokens=1000)

    # Each request costs $1 at most and in fact (1000 output tokens at $1000 per million).
    budget = run(job, send, cap=3.5, price=(0.0, 1000.0))
    assert len(calls) == 3
    assert budget.spent <= 3.5


def test_spend_carries_over_between_runs(job):
    send = lambda body: api_response({"items": []}, input_tokens=0, output_tokens=1000)  # noqa: E731
    run(job, send, cap=2.5, price=(0.0, 1000.0))
    assert len(written(job)) == 2
    run(job, send, cap=2.5, price=(0.0, 1000.0))  # the ledger remembers: nothing more is sent
    assert len(written(job)) == 2


def test_failed_request_is_not_written_and_can_be_retried(job):
    state = {"fail": True}

    def send(body):
        if state["fail"]:
            raise RuntimeError("HTTP 500: boom")
        return api_response({"items": []})

    run(job, send, limit=2)
    assert written(job) == []
    state["fail"] = False
    run(job, send, limit=2)
    assert len(written(job)) == 2


def test_load_key_prefers_env_then_file_and_never_guesses(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "KEY_FILE", tmp_path / "gen_api_key")
    monkeypatch.delenv("LXASK_GEN_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        gen.load_key()
    (tmp_path / "gen_api_key").write_text("from-file\n")
    assert gen.load_key() == "from-file"
    monkeypatch.setenv("LXASK_GEN_API_KEY", "from-env")
    assert gen.load_key() == "from-env"


# filtering


@pytest.fixture(scope="module")
def catalog():
    titles = [("top", t) for t in ("-b, --batch", "-n, --iterations = NUMBER", "-p, --pid = PIDLIST", "-d, --delay = SECS")]
    titles += [("ps", t) for t in ("-e", "-o format", "--sort spec", "-p pidlist")]
    return build_catalog(["top", "ps", "head", "lsof", "kill", "df"], titles)


def test_filter_tasks_keeps_valid_drops_invalid_and_duplicates(catalog):
    records = [{"id": "top", "items": [
        {"question": "show processes once in batch mode", "command": "top -b -n 1"},
        {"question": "show processes once", "command": "top -l 1"},
        {"question": "Show processes once, in batch mode!", "command": "top -b -n 1"},
        {"question": "copy to clipboard please", "command": "ps -e | pbcopy"},
        {"question": "ok", "command": "top -b"},
    ]}]
    kept, rejects = filter_generated.filter_tasks(records, catalog, syntax=False)
    assert kept == [{"cmd": "top", "question": "show processes once in batch mode", "command": "top -b -n 1", "source": "tasks"}]
    reasons = [reject["reasons"] for reject in rejects]
    assert reasons == [
        ["undocumented-option: top: -l is not a documented option"],
        ["duplicate question"],
        ["not-linux: pbcopy: not a Linux command"],
        ["question too short"],
    ]


def test_filter_scenarios_needs_every_step_valid_and_self_contained(catalog):
    good = {"request": "server is slow, find the busiest process and see its open files",
            "steps": [{"task": "list processes sorted by cpu usage", "command": "ps -eo pid,comm --sort=-%cpu | head"},
                      {"task": "list the files a process has open", "command": "lsof -p PID"}]}
    bad_command = {"request": "server is slow, show the busiest process right now",
                   "steps": [{"task": "show processes once", "command": "top -l 1"}]}
    points_back = {"request": "find the busiest process and then stop that one gracefully",
                   "steps": [{"task": "list processes sorted by cpu usage", "command": "ps -e"},
                             {"task": "terminate it gracefully", "command": "kill -TERM PID"}]}
    records = [{"id": "x#0", "seed": {"area": "process management"}, "scenarios": [good, bad_command, points_back]}]
    kept, rejects = filter_generated.filter_scenarios(records, catalog, syntax=False)
    assert [s["request"] for s in kept] == [good["request"]]
    assert kept[0]["area"] == "process management"
    assert rejects[0]["reasons"] == ["undocumented-option: top: -l is not a documented option"]
    assert rejects[1]["reasons"] == ["step not self-contained: terminate it gracefully"]


def test_filter_tasks_drops_what_the_reviewer_judged_wrong_and_names_the_source(catalog):
    records = [
        {"id": "ps#0", "job": "depth", "seed": {"cmd": "ps"}, "items": [
            {"question": "list every process on the system", "command": "ps -e"},
            {"question": "show only my own processes", "command": "ps -e"}]},
        {"id": "everyday:disk space#3", "job": "everyday", "seed": {"area": "disk space"}, "items": [
            {"question": "how full are my disks", "command": "df"}]},
    ]
    wrong = {gen.review_key("show only my own processes", "ps -e"): "lists every user's processes"}
    kept, rejects = filter_generated.filter_tasks(records, catalog, syntax=False, wrong=wrong)
    assert [(task["cmd"], task["source"]) for task in kept] == [("ps", "depth"), ("df", "everyday")]
    assert rejects[0]["reasons"] == ["reviewer: lists every user's processes"]


def test_a_question_whose_answer_the_reviewer_rejected_is_not_refilled_by_a_twin(catalog):
    records = [{"id": "ps", "items": [
        {"question": "show only my own processes", "command": "ps -e"},
        {"question": "Show only my own processes!", "command": "ps -e -o pid"}]}]
    wrong = {gen.review_key("show only my own processes", "ps -e"): "lists every user's processes"}
    kept, rejects = filter_generated.filter_tasks(records, catalog, syntax=False, wrong=wrong)
    assert kept == []  # the twin was never shown to the reviewer
    assert [reject["reasons"] for reject in rejects] == [["reviewer: lists every user's processes"], ["duplicate question"]]


def test_scenarios_must_keep_values_from_request_to_task_to_command():
    request = "Show the 10 biggest files in /var/log/myapp, then delete .log files older than 30 days there."
    good = [{"task": "list the 10 largest files under /var/log/myapp", "command": "du -a /var/log/myapp | sort -nr | head -n 10"},
            {"task": "delete .log files older than 30 days under /var/log/myapp",
             "command": "find /var/log/myapp -name '*.log' -mtime +30 -delete"}]
    assert gen.scenario_literal_problems(request, good) == []
    lost = [good[0], {**good[1], "task": "delete old files under /var/log/myapp"}]
    assert "tasks lose '.log'" in gen.scenario_literal_problems(request, lost)
    guessed = [{**good[0], "task": "list the 10 largest application logs"}, good[1]]
    assert "task lacks '/var/log/myapp'" in gen.scenario_literal_problems(request, guessed)


def test_rewritten_tasks_replace_the_old_ones(catalog):
    steps = [{"task": "list the files a process has open", "command": "lsof -p PID"}]
    record = {"id": "x#0", "job": "scenarios", "seed": {"area": "process management"},
              "scenarios": [{"request": "see what files process 4242 has open please", "steps": steps}]}
    kept, _ = filter_generated.filter_scenarios([record], catalog, syntax=False,
                                                retasks={"see what files process 4242 has open please": ["list the files process 4242 has open"]})
    assert kept[0]["steps"][0]["task"] == "list the files process 4242 has open"
    assert kept[0]["retasked"] is True and kept[0]["source"] == "scenarios"


def test_fixes_are_kept_only_when_they_pass_and_change_something(catalog):
    items = [["show only my own processes", "ps -e", "lists every user's processes", "ps"],
             ["how full are my disks", "df", "reviewer was wrong", "df"],
             ["show processes once in batch mode", "top -l 1", "macOS form", "top"]]
    record = {"seed": {"items": items}, "fixes": [
        {"n": 1, "skip": False, "command": "ps -p PID"},
        {"n": 2, "skip": False, "command": "df"},
        {"n": 3, "skip": False, "command": "top -l 1 -b"}]}
    kept, rejects = filter_generated.filter_fixes([record], catalog, syntax=False)
    assert kept == [{"cmd": "ps", "question": "show only my own processes", "wrong": "ps -e", "command": "ps -p PID",
                     "reason": "lists every user's processes"}]
    assert [r["reasons"] for r in rejects] == [["same as the rejected command"],
                                               ["undocumented-option: top: -l is not a documented option"]]


def test_review_requests_cover_only_unreviewed_small_model_tasks(tmp_path, monkeypatch):
    out = tmp_path / "data" / "generated"
    out.mkdir(parents=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    monkeypatch.setattr(gen, "OUT", out)
    tasks = [{"cmd": "ps", "question": f"question {i}", "command": "ps -e", "source": "tasks"} for i in range(12)]
    tasks.append({"cmd": "df", "question": "how full are my disks", "command": "df", "source": "everyday"})
    (seed / "generated_tasks.jsonl").write_text("".join(json.dumps(task) + "\n" for task in tasks), encoding="utf-8")

    first, second = gen.review_requests()
    assert len(first["seed"]["items"]) == 10 and len(second["seed"]["items"]) == 2  # the everyday task is not reviewed
    assert "1. Q: question 0\n   A: ps -e" in first["user"]

    record = {"id": first["id"], "seed": first["seed"], "verdicts": []}
    (out / "review.raw.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    (remaining,) = gen.review_requests()
    assert remaining["seed"]["items"] == second["seed"]["items"]
