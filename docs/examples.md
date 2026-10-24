# Examples

Real answers from lxask 0.9 (the round-4 model with its adapter), one run, not edited.
Most requests below got a right answer; the ones that did not are at the end, with
what was wrong. How often each kind of answer is right: [accuracy.md](accuracy.md).
The explanations under each command are left out here (`--no-explain`); a plain
`lxask "..."` prints them.

## Files

```
$ lxask "find files bigger than 1GB in my home folder"
$ find ~/ -type f -size +1G

$ lxask "show disk usage of each folder here, biggest first"
$ du -h --max-depth=1 . | sort -hr

$ lxask "delete all .tmp files in this folder"
$ find . -name '*.tmp' -exec rm {} \;
  warning: deletes every file the find expression matches

$ lxask "make a tar.gz of the logs folder"
$ tar -czf logs.tar.gz logs/

$ lxask "extract archive.tar.gz into /tmp/out"
$ tar -xzf archive.tar.gz -C /tmp/out

$ lxask "copy everything from src to backup, keeping permissions and timestamps"
$ rsync -a src/ backup/
```

## Text

```
$ lxask "count the lines in data.txt"
$ wc -l data.txt

$ lxask "show the lines containing ERROR in app.log, with line numbers"
$ grep -n 'ERROR' app.log
```

## Processes

```
$ lxask "show the 10 processes using the most memory"
$ ps -eo pid,comm,%mem --sort=-%mem | head -n 10

$ lxask "which process is listening on port 8080"
$ lsof -i :8080

$ lxask "kill all processes named node"
$ kill $(pidof node)
```

## System

```
$ lxask "show free disk space in human-readable form"
$ df -h

$ lxask "how long has this machine been running"
$ uptime --pretty

$ lxask "which Linux distribution and version is this"
$ lsb_release -a

$ lxask "search for a package named ripgrep"
$ apt list ripgrep
```

## Services, network, git

```
$ lxask "restart the ssh service and check that it is running"
1. Restart the ssh service
   $ sudo systemctl restart ssh.service

2. Check that the ssh service is running
   $ systemctl is-active ssh.service

$ lxask "download https://example.com/file.zip"
$ curl -L -o file.zip https://example.com/file.zip

$ lxask "check if port 443 is open on example.com"
$ nc -zv example.com 443

$ lxask "undo the last commit but keep the changes"
$ git reset HEAD~
```

## Ruling a program out

```
$ lxask "count the words in essay.txt without wc"
$ awk '{n+=NF} END {print n+0}' essay.txt

$ lxask "which process uses port 3000, without lsof or netstat"
$ ss -ltnp 'sport = :3000'
```

## Several steps

```
$ lxask "find the busiest process, see what files it has open, and stop it gracefully"
1. Find the process with the highest reported CPU usage
   $ ps -eo pid,comm,%cpu --sort=-%cpu | head -n 2

2. List the files open by the selected process
   $ lsof -p PID

3. Send SIGTERM to the selected process
   $ kill -TERM PID
```

`PID` is for you to fill in from the step before.

## Asking back

```
$ lxask "add a proxy"
"add a proxy" can mean several things. Which one? Ask again naming it, e.g.:
  this shell session  lxask "set an http proxy for this shell session"
  the whole system    lxask "set an http proxy system-wide for all users"
  Git                 lxask "set a proxy for git"
  APT                 lxask "set a proxy for apt"
  ...
```

## Where it slipped (same run)

These are why every command deserves a read:

| request | lxask said | what is wrong | a right command |
|---|---|---|---|
| find files changed in the last 24 hours | `git diff-files --since '24 hours ago'` | a git command for a file question | `find . -type f -mtime -1` |
| replace foo with bar in every .conf file here | `find . -name '*.conf' -exec sed 's/foo/bar/g' {} \;` | without `-i`, sed only prints the result | `find . -name '*.conf' -exec sed -i 's/foo/bar/g' {} +` |
| print the second column of data.csv | `awk '{print $2}' data.csv` | splits on spaces, not commas | `awk -F, '{print $2}' data.csv` |
| install nginx | `sudo yum install nginx` (marked `unverified: yum is not installed here`) | the wrong package manager; lxask flagged it | `sudo apt install nginx` |
| show the last 50 lines of the nginx service log | `tail -n 50 /var/log/nginx.service.log` | an invented path | `journalctl -u nginx -n 50` |
| rename every .jpeg file to .jpg | `rename .jpeg .jpg *.jpeg` | right for util-linux `rename`; Debian and Ubuntu's `rename` wants `'s/\.jpeg$/.jpg/'` | `rename 's/\.jpeg$/.jpg/' *.jpeg` |

Found one of your own? `lxask --bad "what was wrong"`, then `lxask --share`.
