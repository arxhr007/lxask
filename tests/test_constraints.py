import pytest

from lxask.constraints import check, repair


def kinds(task, command):
    return [miss.kind for miss in check(task, command)]


@pytest.mark.parametrize("task, command", [
    # each literal, in the forms a correct command writes it
    ("delete .log files older than 30 days in /var/log/myapp", "find /var/log/myapp -name '*.log' -mtime +30 -delete"),
    ("delete .log files older than 30 days in /var/log/myapp", "find /var/log/myapp -name '*.log' -mmin +43200 -delete"),
    ("list .tmp files at least seven days old in ~/build", "find ~/build -name '*.tmp' -mtime +6"),
    ("list .tmp files at least seven days old in ~/build", 'find "$HOME/build" -name "*.tmp" -mtime +6'),
    ("files changed within the last 48 hours under /srv", "find /srv -mmin -2880"),
    ("journal errors from the last 2 hours", "journalctl -p err --since '2 hours ago'"),
    ("find files larger than 100 MB in /data", "find /data -size +100M"),
    ("find files larger than 1GB in /data", "find /data -size +1024M"),
    ("show the 10 biggest directories under /var", "du -h /var | sort -hr | head -n 10"),
    ("list the top five processes by memory", "ps -eo pid,%mem --sort=-%mem | head -n 6"),  # header kept
    ("which process is using port 8080", "ss -ltnp 'sport = :8080'"),
    ("test the integrity of /home/alex/backups/project.zip.", "unzip -t /home/alex/backups/project.zip"),
    ("archive /home/alex/project", "tar -czf project.tar.gz -C /home/alex project"),
    ("find backup_worker.py processes", "pgrep -af '[b]ackup_worker\\.py'"),
    ("stop the server listening on port 8080", "kill -TERM PID"),  # the port only found the process
    ("count lines containing 'health check' in app.log", "grep -c 'health check' app.log"),
    ("sort sizes such as 800K and 2G", "sort -h path/to/file"),  # examples, not a condition
    ("compress report.csv into a .gz file", "gzip -k report.csv"),  # gzip names it
    ("find big files in my home folder", "find ~ -size +1G"),
    ("show the most recent process", "ps -e --sort=-start_time | head -n 2"),
])
def test_correct_commands_pass(task, command):
    assert check(task, command) == []


@pytest.mark.parametrize("task, command, expected", [
    ("delete .log files older than 30 days in /var/log/myapp", "find /var/log/myapp -mtime +30 -delete", ["extension"]),
    ("delete .log files older than 30 days in /var/log/myapp", "find /var/log/myapp -name '*.log' -delete", ["duration"]),
    ("delete .log files older than 30 days in /var/log/myapp", "find /var/log -name '*.log' -mtime +30 -delete", ["path"]),
    ("delete all *.tmp under /tmp/build", "find /tmp/build -delete", ["extension"]),
    ("find files larger than 500 MB", "find / -size +50M", ["size"]),
    ("show the 20 most frequent errors", "grep -i error app.log | sort | uniq -c | sort -nr | head", ["count"]),
    ("check whether anything listens on port 8080", "ss -ltn", ["port"]),
    ("find files bigger than 1GB in my home folder", "find . -size +1G", ["home"]),
    ("count lines containing 'health check' in app.log", "grep -c health app.log", ["text"]),
    ("identify the process using the most CPU", "ps -eo pid,comm,%cpu --sort=-%cpu | head -n 1", ["slip"]),
])
def test_dropped_literals_are_reported(task, command, expected):
    assert kinds(task, command) == expected


def test_ps_without_a_header_may_take_one_line():
    task = "identify the process using the most CPU"
    assert check(task, "ps -eo pid,comm,%cpu --sort=-%cpu --no-headers | head -n 1") == []
    assert check(task, "ps -eo pid=,comm=,%cpu= --sort=-%cpu | head -n 1") == []
    assert check(task, "ps -eo pid,comm,%cpu --sort=-%cpu | head -n 2") == []


def test_repair_keeps_the_header_and_the_first_process():
    assert repair("ps -eo pid,comm,%cpu --sort=-%cpu | head -n 1") == "ps -eo pid,comm,%cpu --sort=-%cpu | head -n 2"
    assert repair("ps aux --sort=-%mem | head -1") == "ps aux --sort=-%mem | head -n 2"
    unchanged = ["ps -eo pid= --sort=-%cpu | head -n 1", "ps aux | head -n 10", "ls -t | head -n 1"]
    assert [repair(command) for command in unchanged] == unchanged


def test_the_message_quotes_the_request():
    (miss,) = check("delete .log files under /var/log/myapp", "find /var/log/myapp -delete")
    assert str(miss) == "the request says '.log'; the command does not have it"


