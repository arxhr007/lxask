"""Turn tldr examples into question/answer material. Standard library only."""

from __future__ import annotations

import hashlib
import random
import re

def is_held_out(cmd: str, description: str) -> bool:
    """A stable ~8% of examples never reach training; they become an eval set."""
    digest = hashlib.sha1(f"{cmd}\n{description}".encode("utf-8")).digest()
    return digest[0] % 12 == 0


# Explanations: the documentation's own description, turned from an instruction
# ("Extract an archive") into a statement ("Extracts an archive.").

_NOT_CONJUGATED = {"do", "don't", "be", "is", "are", "have", "has", "can", "if", "when", "the", "a", "an", "all", "this"}


def third_person(verb: str) -> str:
    lower = verb.lower()
    if not lower.isalpha() or lower in _NOT_CONJUGATED:
        return verb
    if lower.endswith(("s", "x", "z", "ch", "sh", "o")):
        return verb + "es"
    if lower.endswith("y") and len(lower) > 1 and lower[-2] not in "aeiou":
        return verb[:-1] + "ies"
    return verb + "s"


def explain(description: str) -> str:
    words = description.strip().rstrip(".:").split()
    if not words:
        return ""
    verb = 1 if len(words) > 1 and words[0].lower().endswith("ly") else 0
    words[verb] = third_person(words[verb])
    text = " ".join(words)
    return text[0].upper() + text[1:] + "."


# Questions without a teacher model: the description, phrased a few ways.

TEMPLATES = (
    "{task}",
    "{task}",
    "how do I {task}?",
    "how to {task}",
    "how can I {task}?",
    "command to {task}",
    "what's the command to {task}?",
    "I want to {task}",
    "{task} in linux",
)


def as_task(description: str) -> str:
    """A description ready to drop into a sentence: no final period, lower-case start."""
    text = description.strip().rstrip(".:")
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        text = text[0].lower() + text[1:]
    return text


def template_question(description: str, rng: random.Random) -> str:
    return rng.choice(TEMPLATES).format(task=as_task(description))


# For uncommon tools the question has to name the tool. Nobody asking how to
# "list ports" wants `openstack port list`, and retrieval holds such tools back
# unless they are named, so an unnamed question would train exactly that mistake.

NAMED_TEMPLATES = (
    "how do I {task} with {tool}?",
    "how to {task} using {tool}",
    "{task} with {tool}",
    "{tool}: {task}",
    "use {tool} to {task}",
    "{tool} command to {task}",
)


def named_question(description: str, tool: str, rng: random.Random) -> str:
    return rng.choice(NAMED_TEMPLATES).format(task=as_task(description), tool=tool)


# Filling placeholders with realistic values, so some training questions carry
# concrete names that the answer has to use in place of the documented placeholder.

_OPTION_ALT = re.compile(r"\{\{\[([^|\]]+)\|([^\]]+)\]\}\}")
_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_BRACKET_CHOICE = re.compile(r"\[([^|\]]*)\|[^\]]*\]")
_EXTENSION = re.compile(r"(\.[A-Za-z0-9]{1,5})+$")

DIRS = ("~/projects", "/var/log", "/tmp/build", "./src", "~/Documents", "/srv/www", "~/Downloads", "/opt/app", "./data")
FILES = ("notes.txt", "data.csv", "report.pdf", "app.log", "config.yaml", "main.py", "photo.jpg", "access.log", "todo.md")
STEMS = ("backup", "archive", "report", "data", "site", "notes", "release", "photos", "logs", "project")

