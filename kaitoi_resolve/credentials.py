"""Shared Kaitoi credentials: ``~/.kaitoi/credentials.json``.

One key for every Kaitoi plugin on this machine (Resolve, SketchUp, …), so
it is entered once. Resolution order for the API key:

1. ``KAITOI_API_KEY`` or ``KAITOI_API`` in the environment (CI, one-off runs);
2. ``~/.kaitoi/credentials.json`` (``KAITOI_HOME`` relocates the directory).

The file is written with mode 0600 and holds only the key and the two URLs.
Other plugins can read it as-is: ``{"api_key": "...", "base_url": "...",
"web_url": "..."}``.
"""

from __future__ import annotations

import json
import os

ENV_KEYS = ("KAITOI_API_KEY", "KAITOI_API")
DEFAULT_BASE_URL = "https://api.studio.kaitoi.io"
DEFAULT_WEB_URL = "https://studio.kaitoi.io"
FIELDS = ("api_key", "base_url", "web_url")


def shared_dir() -> str:
    return os.environ.get("KAITOI_HOME") or os.path.join(os.path.expanduser("~"), ".kaitoi")


def path() -> str:
    return os.path.join(shared_dir(), "credentials.json")


def load() -> dict[str, str]:
    """The shared file's fields (missing ones empty); never raises."""
    values = {field: "" for field in FIELDS}
    try:
        with open(path(), "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            for field in FIELDS:
                values[field] = str(stored.get(field) or "").strip()
    except (OSError, ValueError):
        pass
    return values


def save(api_key: str | None = None, base_url: str | None = None, web_url: str | None = None) -> dict[str, str]:
    """Merge the given fields into the shared file (created 0600)."""
    values = load()
    if api_key is not None:
        values["api_key"] = api_key.strip()
    if base_url is not None:
        values["base_url"] = base_url.strip()
    if web_url is not None:
        values["web_url"] = web_url.strip()
    directory = shared_dir()
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    target = path()
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(values, fh, indent=2, sort_keys=True)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return values


def env_api_key() -> str:
    for name in ENV_KEYS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def api_key() -> str:
    """Environment first, then the shared file; empty when neither is set."""
    return env_api_key() or load()["api_key"]


def source() -> str:
    """Where the current key comes from, for the Settings tab."""
    for name in ENV_KEYS:
        if os.environ.get(name, "").strip():
            return f"environment ({name})"
    if load()["api_key"]:
        return path()
    return "not set"


def base_url() -> str:
    return load()["base_url"] or DEFAULT_BASE_URL


def web_url() -> str:
    return load()["web_url"] or DEFAULT_WEB_URL
