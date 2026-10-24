import pytest

from lxask.index import man_option_docs, man_option_titles
from lxask.validate import Issue, build_catalog, check, parse_option_title, simple_commands

MAN = {
    "top": ["-b, --batch", "-d, --delay = SECS", "-n, --iterations = NUMBER", "-p, --pid = PIDLIST", "-s, --secure-mode"],
    "ps": ["-e", "-o format", "--sort spec", "-p pidlist", "-A", "-f"],
    "ls": ["-a, --all", "-l", "-h, --human-readable", "--color[=WHEN]", "-S"],
    "find": ["-name pattern", "-size n[cwbkMG]", "-mtime n", "-type c", "-delete", "-exec command ;",
             "-print True; print the full file name on the standard output, followed by a newline.",
             "-print are -delete, -exec, -execdir, -ok"],
    "tar": ["-c, --create", "-x, --extract, --get", "-z, --gzip", "-f, --file=ARCHIVE", "-C, --directory=DIR"],
    "grep": ["-E, --extended-regexp", "-r, --recursive", "-n, --line-number", "-c, --count", "--include=GLOB"],
    "sudo": ["-u user, --user=user", "-i, --login", "-n, --non-interactive"],
    "xargs": ["-0, --null", "-I replace-str", "-n max-args, --max-args=max-args", "-r, --no-run-if-empty"],
    "rm": ["-f, --force", "-r, -R, --recursive", "-v, --verbose"],
    "kill": ["-s, --signal signal", "-l, --list [signal]", "-L, --table"],
    "chmod": ["-R, --recursive", "-v, --verbose", "-c, --changes"],
    "git": ["--version", "-C <path>", "-c <name>=<value>", "--no-pager"],
    "git-commit": ["-m <msg>, --message=<msg>", "--amend", "-a, --all", "--[no-]verify"],
    "apt-get": ["-y, --yes, --assume-yes", "-q, --quiet", "--no-install-recommends"],
    "sed": ["-n, --quiet, --silent", "-i[SUFFIX], --in-place[=SUFFIX]", "-e script, --expression=script", "-E, -r, --regexp-extended"],
    "timeout": ["-k, --kill-after=DURATION", "-s, --signal=SIGNAL", "--foreground"],
    "echo": ["-n", "-e", "-E"],
    "stat": ["-c, --format=FORMAT", "-f, --file-system", "-L, --dereference"],
}
PROGRAMS = [*MAN, "head", "sort", "du", "lsof", "pidof", "gzip", "cp", "docker", "docker run", "apt", "date", "ifconfig", "cat"]


@pytest.fixture(scope="module")
def catalog():
    titles = [(page, title) for page, group in MAN.items() for title in group]
    return build_catalog(PROGRAMS, titles, examples=["tar xf archive.tar --wildcards '*.html'"])


def details(command, catalog):
    return [str(issue) for issue in check(command, catalog)]


@pytest.mark.parametrize(
    "title, expected",
    [
        ("-a, --all", [("-a", False), ("--all", False)]),
        ("-C, --directory=DIR", [("-C", True), ("--directory", True)]),
        ("-I replace-str", [("-I", True)]),
        ("-m <msg>, --message=<msg>", [("-m", True), ("--message", True)]),
        ("--color[=WHEN]", [("--color", False)]),
        ("--[no-]verify", [("--verify", False), ("--no-verify", False)]),
        ("-, -l, --login", [("-l", False), ("--login", False)]),
        ("-print True; print the full file name on the standard output, followed by a newline.", [("-print", False)]),
        ("-print are -delete, -exec, -execdir, -ok", [("-print", False), ("-exec", False), ("-execdir", False), ("-ok", False)]),
        ("-ppattern or --pattern=pattern", [("-ppattern", True), ("--pattern", True), ("-p", True)]),
    ],
)
def test_parse_option_title(title, expected):
    assert parse_option_title(title) == expected


def test_man_option_titles_finds_headings_wherever_the_page_puts_them():
    page = (
        "OPTIONS\n"
        "   Pattern Syntax\n"
        "       -E, --extended-regexp\n"
        "              Interpret PATTERNS as extended regular expressions.\n"
        "\n"
        "       -n, --quiet\n"
        "\n"
        "              suppress automatic printing\n"
        "\n"
        "       -, -l, --login\n"
        "              Start the shell as a login shell.\n"
        "              -t, --tcp\n"
        "                     Display TCP sockets, and never a line like this one:\n"
        "                     -x is not a heading\n"
    )
    assert list(man_option_titles(page)) == ["-E, --extended-regexp", "-n, --quiet", "-, -l, --login", "-t, --tcp"]


