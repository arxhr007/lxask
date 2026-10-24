from lxask.index import (
    clean_description,
    man_option_rows,
    man_subcommands,
    man_summary,
    parse_tldr_page,
    strip_placeholders,
    tldr_rows,
)

TAR_PAGE = """# tar

> Archiving utility.
> Often combined with a compression method, such as `gzip` or `bzip2`.
> More information: <https://www.gnu.org/software/tar>.

- [c]reate an archive and write it to a [f]ile:

`tar cf {{path/to/target.tar}} {{path/to/file1 path/to/file2 ...}}`

- E[x]tract a (compressed) archive [f]ile into the current directory [v]erbosely:

`tar xvf {{path/to/source.tar[.gz|.bz2|.xz]}}`
"""

LS_MAN = """LS(1)                         User Commands                         LS(1)

NAME
       ls - list directory contents

SYNOPSIS
       ls [OPTION]... [FILE]...

DESCRIPTION
       List information about the FILEs (the current directory by default).

       -a, --all
              do not ignore entries starting with .

       -h, --human-readable
              with -l and -s, print sizes like 1K 234M 2G etc.

       --block-size=SIZE  scale sizes by SIZE before printing them

       - this is a bullet, not an option
"""


def test_clean_description_drops_mnemonic_brackets():
    assert clean_description("[c]reate an archive and write it to a [f]ile:") == (
        "create an archive and write it to a file"
    )


def test_strip_placeholders_prefers_long_option():
    assert strip_placeholders("grep {{[-i|--ignore-case]}} {{pattern}} {{path/to/file}}") == (
        "grep --ignore-case pattern path/to/file"
    )


def test_parse_tldr_page():
    page = parse_tldr_page(TAR_PAGE)
    assert page.name == "tar"
    assert page.summary.startswith("Archiving utility.")
    assert "More information" not in page.summary
    assert page.examples[0] == (
        "create an archive and write it to a file",
        "tar cf {{path/to/target.tar}} {{path/to/file1 path/to/file2 ...}}",
    )
    assert len(page.examples) == 2


def test_tldr_rows_strip_placeholders_and_add_summary():
    rows = list(tldr_rows(parse_tldr_page(TAR_PAGE)))
    assert [r.source for r in rows] == ["summary", "tldr", "tldr"]
    assert rows[1].body == "tar cf path/to/target.tar path/to/file1 path/to/file2 ..."


def test_parse_tldr_page_without_title_is_none():
    assert parse_tldr_page("just some text\n") is None


def test_man_summary():
    assert man_summary(LS_MAN) == "list directory contents"


def test_man_option_rows_keep_only_documented_flags():
    rows = list(man_option_rows("ls", LS_MAN))
    assert [(r.title, r.body) for r in rows] == [
        ("-a, --all", "do not ignore entries starting with ."),
        ("-h, --human-readable", "with -l and -s, print sizes like 1K 234M 2G etc."),
        ("--block-size=SIZE", "scale sizes by SIZE before printing them"),
    ]
    assert all(r.cmd == "ls" and r.source == "man" for r in rows)


SYSTEMCTL_MAN = """NAME
       systemctl - Control the systemd system and service manager

COMMANDS
       The following commands are understood:

   Unit Commands (Introspection and Modification)
       list-units [PATTERN...]
           List units that systemd currently has in memory. This includes units that
           are either referenced directly or through a dependency.

       start PATTERN...
           Start (activate) one or more units specified on the command line.

       enable UNIT..., enable PATH...
           Enable one or more units or unit instances.

       is-active PATTERN...
       is-failed PATTERN...
           Check whether any of the specified units are active (i.e. running).

OPTIONS
       -t, --type=
           The argument is a comma-separated list of unit types.

       --version
           Print a short version string and exit.
"""


def test_man_subcommands_reads_the_commands_section():
    verbs = man_subcommands(SYSTEMCTL_MAN)
    assert verbs[:5] == ["list-units", "start", "enable", "is-active", "is-failed"]
    assert "version" not in verbs and "type" not in verbs  # options are not verbs
    assert man_subcommands("NAME\n       ls - list directory contents\n") == []