@pytest.mark.parametrize("task, command", [
    ("show error lines in /var/log", "grep -i error /var/log"),
    ("search the logs for timeouts", "sudo grep timeout /var/log/nginx/ | head"),
    ("search my projects for TODO", "grep -n TODO ~/projects/"),
])
def test_grep_on_a_directory_is_a_slip(task, command):
    assert kinds(task, command) == ["slip"]


@pytest.mark.parametrize("command", [
    "grep -ri error /var/log",
    "grep -R error /etc",
    "grep -c . /home/mira/words.txt",
    "grep -v .git/ list.txt",
    "zgrep error /var/log/syslog.2.gz",
    "pgrep -u root sshd",
])
def test_grep_on_files_or_recursive_is_not(command):
    assert "slip" not in kinds("search for errors", command)


def test_repair_adds_r_to_grep_on_a_directory():
    assert repair("grep -i 'ERROR' /var/log | grep -iv 'health check' | sort") == \
        "grep -r -i 'ERROR' /var/log | grep -iv 'health check' | sort"
    assert repair("sudo grep -n x /etc") == "sudo grep -r -n x /etc"
    unchanged = ["grep -ri x /var/log", "grep x file.txt", "ps aux | grep nginx"]
    assert [repair(command) for command in unchanged] == unchanged


def test_shut_down_answered_with_a_reboot_is_a_slip():
    assert kinds("shut down the computer in 10 minutes", "shutdown --reboot +10") == ["slip"]
    assert kinds("power off the machine now", "sudo reboot") == ["slip"]
    assert kinds("shut down the computer in 10 minutes", "shutdown +10") == []
    assert kinds("reboot the server now", "sudo shutdown -h now") == ["slip"]
    assert kinds("reboot the server now", "sudo shutdown -r now") == []
    assert kinds("restart the nginx server", "sudo systemctl restart nginx") == []
    # reboot, poweroff and halt are one program; the flag decides
    assert kinds("reboot the system instead of shutting it down", "poweroff --reboot") == []
    assert kinds("halt the system and shut down the CPU", "reboot --halt") == []
    assert kinds("reboot the system", "halt --reboot") == []
    assert kinds("shut down the machine", "sudo systemctl reboot") == ["slip"]


def test_pip_for_system_packages_is_a_slip():
    assert kinds("list installed packages", "pip list") == ["slip"]
    assert kinds("list installed packages", "dpkg -l") == []
    assert kinds("list installed python packages", "pip list") == []
    assert kinds("install the packages listed in requirements.txt", "pip install -r requirements.txt") == []
    assert kinds("which installed packages can be upgraded?", "pip list --outdated") == ["slip"]
    assert kinds("install a package by name", "pip install package") == []
    assert kinds("show me packages installed in my user-site", "pip freeze --user") == []


def test_repair_installs_a_bare_crontab_entry():
    assert repair("0 3 * * * path/to/script.sh") == "(crontab -l 2>/dev/null; echo '0 3 * * * path/to/script.sh') | crontab -"
    assert repair("*/5 * * * * echo 'hi' >> /tmp/x") == \
        "(crontab -l 2>/dev/null; echo '*/5 * * * * echo '\\''hi'\\'' >> /tmp/x') | crontab -"
    assert repair("@reboot /usr/local/bin/start.sh") == "(crontab -l 2>/dev/null; echo '@reboot /usr/local/bin/start.sh') | crontab -"
    unchanged = ["crontab -e", "echo '0 3 * * * x' | crontab -", "7z a out.7z dir"]
    assert [repair(command) for command in unchanged] == unchanged


def test_grep_on_a_relative_directory_with_a_slash():
    assert kinds("files in docs that mention TODO", "grep -l 'TODO' docs/") == ["slip"]
    assert repair("grep -l 'TODO' docs/") == "grep -r -l 'TODO' docs/"
    # the pattern is not a file, even when it ends in a slash
    assert repair("grep -v .git/ list.txt") == "grep -v .git/ list.txt"
    assert repair("grep -e foo -A 3 src/") == "grep -r -e foo -A 3 src/"
    assert repair("grep -A 3 foo notes.txt") == "grep -A 3 foo notes.txt"


def test_repair_fills_in_the_line_count_of_a_bare_head_or_tail():
    assert repair("tail data.txt", "print the last 2 lines of data.txt") == "tail -n 2 data.txt"
    assert repair("head data.txt", "print the first three lines of data.txt") == "head -n 3 data.txt"
    assert repair("sort -nr x | head", "show the top 5 lines by count") == "sort -nr x | head -n 5"
    unchanged = [("tail -n 2 data.txt", "print the last 2 lines of data.txt"),
                 ("head -2 data.txt", "print the first 2 lines of data.txt"),
                 ("tail data.txt", "print the end of data.txt"),
                 ("head data.txt", "print the last 2 lines of data.txt")]
    assert [repair(command, task) for command, task in unchanged] == [command for command, _ in unchanged]