def test_man_option_docs_skips_prose_lines_that_start_with_an_option():
    # find(1): the first two "-delete" lines are wrapped prose, the third the heading.
    page = (
        "              Furthermore,  find  with  the -ignore_readdir_race option will ignore errors of the\n"
        "              -delete action in the case the file has disappeared since the parent directory  was\n"
        "              read:  it  will  not output an error diagnostic.\n"
        "\n"
        "              Often, the user might want to test a find command line with -print prior to  adding\n"
        "              -delete  for  the  actual  removal run.\n"
        "\n"
        "       -delete\n"
        "              Delete files or directories; true if removal succeeded.\n"
        "\n"
        "       --max-depth=N\n"
        "              print the total for a directory only if it is N or fewer lev‐\n"
        "              els below the command line argument; user‐ and groupnames stay\n"
    )
    docs = list(man_option_docs(page))
    # The prose lines still count as mentions of -delete, for the validator, with no description.
    assert [title for title, text in docs if not text] == [
        "-delete action in the case the file has disappeared since the parent directory", "-delete"]
    assert [(title, text) for title, text in docs if text] == [
        ("-delete", "Delete files or directories; true if removal succeeded."),
        ("--max-depth=N", "print the total for a directory only if it is N or fewer levels below the command "
                          "line argument; user‐ and groupnames stay"),
    ]


def test_simple_commands_splits_pipelines_lists_and_substitutions():
    assert simple_commands("ps aux | sort -k3 && echo done") == [["ps", "aux"], ["sort", "-k3"], ["echo", "done"]]
    assert simple_commands("ls -la $(pwd) -h") == [["ls", "-la", "$", "-h"], ["pwd"]]
    assert simple_commands("kill -9 `pidof nginx` 2>/dev/null") == [["kill", "-9", "2"], ["pidof", "nginx"]]
    assert simple_commands("cat <<EOF\ntop -l\nEOF") == []


@pytest.mark.parametrize(
    "command",
    [
        "ps -eo pid,user,%cpu,%mem,comm --sort=-%cpu | head",
        "tar czf backup.tar.gz ~/docs",
        "tar -xzf a.tgz -C /opt",
        "tar xf archive.tar --wildcards '*.html'",  # option known only from a documented example
        "find . -size +1G",
        "find . -name '*.log' -mtime +30 -print -exec rm -rf {} \\;",
        "find . \\( -name a -o -name b \\) -delete",
        "ls -la $(pwd) -h",
        "ls --col",  # unambiguous prefix of --color
        "grep -rn --include='*.py' TODO .",
        "kill -TERM 1234",
        "kill -9 1234",
        "kill -s TERM 1234",
        "head -5 file",
        "chmod -x script.sh",
        "sudo -u www-data ls -la /srv",
        "xargs -0r -I {} cp {} /tmp",
        "timeout 5 ls -l",
        "git commit -m 'x' --amend --no-verify",
        "apt install -y nginx",
        "sed -n '5p' file",
        "du -sh * | sort -rh | head -5",
        "docker run --rm -it --whatever ubuntu bash",  # options live in a subcommand we have no page for
        "for f in *.log; do gzip \"$f\"; done",
        "FOO=1 ./script.sh --anything",
        "echo -------",
        "echo ffmpeg -f mp4",
        'i=1; while [ "$i" -le 5 ]; do echo "$i"; i=$((i + 1)); done',
        "count=0; while read -r line; do count=$((count + 1)); done < file; echo $count",
        "test -f /etc/passwd && echo yes",
        "/usr/bin/find . -type f",
    ],
)
def test_valid_linux_commands_pass(command, catalog):
    assert details(command, catalog) == []


