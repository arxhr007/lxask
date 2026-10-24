"""Download the raw sources into data/raw: tldr pages, NL2Bash, and rendered man pages.

Standard library only. Run it on Linux (or WSL) so the man pages come from a real install.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"

TLDR_ZIP = "https://github.com/tldr-pages/tldr/releases/latest/download/tldr-pages.en.zip"
NL2BASH = "https://raw.githubusercontent.com/TellinaTool/nl2bash/master/data/bash/all.{ext}"
TLDR_PLATFORMS = ("common", "linux")
MAN_DIRS = ("/usr/share/man/man1", "/usr/share/man/man8")
ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "lxask-fetch"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def fetch_tldr() -> None:
    out = RAW / "tldr"
    shutil.rmtree(out, ignore_errors=True)
    count = 0
    with zipfile.ZipFile(io.BytesIO(download(TLDR_ZIP))) as archive:
        for name in archive.namelist():
            parts = Path(name).parts
            if not name.endswith(".md") or len(parts) < 2 or parts[-2] not in TLDR_PLATFORMS:
                continue
            target = out / parts[-2] / parts[-1]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
            count += 1
    print(f"tldr: {count} pages -> {out}")


def fetch_nl2bash() -> None:
    out = RAW / "nl2bash"
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("nl", "cm"):
        data = download(NL2BASH.format(ext=ext))
        (out / f"all.{ext}").write_bytes(data)
    lines = (out / "all.nl").read_text(encoding="utf-8").count("\n")
    print(f"nl2bash: {lines} pairs -> {out}")


def render_man(path: Path) -> dict | None:
    # GROFF_NO_SGR makes groff emit backspace overstrikes, which `col -b` removes,
    # instead of ANSI colour codes, which it would leave half-stripped in the text.
    env = {**os.environ, "MANWIDTH": "100", "LC_ALL": "C.UTF-8", "GROFF_NO_SGR": "1"}
    try:
        page = subprocess.run(
            ["man", "-l", str(path)], env=env, capture_output=True, timeout=60, check=True
        )
        text = subprocess.run(
            ["col", "-bx"], input=page.stdout, capture_output=True, timeout=60, check=True
        ).stdout.decode("utf-8", "replace")
    except (subprocess.SubprocessError, OSError):
        return None
    text = ANSI_SGR.sub("", text)
    stem = path.name.removesuffix(".gz")
    name, _, section = stem.rpartition(".")
    if not name or not text.strip():
        return None
    return {"name": name, "section": section, "text": text}


def fetch_man() -> None:
    """This machine's man pages, rendered. The catalogue of documented options is built
    from them, so a fuller install documents more: a minimal image (a container, Colab)
    has almost none (Debian/Ubuntu: `unminimize`, or install man-db and manpages)."""
    out = RAW / "man.jsonl.gz"
    if not shutil.which("man") or not shutil.which("col"):
        print("man: skipped, `man` and `col` are needed (run this on Linux with man-db installed)", file=sys.stderr)
        return
    paths = sorted(p for d in MAN_DIRS for p in Path(d).glob("*.[18]*") if p.is_file())
    if len(paths) < 200:
        print(f"man: only {len(paths)} pages in {', '.join(MAN_DIRS)}; is this a minimal install?", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool, gzip.open(out, "wt", encoding="utf-8") as fh:
        for page in pool.map(render_man, paths):
            if page:
                fh.write(json.dumps(page) + "\n")
                count += 1
    print(f"man: {count} of {len(paths)} pages -> {out}")


SOURCES = {"tldr": fetch_tldr, "nl2bash": fetch_nl2bash, "man": fetch_man}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", help=f"any of: {', '.join(SOURCES)} (default: all)")
    args = parser.parse_args()
    if unknown := set(args.sources) - set(SOURCES):
        parser.error(f"unknown source: {', '.join(sorted(unknown))}")
    for name in args.sources or SOURCES:
        SOURCES[name]()


if __name__ == "__main__":
    main()