def test_repair_drops_the_placeholder_prefix_of_a_named_path():
    assert repair("rm --recursive path/to/build", "delete the directory build and everything in it") == "rm --recursive build"
    assert repair("tar -czf site.tar.gz path/to/site/", "archive the directory site") == "tar -czf site.tar.gz site"
    assert repair("rm -r path/to/directory", "delete the directory build") == "rm -r path/to/directory"
    assert repair("pip install -r path/to/requirements.txt", "install what requirements.txt lists") == "pip install -r requirements.txt"
    # a placeholder for a path to find out, not a name the task gives
    assert repair("sudo chsh --shell /path/to/shell alice", "set a default shell for alice") == "sudo chsh --shell /path/to/shell alice"
    assert repair("mkfifo path/to/pipe", "make a named pipe") == "mkfifo path/to/pipe"


def test_home_without_home_in_the_request_is_a_slip():
    assert kinds("find files larger than 1 megabyte under the data directory", 'find "$HOME/data" -size +1M') == ["slip"]
    assert kinds("find big files in my home folder", "find ~ -size +1G") == []
    assert kinds("find big files in my downloads", "find ~/Downloads -size +1G") == []
    assert kinds("edit the bash configuration in .bashrc", "nano ~/.bashrc") == []


def test_home_slip_reads_the_whole_request_and_ignores_awk():
    step = "list .log files under the project directory"
    assert kinds(step, 'find "$HOME/project" -name "*.log"') == ["slip"]
    assert check(step, 'find "$HOME/project" -name "*.log"', "in my home, clean up the project directory") == []
    assert kinds("print rows of /srv/x.tsv whose first field starts with 2025", "awk -F '\\t' '$1 ~ /^2025/' /srv/x.tsv") == []


def test_repair_turns_a_day_age_into_minutes_for_an_hours_window():
    assert repair("find . -type f -mtime 0", "find files that were changed in the last hour") == "find . -type f -mmin -60"
    assert repair("find /srv -mtime -1 -name '*.log'", "logs under /srv modified in the past 6 hours") == \
        "find /srv -mmin -360 -name '*.log'"
    assert repair("find . -ctime 0", "files changed in the last 30 minutes") == "find . -cmin -30"
    unchanged = [("find . -mtime -1", "files changed in the last day"),
                 ("find . -mmin -60", "files changed in the last hour"),
                 ("find . -mtime -7", "files from the last week, not the last hour"),
                 ("journalctl --since '1 hour ago'", "journal from the last hour")]
    assert [repair(command, task) for command, task in unchanged] == [command for command, _ in unchanged]


def test_repair_starts_find_at_home_when_the_request_says_home():
    assert repair("find . -type f -size +1G", "how do i find files bigger than 1GB in my home folder") == "find ~ -type f -size +1G"
    assert repair("find . -name '*.pdf'", "find pdf files in this folder") == "find . -name '*.pdf'"
    assert repair("find ~/Downloads -size +1G", "big files in my home directory") == "find ~/Downloads -size +1G"


@pytest.fixture
def ubuntu(monkeypatch):
    monkeypatch.setenv("LXASK_PACKAGE_MANAGERS", "apt,apt-get,dpkg")


def test_another_distributions_package_manager_is_a_slip(ubuntu):
    assert kinds("install nginx", "sudo yum install nginx") == ["slip"]
    assert kinds("remove the old kernel packages", "sudo dnf remove kernel-old") == ["slip"]
    assert kinds("install nginx", "sudo apt install nginx") == []
    # the request chose the manager or the distribution itself
    assert kinds("install nginx with dnf", "sudo dnf install nginx") == []
    assert kinds("on fedora, install nginx", "sudo dnf install nginx") == []
    assert kinds("show what is in package.rpm", "rpm -qpl package.rpm") == []
    assert kinds("in an alpine container, add curl", "apk add curl") == []
    # dpkg-query and apt-cache are the system's own tools, not other managers
    assert kinds("find which package owns /bin/ls", "dpkg-query -S /bin/ls") == []


def test_no_package_manager_known_means_no_slip(monkeypatch):
    assert kinds("install nginx", "sudo yum install nginx") == []
    monkeypatch.setenv("LXASK_PACKAGE_MANAGERS", "apt-get,dnf")  # two families: cannot tell
    assert kinds("install nginx", "sudo yum install nginx") == []


