"""Download GGUF models into the lxask data directory (~/.local/share/lxask).

    python scripts/fetch_models.py embed qwen2.5-coder-1.5b
    python scripts/fetch_models.py --use qwen2.5-coder-1.5b   # make it the default model

Standard library only.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from lxask import config

HF = "https://huggingface.co/{repo}/resolve/main/{file}"

# name -> (repo, file). `embed` is the retrieval encoder; the rest are candidate
# base models, all Q4_K_M and all downloadable without a Hugging Face login.
MODELS = {
    "embed": ("CompendiumLabs/bge-small-en-v1.5-gguf", "bge-small-en-v1.5-q8_0.gguf"),
    "qwen2.5-coder-1.5b": ("Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF", "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"),
    "qwen2.5-coder-0.5b": ("Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF", "qwen2.5-coder-0.5b-instruct-q4_k_m.gguf"),
    "qwen2.5-1.5b": ("Qwen/Qwen2.5-1.5B-Instruct-GGUF", "qwen2.5-1.5b-instruct-q4_k_m.gguf"),
    "qwen3-1.7b": ("unsloth/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q4_K_M.gguf"),
}


def model_file(name: str) -> Path:
    if name == "embed":
        return config.data_home() / config.EMBED_FILENAME
    return config.data_home() / "models" / f"{name}.gguf"


def download(url: str, partial: Path) -> int | None:
    """Fetch `url` into `partial`, continuing a previous attempt. Returns the full size if known."""
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "lxask-fetch"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
            if response.status == 206:  # "Content-Range: bytes 100-999/1000"
                total = int(response.headers["Content-Range"].rsplit("/", 1)[-1])
                mode = "ab"
            else:
                total = int(response.headers["Content-Length"]) if "Content-Length" in response.headers else None
                mode = "wb"
            with open(partial, mode) as out:
                try:
                    shutil.copyfileobj(response, out, length=1 << 20)
                except OSError:
                    pass  # dropped connection: the caller sees the short file and continues it
            return total
    except urllib.error.HTTPError as error:
        if offset and error.code == 416:  # nothing left to fetch: the partial is already complete
            return offset
        raise


def fetch(name: str, attempts: int = 8) -> bool:
    target = model_file(name)
    if target.exists():
        print(f"{name}: already at {target}")
        return True
    url = HF.format(repo=MODELS[name][0], file=MODELS[name][1])
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    # A connection that closes early yields a short file without any error, so the
    # size is checked against what the server announced before the file is accepted.
    for _ in range(attempts):
        try:
            total = download(url, partial)
        except OSError as error:
            print(f"{name}: download failed ({error}) from {url}", file=sys.stderr)
            return False
        size = partial.stat().st_size
        if total is None or size == total:
            partial.replace(target)
            print(f"{name}: {size / 1e6:.0f} MB -> {target}")
            return True
        if size > total:
            partial.unlink()
        print(f"{name}: {size} of {total} bytes, continuing", flush=True)
    print(f"{name}: still incomplete, run again to continue", file=sys.stderr)
    return False


def use(name: str) -> None:
    source = model_file(name)
    if not source.exists():
        sys.exit(f"{name} is not downloaded yet")
    default = config.data_home() / config.MODEL_FILENAME
    default.unlink(missing_ok=True)
    default.symlink_to(source)
    print(f"{default} -> {source}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help=f"any of: {', '.join(MODELS)}")
    parser.add_argument("--use", metavar="NAME", help="point the default model at a downloaded one")
    args = parser.parse_args()
    if unknown := set(args.names) - set(MODELS):
        parser.error(f"unknown model: {', '.join(sorted(unknown))}")
    ok = all([fetch(name) for name in args.names])
    if args.use:
        use(args.use)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
