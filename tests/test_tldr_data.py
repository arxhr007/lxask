import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import nl2bash  # noqa: E402
from tldr_data import (  # noqa: E402
    as_task,
    explain,
    fill,
    is_held_out,
    named_question,
    template_question,
    third_person,
)


@pytest.mark.parametrize(
    "verb, expected",
    [("Extract", "Extracts"), ("Search", "Searches"), ("Copy", "Copies"), ("Display", "Displays"),
     ("Fix", "Fixes"), ("Do", "Do"), ("[Interactive]", "[Interactive]")],
)
def test_third_person(verb, expected):
    assert third_person(verb) == expected


def test_explain_turns_an_instruction_into_a_statement():
    assert explain("Extract an archive into the current directory:") == "Extracts an archive into the current directory."
    assert explain("recursively search a directory") == "Recursively searches a directory."


def test_as_task_lowercases_the_start_but_not_acronyms():
    assert as_task("Display free disk space.") == "display free disk space"
    assert as_task("SSH into a host") == "SSH into a host"


def test_template_question_contains_the_task():
    assert "display free disk space" in template_question("Display free disk space", random.Random(1))


def test_named_question_names_the_tool():
    question = named_question("List ports.", "openstack", random.Random(2))
    assert "openstack" in question and "list ports" in question


def test_fill_replaces_every_placeholder_and_reports_the_values():
    filled, values = fill("tar xf {{path/to/source.tar[.gz|.bz2|.xz]}} --directory={{path/to/directory}}", random.Random(3))
    assert "{{" not in filled
    assert len(values) == 2
    assert values[0].endswith(".tar.gz")
    assert filled == f"tar xf {values[0]} --directory={values[1]}"


def test_fill_gives_distinct_values_to_numbered_placeholders():
    filled, values = fill("cat {{path/to/file1 path/to/file2 ...}}", random.Random(0))
    assert len(values) == 2 and values[0] != values[1]
    assert filled == f"cat {values[0]} {values[1]}"


def test_fill_resolves_option_alternatives_without_counting_them_as_values():
    filled, values = fill("grep {{[-i|--ignore-case]}} \"{{search_pattern}}\" {{path/to/file}}", random.Random(0))
    assert filled.startswith("grep --ignore-case ")
    assert len(values) == 2


def test_fill_refuses_placeholders_it_does_not_understand():
    assert fill("frobnicate {{quux_level}} {{path/to/file}}", random.Random(0)) is None
    assert fill("uptime", random.Random(0)) is None


def test_held_out_split_is_stable_and_small():
    names = [(f"cmd{i}", "do a thing") for i in range(2000)]
    held = [is_held_out(*name) for name in names]
    assert held == [is_held_out(*name) for name in names]
    assert 0.04 < sum(held) / len(held) < 0.13


def test_nl2bash_utility_skips_wrappers_and_assignments():
    assert nl2bash.utility("sudo FOO=1 /usr/bin/find . -name x") == "find"
    assert nl2bash.utilities("find . -type f | xargs grep -l foo && echo done") == ["find", "xargs", "echo"]


def test_nl2bash_flags():
    assert nl2bash.flags("ls -la --color=auto /tmp -1") == {"-la", "--color"}


def test_nl2bash_split_keeps_every_phrasing_of_a_command_together():
    assert nl2bash.is_eval("find . -name '*.txt'") == nl2bash.is_eval("find . -name '*.txt'")
    share = sum(nl2bash.is_eval(f"cmd {i}") for i in range(2000)) / 2000
    assert 0.06 < share < 0.14
