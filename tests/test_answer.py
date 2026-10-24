"""The flow from question to printed steps, with a scripted model and no index."""

import pytest

from lxask.answer import Step, answer, render, unhandled
from lxask.explain import Described, brief, explain
from lxask.explain import render as render_described
from lxask.llm import looks_looped
from lxask.prompt import ANSWER_SYSTEM, SPLIT_SYSTEM
from lxask.retrieve import Doc
from lxask.split import needs_split, tasks_for
from lxask.validate import build_catalog, load_catalog, save_catalog

OPTIONS = [
    ("ps", "-e", "Select all processes. Identical to -A."),
    ("ps", "-o format", "User-defined format. format is a single argument in the form of a blank-separated list."),
    ("ps", "--sort spec", "Specify sorting order."),
    ("ps", "-p pidlist", "Select by PID."),
    ("lsof", "-p s", "excludes or selects the listing of files for the processes whose IDs are in s."),
    ("lsof", "-i [i]", "selects the listing of files any of whose Internet address matches i."),
    ("lsof", "-u s", "selects the listing of files for the user whose login name is in s."),
    ("top", "-b, --batch", "Starts top in Batch mode."),
    ("top", "-n, --iterations = NUMBER", "Specifies the maximum number of iterations."),
    ("top", "-d, --delay = SECS", "Specifies the delay between screen updates."),
    ("git-commit", "-m <msg>, --message=<msg>", "Use the given <msg> as the commit message."),
    ("git-commit", "--amend", "Replace the tip of the current branch by creating a new commit."),
    ("git-commit", "-a, --all", "Tell the command to automatically stage files."),
    ("rm", "-r, -R, --recursive", "remove directories and their contents recursively"),
    ("rm", "-f, --force", "ignore nonexistent files and arguments, never prompt"),
    ("rm", "-v, --verbose", "explain what is being done"),
]
SUMMARIES = {
    "ps": "Information about running processes.",
    "head": "Output the first part of files.",
    "lsof": "List open files and the corresponding processes.",
    "kill": "Send a signal to a process, usually related to stopping the process.",
    "top": "Display dynamic real-time information about running processes.",
    "git": "Distributed version control system.",
    "git commit": "Commit files to the repository.",
    "rm": "Remove files or directories.",
}


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(["ps", "head", "lsof", "kill", "top", "git", "git commit", "rm"], OPTIONS, summaries=SUMMARIES)


class ScriptedModel:
    """Answers the SPLIT prompt with `tasks` and each ANSWER prompt from `commands`, by task."""

    def __init__(self, tasks, commands):
        self.tasks, self.commands, self.calls = tasks, commands, []

    def __call__(self, messages):
        system, user = messages[0]["content"], messages[1]["content"]
        self.calls.append(system)
        if system == SPLIT_SYSTEM:
            return self.tasks
        task = user.rsplit("Question: ", 1)[-1]
        command = self.commands.get(task)
        return None if command is None else f"```bash\n{command}\n```"


REQUEST = "My server is slow. Find the busiest process, check which files it has open, and stop it gracefully."
TASKS = "- list processes sorted by CPU usage\n- list the files a process has open\n- terminate a process gracefully"
COMMANDS = {
    "list processes sorted by CPU usage": "ps -eo pid,user,%cpu,%mem,comm --sort=-%cpu | head",
    "list the files a process has open": "lsof -p PID",
    "terminate a process gracefully": "kill -TERM PID",
}


# splitting


def test_short_single_clause_questions_are_not_split():
    assert not needs_split("show disk usage of each folder")
    assert needs_split("find the busiest process and stop it")
    assert needs_split("show free disk space, then list the largest folders")
    model = ScriptedModel(TASKS, {})
    assert tasks_for("  show disk usage of each folder ", model) == ["show disk usage of each folder"]
    assert model.calls == []  # no model call was spent on it


