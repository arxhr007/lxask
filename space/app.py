"""The lxask demo: the real `lxask` program behind a Gradio page.

Questions go to one `lxask --serve` started here, so the model loads once; commands to
explain go to `lxask --explain`, which needs no model. Nothing typed here is stored or
logged by this app.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import gradio as gr

LXASK = "lxask"
REPO = "https://github.com/arxhr007/lxask"
MAX_QUESTION = 300
MAX_COMMAND = 600
ANSWER_TIMEOUT = 180
ASKED_BACK = 3

QUESTIONS = [
    "show disk usage of each folder here, biggest first",
    "find files bigger than 1GB in my home folder",
    "delete all .tmp files in this folder",
    "which process uses port 3000, without lsof or netstat",
    "show the lines containing ERROR in app.log, with line numbers",
    "undo the last commit but keep the changes",
    "find the busiest process, see what files it has open, and stop it gracefully",
    "add a proxy",
]
COMMANDS = [
    "tar -xzvf logs.tar.gz",
    "find . -name '*.tmp' -delete",
    "du -h --max-depth=1 . | sort -hr",
    "ps aux --sort=-%mem | head -n 11",
    "top -b -n 1 -l 1",
    "git push --force origin main",
]


def cpu_count() -> int:
    """CPUs this container may use: the cgroup quota, else the affinity mask."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    return max(1, len(os.sched_getaffinity(0)))


THREADS = int(os.environ.get("LXASK_THREADS") or min(cpu_count(), 8))
VERSION = subprocess.run([LXASK, "--version"], capture_output=True, text=True).stdout.strip() or "lxask"


class Server:
    """One `lxask --serve`, restarted if it stops."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def ensure(self, timeout: float = 240) -> bool:
        with self.lock:
            if self.process and self.process.poll() is None:
                return True
            self.process = subprocess.Popen(
                [LXASK, "--serve", "--idle", "0", "--threads", str(THREADS)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = self.process.stderr.readline()
                if not line:
                    break  # it exited
                if "ready" in line:
                    # keep draining stderr so the server never blocks on a full pipe
                    threading.Thread(target=self.process.stderr.read, daemon=True).start()
                    return True
            return False


SERVER = Server()

CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str, limit: int) -> str:
    return CONTROL.sub(" ", text or "").strip()[:limit]


def report_link(question: str, answer: str) -> str:
    """A prefilled "Bad answer" issue, the one `lxask --share` makes."""
    body = (
        f"**Question:**\n{question}\n\n**lxask answered:**\n\n```\n{answer}\n```\n\n"
        f"**What was wrong:**\n\n\n<sub>{VERSION}, the Hugging Face Space</sub>"
    )
    query = urllib.parse.urlencode(
        {"template": "bad-answer.md", "title": f"Bad answer: {question[:80]}", "body": body},
        quote_via=urllib.parse.quote,
    )
    return f"[Wrong answer? Report it on GitHub]({REPO}/issues/new?{query}) (a public issue; you can edit it first)"


def ask(question: str):
    question = clean(question, MAX_QUESTION)
    if not question:
        return "", "Say what you want to do, in plain words.", ""
    if not SERVER.ensure():
        return "", "The model did not start. Try again in a minute.", ""
    started = time.monotonic()
    try:
        done = subprocess.run([LXASK, "--", question], capture_output=True, text=True, timeout=ANSWER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "", "That took too long on this shared CPU. Try a shorter request.", ""
    seconds = time.monotonic() - started
    answer = done.stdout.strip()
    if done.returncode == ASKED_BACK:
        return answer, "lxask asked back: the request means different commands in different places.", ""
    if done.returncode != 0 or not answer:
        return "", "Something went wrong: " + (done.stderr.strip()[-300:] or f"exit status {done.returncode}"), ""
    if seconds < 0.5:  # about lxask itself: fixed text, no model
        status = "Answered without the model. Nothing was run."
    else:
        status = f"Answered in {seconds:.1f} s on a shared CPU (about 2 s on a laptop). Nothing was run."
    return answer, status, report_link(question, answer)


def explain(command: str):
    command = clean(command, MAX_COMMAND)
    if not command:
        return "", "Paste a command to take apart."
    started = time.monotonic()
    done = subprocess.run([LXASK, "--explain", command], capture_output=True, text=True, timeout=30)
    seconds = time.monotonic() - started
    if done.returncode != 0:
        return "", "Something went wrong: " + (done.stderr.strip()[-300:] or f"exit status {done.returncode}")
    return done.stdout.strip(), f"Explained in {seconds:.2f} s from the man pages and tldr, without the model. Nothing was run."


INTRO = f"""
# lxask
**Forgot the command? Just ask.** A 1.5B model fine-tuned for Linux commands, which checks
every answer against the man pages and tldr and says what it cannot confirm.

This page runs the same program and model you install with one line, on a shared
Hugging Face CPU. On your own machine it runs offline: no account, no API key, no GPU.
[GitHub]({REPO}) · [How accurate it is]({REPO}/blob/main/docs/accuracy.md)
"""

INSTALL = f"""
**Install it** (Linux, x86_64 or arm64, about 1.1 GB, no root):
```sh
curl -fsSLO {REPO}/releases/latest/download/install.sh
sh install.sh
```
Lines starting with `$` are commands; nothing here or there is ever run for you.
`unverified:` marks what the documentation does not confirm, `warning:` what deletes or
overwrites. This demo does not store what you type. {VERSION}.
"""

with gr.Blocks(title="lxask: Linux commands, checked against the man pages", analytics_enabled=False) as demo:
    gr.Markdown(INTRO)
    with gr.Tab("Ask"):
        question = gr.Textbox(label="What do you want to do?", placeholder="show disk usage of each folder here",
                              max_length=MAX_QUESTION, lines=1)
        ask_button = gr.Button("Ask", variant="primary")
        answer = gr.Code(label="lxask", language="shell", interactive=False)
        ask_status = gr.Markdown()
        report = gr.Markdown()
        gr.Examples(QUESTIONS, inputs=question)
        for trigger in (ask_button.click, question.submit):
            trigger(ask, question, [answer, ask_status, report], concurrency_limit=1, concurrency_id="model")
    with gr.Tab("Explain a command"):
        command = gr.Textbox(label="A command you already have", placeholder="tar -xzvf logs.tar.gz",
                             max_length=MAX_COMMAND, lines=1)
        explain_button = gr.Button("Explain", variant="primary")
        explanation = gr.Code(label="lxask --explain", language="shell", interactive=False)
        explain_status = gr.Markdown()
        gr.Examples(COMMANDS, inputs=command)
        for trigger in (explain_button.click, command.submit):
            trigger(explain, command, [explanation, explain_status], concurrency_limit=4)
    gr.Markdown(INSTALL)

if __name__ == "__main__":
    threading.Thread(target=SERVER.ensure, daemon=True).start()  # load the model while the page comes up
    demo.queue(max_size=32).launch()