@pytest.mark.parametrize(
    "command, expected",
    [
        ("top -l 1 -s 10", ["top: -l is not a documented option"]),
        ("ls -zz", ["ls: -zz is not a documented option"]),
        ("ls --bogus", ["ls: --bogus is not a documented option"]),
        ("git commit --bogus", ["git: --bogus is not a documented option"]),
        ("sudo -u bob ls -zz /srv", ["ls: -zz is not a documented option"]),
        ("find . -name x -exec rm -q {} \\;", ["rm: -q is not a documented option"]),
        ("ps aux | frobnicate --now", ["frobnicate: not a known Linux command"]),
        ("cat file | pbcopy", ["pbcopy: not a Linux command"]),
        ("sed -i '' 's/a/b/' file", ["sed -i '' is the BSD form; GNU sed takes no separate argument"]),
        ("stat -f '%z' file", ["stat -f FORMAT is the BSD form; GNU stat uses -c"]),
        ("ifconfig en0", ["en0-style interface names are macOS"]),
    ],
)
def test_invalid_or_non_linux_commands_are_flagged(command, expected, catalog):
    assert details(command, catalog) == expected


def test_unparseable_line_is_a_syntax_issue(catalog):
    (issue,) = check('echo "unterminated', catalog)
    assert issue.kind == "syntax"


def test_each_issue_is_reported_once(catalog):
    assert check("top -l; top -l", catalog) == [Issue("top", "undocumented-option", "top: -l is not a documented option")]


def test_program_without_documented_options_is_only_checked_by_name(catalog):
    assert details("lsof -p 1234 --anything", catalog) == []
    assert details("lsoff -p 1234", catalog) == ["lsoff: not a known Linux command"]


@pytest.fixture(scope="module")
def git_catalog():
    """A catalogue where git's subcommand pages are installed, as on a real system."""
    subcommands = ["commit", "log", "status", "push", "pull", "rebase", "merge", "branch", "checkout", "switch",
                   "restore", "stash", "tag", "fetch", "clone", "init", "add", "rm", "mv", "diff", "show", "reset"]
    titles = [("git", "-C <path>"), ("git", "--version"), ("git", "--no-pager"), ("git", "-c <name>=<value>")]
    titles += [(f"git-{sub}", title) for sub in subcommands for title in ("-v, --verbose", "-q, --quiet", "-m <msg>, --message=<msg>")]
    return build_catalog(["git", "git magic", "git setup", "git commit"], titles)


@pytest.mark.parametrize("command", [
    "git commit -m 'fix'",
    "git -C /srv/repo commit -m 'fix'",  # `/srv/repo` is -C's value, not the subcommand
    "git --version",
    "git status",
])
def test_standard_git_commands_pass(command, git_catalog):
    assert details(command, git_catalog) == []


def test_git_add_on_commands_are_flagged(git_catalog):
    # tldr documents git-extras beside git; a stock system does not have it.
    assert details("git magic -m 'fix login redirect'", git_catalog) == ["git magic: not a standard command (no manual page)"]
    assert details("git setup /tmp/demo", git_catalog) == ["git setup: not a standard command (no manual page)"]


def test_add_ons_are_not_judged_where_the_subcommand_pages_are_missing(catalog):
    # The small catalogue has one git-* page: too few to tell an add-on from a gap.
    assert details("git magic", catalog) == []


def test_systemctl_verbs_outside_the_manual(tmp_path):
    from lxask.index import man_subcommands  # noqa: F401  (the build side fills these)
    from lxask.validate import VERB_PROGRAMS, load_catalog, save_catalog

    assert "systemctl" in VERB_PROGRAMS
    catalog = build_catalog(["systemctl", "sudo"], [("systemctl", "--user"), ("sudo", "-u user, --user=user")])
    assert details("systemctl inspect /etc/systemd/system", catalog) == []  # no verbs known: nothing to judge by
    for verb in ("status", "start", "stop", "restart", "enable", "daemon-reload"):
        catalog.add_subcommand("systemctl", verb)
    assert details("sudo systemctl inspect /etc/systemd/system", catalog) == ["systemctl: inspect is not a documented command"]
    assert details("systemctl --user restart app && systemctl daemon-reload", catalog) == []
    assert details("systemctl force-reload nginx", catalog) == []  # an older name, still accepted
    db = tmp_path / "docs.sqlite"
    save_catalog(db, catalog)
    assert load_catalog(db).subcommands == {"systemctl": {"status", "start", "stop", "restart", "enable", "daemon-reload"}}