_USERS = ("alice", "bob", "deploy", "www-data", "maria")
_HOSTS = ("example.com", "192.168.1.20", "server1", "db.internal", "10.0.0.5")
_PATTERNS = ("error", "TODO", "timeout", "failed", "warning")
_SERVICES = ("nginx", "sshd", "docker", "cron", "postgresql")
_NUMBERS = ("5", "10", "20", "100")
VALUES: dict[str, tuple[str, ...]] = {
    "username": _USERS, "user": _USERS, "user_name": _USERS,
    "group": ("developers", "docker", "sudo", "staff"), "group_name": ("developers", "docker", "sudo", "staff"),
    "host": _HOSTS, "hostname": _HOSTS, "remote_host": _HOSTS, "ip_address": ("192.168.1.20", "10.0.0.5"),
    "ip": ("192.168.1.20", "10.0.0.5"), "domain": ("example.com", "github.com"), "example.com": ("example.org", "github.com"),
    "port": ("8080", "22", "443", "3000", "5432"), "port_number": ("8080", "22", "443", "3000"),
    "pattern": _PATTERNS, "search_pattern": _PATTERNS, "search_string": _PATTERNS, "regex": _PATTERNS,
    "regular_expression": _PATTERNS, "string": _PATTERNS, "keyword": _PATTERNS,
    "url": ("https://example.com/file.zip", "https://example.com/data.json"),
    "package": ("htop", "nginx", "curl", "git"), "package_name": ("htop", "nginx", "curl", "git"),
    "service": _SERVICES, "service_name": _SERVICES, "unit": _SERVICES,
    "pid": ("1234", "4321", "987"), "process_id": ("1234", "4321", "987"),
    "process_name": ("nginx", "python3", "firefox"),
    "interface": ("eth0", "wlan0", "enp3s0"), "interface_name": ("eth0", "wlan0", "enp3s0"),
    "branch": ("main", "develop", "feature/login"), "branch_name": ("main", "develop", "feature/login"),
    "remote": ("origin", "upstream"), "remote_name": ("origin", "upstream"),
    "number": _NUMBERS, "count": _NUMBERS, "n": _NUMBERS, "seconds": ("2", "5", "30"),
    "variable": ("PATH", "HOME", "EDITOR"), "container": ("web", "db", "redis"), "container_name": ("web", "db", "redis"),
    "image": ("nginx:latest", "ubuntu:24.04", "postgres:16"), "message": ("Fix typo", "Update docs"),
    "/dev/sdX": ("/dev/sdb", "/dev/sdc"), "/dev/sdXN": ("/dev/sdb1", "/dev/sdc1"), "/dev/sdX1": ("/dev/sdb1", "/dev/sdc1"),
}


def _value(token: str, rng: random.Random, used: set[str]) -> str | None:
    """A realistic stand-in for one placeholder word, or None if it is not understood."""
    token = _BRACKET_CHOICE.sub(r"\1", token)
    key = token.rstrip("0123456789")
    pool: tuple[str, ...] | None = VALUES.get(token) or VALUES.get(key) or VALUES.get(key.lower())
    if pool is None and "/" in token:
        base = token.rsplit("/", 1)[-1].lower()
        extension = _EXTENSION.search(base)
        if any(word in base for word in ("directory", "dir", "folder")):
            pool = DIRS
        elif extension:
            pool = tuple(stem + extension.group(0) for stem in STEMS)
        elif "file" in base:
            pool = FILES
    if pool is None:
        return None
    fresh = [value for value in pool if value not in used] or list(pool)
    choice = rng.choice(fresh)
    used.add(choice)
    return choice


def fill(command: str, rng: random.Random) -> tuple[str, list[str]] | None:
    """Replace every placeholder with a realistic value.

    Returns the command and the values put in, or None when a placeholder is not
    understood or there is nothing to fill (so the result is always trustworthy).
    """
    command = _OPTION_ALT.sub(r"\2", command)
    used: set[str] = set()
    values: list[str] = []
    failed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal failed
        words = [word for word in match.group(1).split() if word != "..."]
        chosen = [_value(word, rng, used) for word in words]
        if not chosen or None in chosen:
            failed = True
            return match.group(0)
        values.extend(chosen)
        return " ".join(chosen)

    filled = _PLACEHOLDER.sub(replace, command)
    return None if failed or not values else (filled, values)