def test_split_falls_back_to_the_question_when_the_model_gives_nothing():
    assert tasks_for(REQUEST, lambda messages: None) == [REQUEST]
    assert tasks_for(REQUEST, lambda messages: "I am not sure what you mean.") == [REQUEST]


# the documentation-based explanation


def test_brief_keeps_the_first_sentence_and_cuts_long_ones():
    assert brief("Select all processes. Identical to -A.") == "Select all processes"
    assert brief("Use e.g. this. Or that.") == "Use e.g. this"
    assert brief("File is of type c:") == "File is of type c"
    cut = brief("word " * 60)
    assert cut.endswith(" ...") and len(cut) <= 115


def test_explain_describes_programs_and_the_options_they_are_given(catalog):
    assert explain("ps -eo pid,user --sort=-%cpu | head", catalog) == [
        Described("ps", "Information about running processes", (
            ("-e", "Select all processes"), ("-o", "User-defined format"), ("--sort", "Specify sorting order"))),
        Described("head", "Output the first part of files", ()),
    ]


def test_explain_uses_the_subcommand_page(catalog):
    (entry,) = explain("git commit --amend -m 'fix'", catalog)
    assert entry.program == "git commit" and entry.summary == "Commit files to the repository"
    assert [option for option, _ in entry.options] == ["--amend", "-m"]


def test_explain_says_nothing_about_what_it_does_not_know(catalog):
    assert explain("frobnicate --now", catalog) == []
    assert explain('echo "unterminated', catalog) == []
    assert explain("top -l 1", catalog) == [
        Described("top", "Display dynamic real-time information about running processes", ())]


def test_render_described_aligns_columns_to_the_longest_name():
    lines = render_described([
        Described("ps", "Information about running processes", (("-e", "Select all processes"), ("--sort", "Specify sorting order"))),
        Described("journalctl", "Query the systemd journal", ()),
    ])
    assert lines == [
        "ps          Information about running processes",
        "  -e      Select all processes",
        "  --sort  Specify sorting order",
        "journalctl  Query the systemd journal",
    ]


# the catalogue in the index


def test_catalogue_survives_a_round_trip_through_the_index(catalog, tmp_path):
    db = tmp_path / "docs.sqlite"
    assert load_catalog(db) is None  # an index built before catalogues existed
    save_catalog(db, catalog)
    loaded = load_catalog(db)
    assert loaded.programs == catalog.programs
    assert loaded.with_subcommands == catalog.with_subcommands == {"git"}
    assert loaded.options == catalog.options
    assert loaded.texts == catalog.texts
    assert loaded.summaries == catalog.summaries
    save_catalog(db, catalog)  # rewriting replaces, it does not duplicate
    assert load_catalog(db).options == catalog.options


# loops


@pytest.mark.parametrize("text, looped", [
    ("```bash\nls -la\n```", False),
    ("grep -v a | grep -v b | grep -v c | grep -v d", False),
    ("less -S -X -S -X -S -X -S -X -S -X", True),
    ("- show disk usage\n- show disk usage\n- show disk usage", True),
    ("", False),
])
def test_looks_looped(text, looped):
    assert looks_looped(text) is looped


# the whole flow


def test_answer_gives_one_checked_and_explained_step_per_task(catalog):
    model = ScriptedModel(TASKS, COMMANDS)
    searched = []

    def search(task):
        searched.append(task)
        return [Doc("ps", "List all running processes", "ps aux", "tldr", -0.5)]

    steps = answer(REQUEST, model, search, catalog)
    assert [step.task for step in steps] == list(COMMANDS)
    assert [step.command for step in steps] == list(COMMANDS.values())
    assert searched == list(COMMANDS)  # each task is searched on its own, never the whole request
    assert model.calls == [SPLIT_SYSTEM, ANSWER_SYSTEM, ANSWER_SYSTEM, ANSWER_SYSTEM]
    assert all(step.issues == [] and step.closest == "" for step in steps)
    assert steps[0].described[0].program == "ps"


