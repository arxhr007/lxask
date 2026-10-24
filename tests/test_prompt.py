from lxask.prompt import (
    ANSWER_SYSTEM,
    SPLIT_SYSTEM,
    build_messages,
    build_split_messages,
    format_answer,
    format_context,
    format_tasks,
    parse_answer,
    parse_tasks,
)
from lxask.retrieve import Doc

DOCS = [
    Doc("find", "find files larger than a given size", "find path/to/dir -size +500k", "tldr", -5.0),
    Doc("du", "Disk usage: estimate and summarize file and directory space usage.", "", "summary", -3.0),
]


def test_format_context_per_source():
    assert format_context(DOCS).splitlines() == [
        "- find: find files larger than a given size: `find path/to/dir -size +500k`",
        "- du: Disk usage: estimate and summarize file and directory space usage.",
    ]


def test_format_context_respects_budget():
    assert format_context(DOCS, max_chars=90).count("\n") == 0


def test_build_messages_with_and_without_context():
    system, user = build_messages("  find big files ", DOCS)
    assert system == {"role": "system", "content": ANSWER_SYSTEM}
    assert user["content"].startswith("Reference notes:\n- find:")
    assert user["content"].endswith("\n\nQuestion: find big files")

    assert build_messages("find big files")[1]["content"] == "Question: find big files"


def test_parse_answer_round_trips_format_answer():
    assert format_answer(" find / -type f -size +1G\n") == "```bash\nfind / -type f -size +1G\n```"
    assert parse_answer(format_answer("find / -type f -size +1G")) == "find / -type f -size +1G"


def test_parse_answer_ignores_prose_and_other_fence_labels():
    assert parse_answer("Use this:\n```sh\ndf -h\n```\nShows free space.") == "df -h"


def test_parse_answer_accepts_a_block_cut_off_by_the_token_limit():
    assert parse_answer("```bash\ntar czf backup.tar.gz /home") == "tar czf backup.tar.gz /home"


def test_parse_answer_without_a_block():
    assert parse_answer("I do not know.") == ""


def test_build_split_messages():
    system, user = build_split_messages("  find it, then stop it ")
    assert system == {"role": "system", "content": SPLIT_SYSTEM}
    assert user == {"role": "user", "content": "find it, then stop it"}


def test_parse_tasks_round_trips_format_tasks():
    tasks = ["list processes sorted by CPU usage", "list the files a process has open"]
    assert format_tasks(tasks) == "- list processes sorted by CPU usage\n- list the files a process has open"
    assert parse_tasks(format_tasks(tasks)) == tasks


def test_parse_tasks_takes_bullets_and_numbers_and_drops_the_rest():
    reply = "Here are the tasks:\n1. show disk usage\n2) show disk usage\n* find large files\n\nThat is all."
    assert parse_tasks(reply) == ["show disk usage", "find large files"]


def test_parse_tasks_is_capped_and_can_be_empty():
    assert len(parse_tasks("\n".join(f"- task {i}" for i in range(20)))) == 6
    assert parse_tasks("no list here") == []
