#!/bin/bash
# Records the README demo: asciinema rec lxask-demo.cast -c "scripts/demo-record.sh"
# Run with `lxask --serve` already warm so answers land in ~2 s.
stty cols 96 rows 26 2>/dev/null
type() { local s="$1"; for ((i = 0; i < ${#s}; i++)); do printf "%s" "${s:i:1}"; sleep 0.045; done; }
ask() { printf "\n\033[1;32m$\033[0m "; type "$1"; printf "\n"; sleep 0.35; eval "$1"; sleep 1.1; }
clear
printf "lxask — offline Linux command assistant\n"
sleep 0.8
ask "lxask \"extract archive.tar.gz to /tmp\""
ask "lxask \"count the words in report.txt\""
ask "lxask \"which process listens on port 8080 without lsof\""
ask "lxask \"add a proxy\""
ask "lxask \"set a proxy for git\""
printf "\n"
sleep 1.2