def test_repair_lists_this_systems_installed_packages(ubuntu, monkeypatch):
    assert repair("yum list installed", "list installed packages") == "dpkg -l"
    assert repair("rpm -qa | grep nginx", "is nginx installed?") == "dpkg -l | grep nginx"
    assert repair("pacman -Q", "list installed packages") == "dpkg -l"
    unchanged = [("apt list --installed", "list installed packages"),
                 ("dpkg -l", "list installed packages"),
                 ("dnf list installed", "list installed packages on fedora"),
                 ("yum list installed nginx", "is nginx installed?")]  # not the bare listing
    assert [repair(command, task) for command, task in unchanged] == [command for command, _ in unchanged]
    monkeypatch.setenv("LXASK_PACKAGE_MANAGERS", "dnf,rpm")
    assert repair("dpkg -l", "list installed packages") == "rpm -qa"


def test_repair_searches_this_systems_repositories(ubuntu, monkeypatch):
    assert repair("yum search package_name", "search for a package by name") == "apt search package_name"
    assert repair("sudo pacman -Ss nginx | head", "search packages for nginx") == "apt search nginx | head"
    unchanged = [("apt-cache search nginx", "search packages for nginx"),
                 ("dnf search nginx", "search for nginx with dnf"),
                 ("yum search", "search")]  # no terms: not a search to carry over
    assert [repair(command, task) for command, task in unchanged] == [command for command, _ in unchanged]
    monkeypatch.setenv("LXASK_PACKAGE_MANAGERS", "pacman")
    assert repair("apt search nginx", "search packages for nginx") == "pacman -Ss nginx"


@pytest.fixture
def user(monkeypatch):
    monkeypatch.setenv("LXASK_AS_ROOT", "0")


def test_repair_adds_sudo_to_commands_that_need_root(user):
    assert repair("apt update", "refresh the package lists") == "sudo apt update"
    assert repair("apt update && apt upgrade -y", "update everything") == "sudo apt update && sudo apt upgrade -y"
    assert repair("fsck /dev/sdb1", "check the file system on /dev/sdb1") == "sudo fsck /dev/sdb1"
    assert repair("mkfs.ext4 /dev/sdb1", "format /dev/sdb1 as ext4") == "sudo mkfs.ext4 /dev/sdb1"
    assert repair("systemctl restart nginx", "restart nginx") == "sudo systemctl restart nginx"
    assert repair("mount /dev/sdb1 /mnt", "mount /dev/sdb1 on /mnt") == "sudo mount /dev/sdb1 /mnt"
    assert repair("pacman -Syu", "upgrade the system") == "sudo pacman -Syu"
    assert repair("useradd -m bob", "add a user bob") == "sudo useradd -m bob"


def test_repair_leaves_sudo_out_where_it_does_not_belong(user, monkeypatch):
    unchanged = [("sudo apt update", "refresh the package lists"),
                 ("apt list --upgradable", "which packages can be upgraded"),
                 ("apt search nginx", "search for nginx packages"),
                 ("pacman -Ss nginx", "search for nginx"),
                 ("systemctl --user restart pipewire", "restart pipewire"),
                 ("systemctl status nginx", "is nginx running"),
                 ("mount | grep sdb", "is sdb mounted"),
                 ("apt-get install -y curl", "in the Dockerfile, install curl"),
                 ("apt update", "refresh the package lists, I am root"),
                 ("doas apt update", "refresh the package lists"),
                 ("echo 'apt update' >> notes.txt", "note down the update command")]
    assert [repair(command, task) for command, task in unchanged] == [command for command, _ in unchanged]
    monkeypatch.setenv("LXASK_AS_ROOT", "1")
    assert repair("apt update", "refresh the package lists") == "apt update"


def test_a_ruled_out_program_is_an_objection():
    from lxask.constraints import forbidden, programs_run

    assert forbidden("show what is listening on port 8080 without lsof or netstat") == ["lsof", "netstat"]
    assert programs_run("sudo xargs -I{} grep x {} | sh -c 'wc -l'") == ["sudo", "xargs", "grep", "sh", "wc"]
    misses = check("show what is listening on port 8080 without lsof", "sudo lsof -iTCP:8080 -sTCP:LISTEN")
    assert [m.kind for m in misses] == ["forbidden"]
    assert str(misses[0]) == "the request says without lsof; the command uses it"
    assert check("show what is listening on port 8080 without lsof", "ss -tlnp 'sport = :8080'") == []
    # a step whose task lost the restriction is still held to the request
    assert [m.kind for m in check("show what is listening on port 8080", "lsof -i :8080",
                                  "port 8080 is busy, find out why; don't use lsof")] == ["forbidden"]
