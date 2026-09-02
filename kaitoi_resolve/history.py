"""Persisted log of recent generations: ``~/.kaitoi_resolve/history.json``.

One entry per finished run — what was sent, which model, which clip, and the
local file the result landed in — so a generation can be re-imported later
without re-running it.
"""

from __future__ import annotations

import json
import os
import time

from . import config

MAX_ENTRIES = 50


def path() -> str:
    os.makedirs(config.APP_DIR, exist_ok=True)
    return os.path.join(config.APP_DIR, "history.json")


def load() -> list[dict]:
    try:
        with open(path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def add(entry: dict) -> list[dict]:
    entry = dict(entry)
    entry.setdefault("at", time.strftime("%Y-%m-%d %H:%M:%S"))
    entries = [entry] + load()
    entries = entries[:MAX_ENTRIES]
    try:
        with open(path(), "w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=2)
    except OSError as exc:
        config.log(f"history write failed: {exc}", "WARN")
    return entries


def clear() -> list[dict]:
    try:
        with open(path(), "w", encoding="utf-8") as fh:
            fh.write("[]")
    except OSError:
        pass
    return []
