"""Where the model and the docs index live."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

MODEL_FILENAME = "model.gguf"
EMBED_FILENAME = "embed.gguf"
INDEX_FILENAME = "docs.sqlite"
ADAPTER_FILENAME = "restrict.gguf"  # the restriction adapter for the installed model (lxask.route)


def setting(name: str) -> str | None:
    """The environment setting LXASK_<name>, else LINU_<name> (its name up to 0.8, when
    lxask was called linu). An empty value is returned as set."""
    for prefix in ("LXASK_", "LINU_"):
        if (value := os.environ.get(prefix + name)) is not None:
            return value
    return None


def data_home() -> Path:
    if home := setting("HOME"):
        return Path(home)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    # installed as linu (0.8 and earlier) and not moved yet: install.sh moves it
    if not (base / "lxask").exists() and (base / "linu" / MODEL_FILENAME).exists():
        return base / "linu"
    return base / "lxask"


def _resolve(name: str, filename: str, dev_dir: str) -> Path:
    """The LXASK_<name> setting, then the installed location, then the dev checkout."""
    if value := setting(name):
        return Path(value)
    installed = data_home() / filename
    if installed.exists():
        return installed
    return REPO_ROOT / dev_dir / filename


def model_path() -> Path:
    return _resolve("MODEL", MODEL_FILENAME, "models")


def embed_model_path() -> Path:
    return _resolve("EMBED_MODEL", EMBED_FILENAME, "models")


def index_path() -> Path:
    return _resolve("INDEX", INDEX_FILENAME, "data")


def adapter_path() -> Path | None:
    """The adapter, if there is one; LXASK_ADAPTER=off answers without it."""
    if setting("ADAPTER") == "off":
        return None
    path = _resolve("ADAPTER", ADAPTER_FILENAME, "models")
    return path if path.exists() else None
