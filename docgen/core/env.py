"""Minimal .env loader (KEY=VALUE lines); real environment variables win."""

from __future__ import annotations

import os
from pathlib import Path

from .. import REPO_ROOT

_LOADED = False


def load_env(path: Path | None = None) -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def env(key: str, default: str = "") -> str:
    load_env()
    return os.environ.get(key, default) or default