def test_render_numbers_the_steps_and_explains_from_the_documentation(catalog):
    steps = answer(REQUEST, ScriptedModel(TASKS, COMMANDS), lambda task: [], catalog)
    assert render(steps).splitlines() == [
        "1. List processes sorted by CPU usage",
        "   $ ps -eo pid,user,%cpu,%mem,comm --sort=-%cpu | head",
        "     ps    Information about running processes",
        "       -e      Select all processes",
        "       -o      User-defined format",
        "       --sort  Specify sorting order",
        "     head  Output the first part of files",
        "",
        "2. List the files a process has open",
        "   $ lsof -p PID",
        "     lsof  List open files and the corresponding processes",
        "       -p  excludes or selects the listing of files for the processes whose IDs are in s",
        "",
        "3. Terminate a process gracefully",
        "   $ kill -TERM PID",
        "     kill  Send a signal to a process, usually related to stopping the process",
    ]
    assert "Information about" not in render(steps, explanations=False)


def test_a_single_task_is_printed_bare(catalog):
    steps = answer("list open files of a process", ScriptedModel("", {"list open files of a process": "lsof -p PID"}), lambda task: [], catalog)
    assert render(steps).splitlines() == [
        "$ lsof -p PID",
        "  lsof  List open files and the corresponding processes",
        "    -p  excludes or selects the listing of files for the processes whose IDs are in s",
    ]


def test_the_reported_failure_is_flagged_not_hidden(catalog):
    # The answer that prompted this work: a macOS option on a Linux question.
    model = ScriptedModel("", {"show the busiest process once": "top -l 1 -s 10"})
    notes = [Doc("top", "Start top in batch mode for one iteration", "top -b -n 1", "tldr", -0.7)]
    (step,) = answer("show the busiest process once", model, lambda task: notes, catalog)
    assert step.issues == ["top: -l is not a documented option", "top: -s is not a documented option"]
    assert step.closest == "top -b -n 1"
    assert render([step]).splitlines()[:3] == [
        "$ top -l 1 -s 10",
        "  unverified: top: -l is not a documented option",
        "  unverified: top: -s is not a documented option",
    ]
    assert "  closest documented example: top -b -n 1" in render([step])


def test_destructive_commands_carry_a_warning(catalog):
    model = ScriptedModel("", {"delete the build folder": "rm -rf /tmp/build"})
    (step,) = answer("delete the build folder", model, lambda task: [], catalog)
    assert step.warnings == ["rm -r deletes whole directory trees and cannot be undone"]
    assert render([step]).splitlines()[-1] == "  warning: rm -r deletes whole directory trees and cannot be undone"


def test_a_step_the_model_cannot_answer_says_so(catalog):
    commands = dict(COMMANDS, **{"list the files a process has open": None})
    steps = answer(REQUEST, ScriptedModel(TASKS, commands), lambda task: [], catalog)
    assert steps[1] == Step("list the files a process has open")
    assert "   (could not produce a command for this step)" in render(steps).splitlines()


TASK = "delete .log files older than 30 days in /var/log/myapp"


def test_a_command_that_drops_part_of_the_task_is_flagged(catalog):
    # Seen from the round-2 model: valid, and it deletes every old file, not just the logs.
    model = ScriptedModel("", {TASK: "find /var/log/myapp -type f -mtime +30 -delete"})
    (step,) = answer(TASK, model, lambda task: [], catalog)
    assert step.misses == ["the request says '.log'; the command does not have it"]
    assert "  unverified: the request says '.log'; the command does not have it" in render([step]).splitlines()


def test_an_objection_gets_another_try_and_the_clean_answer_is_kept():
    model = ScriptedModel("", {TASK: "find /var/log/myapp -type f -mtime +30 -delete"})
    tries = []

    def resample(messages, attempt):
        tries.append(attempt)
        return "```bash\nfind /var/log/myapp -type f -name '*.log' -mtime +30 -delete\n```"

    # No catalogue: the test one has no `find`, and only the task check is under test here.
    (step,) = answer(TASK, model, lambda task: [], None, resample=resample)
    assert step.command == "find /var/log/myapp -type f -name '*.log' -mtime +30 -delete"
    assert step.misses == [] and step.tries == 2 and tries == [0]


def test_retries_stop_after_two_and_keep_the_least_objectionable():
    model = ScriptedModel("", {TASK: "find /var/log/myapp -type f -mtime +30 -delete"})
    worse = "```bash\nfind /tmp -mtime +30 -delete\n```"  # loses the path as well
    (step,) = answer(TASK, model, lambda task: [], None, resample=lambda messages, attempt: worse)
    assert step.command == "find /var/log/myapp -type f -mtime +30 -delete"  # the first answer, on fewer objections
    assert step.tries == 3


def test_a_clean_answer_is_not_retried(catalog):
    def resample(messages, attempt):
        raise AssertionError("no retry was needed")

    (step,) = answer("list open files of a process", ScriptedModel("", {"list open files of a process": "lsof -p PID"}),
                     lambda task: [], catalog, resample=resample)
    assert step.tries == 1


def test_a_missing_answer_is_retried(catalog):
    (step,) = answer("list open files of a process", ScriptedModel("", {}), lambda task: [], catalog,
                     resample=lambda messages, attempt: "```bash\nlsof -p PID\n```")
    assert step.command == "lsof -p PID" and step.tries == 2


LOG_REQUEST = "Check free space on /var, then delete .log files older than 30 days in /var/log/myapp."
LOG_TASKS_LOST = "- show free space on /var\n- delete files older than 30 days in /var/log/myapp"
LOG_TASKS = "- show free space on /var\n- delete .log files older than 30 days in /var/log/myapp"
LOG_COMMANDS = {
    "show free space on /var": "df -h /var",
    "delete files older than 30 days in /var/log/myapp": "find /var/log/myapp -type f -mtime +30 -delete",
    "delete .log files older than 30 days in /var/log/myapp": "find /var/log/myapp -name '*.log' -mtime +30 -delete",
}


def test_a_split_that_loses_the_file_pattern_is_tried_again():
    resplit = ScriptedModel(LOG_TASKS, LOG_COMMANDS)
    steps = answer(LOG_REQUEST, ScriptedModel(LOG_TASKS_LOST, LOG_COMMANDS), lambda task: [], None,
                   resample=lambda messages, attempt: resplit(messages))
    assert [step.task for step in steps] == ["show free space on /var", "delete .log files older than 30 days in /var/log/myapp"]
    assert steps[1].command == "find /var/log/myapp -name '*.log' -mtime +30 -delete"


def test_what_no_command_keeps_is_noted_at_the_end():
    steps = answer(LOG_REQUEST, ScriptedModel(LOG_TASKS_LOST, LOG_COMMANDS), lambda task: [], None)
    assert unhandled(LOG_REQUEST, steps) == ["the request says '.log'; none of the commands have it"]
    assert render(steps, question=LOG_REQUEST).splitlines()[-1] == "note: the request says '.log'; none of the commands have it"
    assert "note:" not in render(steps)  # only when the request is given


def test_without_a_catalogue_commands_are_shown_unchecked():
    (step,) = answer("show the busiest process once", ScriptedModel("", {"show the busiest process once": "top -l 1"}), lambda task: [], None)
    assert step.command == "top -l 1" and step.issues == [] and step.described == []


def test_a_vague_request_is_asked_back():
    from lxask.answer import clarification

    asked = clarification("add a proxy")
    assert asked.startswith('"add a proxy" can mean several things.')
    assert 'lxask "set a proxy for git"' in asked and 'lxask "set a proxy for apt"' in asked
    for clear in ("set a proxy for git", "make apt use a proxy", "set up nginx as a reverse proxy",
                  "show what is listening on port 8080", "edit proxy.conf"):
        assert clarification(clear) is None, clear
