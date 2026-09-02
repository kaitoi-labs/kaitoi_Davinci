"""Persisted plugin settings: ``~/.kaitoi_resolve/config.json``.

Resolve has no add-on preference store, so the plugin keeps its own JSON file
next to its log and history. The API key is **not** kept here: it lives in the
shared ``~/.kaitoi/credentials.json`` (see :mod:`credentials`) so every Kaitoi
plugin on the machine uses the same key. :func:`load` merges it in under
``api_key`` for convenience; :func:`save` strips it back out. The key is never
written into the Resolve project or the timeline.
"""

from __future__ import annotations

import json
import os
import time

from . import credentials

APP_DIR = os.path.join(os.path.expanduser("~"), ".kaitoi_resolve")

# Read-only view keys filled from the shared credentials, never persisted here.
SHARED_KEYS = ("api_key", "base_url", "web_url")

DEFAULTS: dict[str, object] = {
    "base_url": credentials.DEFAULT_BASE_URL,
    "web_url": credentials.DEFAULT_WEB_URL,
    "api_key": "",
    # Where rendered clips and downloaded results are written.
    "work_dir": os.path.join(os.path.expanduser("~"), "Movies", "Kaitoi"),
    "request_timeout_seconds": 120,
    "run_timeout_seconds": 900,
    "poll_interval_seconds": 3.0,
    # Clip export (what gets uploaded).
    "export_format": "mp4",
    "export_codec": "H264",
    "export_max_height": 720,
    "export_quality": "Medium",
    # Last used UI state, restored on next launch.
    "last_mode": "v2v",
    "last_v2v_model": "RAY35",
    "last_i2v_model": "SEEDANCE2",
    "last_placement": "new_track",
    "last_prompt": "",
    # Optional: run a Studio-authored project graph instead of an inline one.
    "template_project_id": "",
    "template_target_node_id": "",
    "external_id": "davinci_resolve_plugin",
}


def path() -> str:
    os.makedirs(APP_DIR, exist_ok=True)
    return os.path.join(APP_DIR, "config.json")


def log_path() -> str:
    os.makedirs(APP_DIR, exist_ok=True)
    return os.path.join(APP_DIR, "plugin.log")


def _read_file() -> dict:
    try:
        with open(path(), "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        return stored if isinstance(stored, dict) else {}
    except (OSError, ValueError):
        return {}


def _migrate_legacy_key(stored: dict) -> dict:
    """Move an API key an older version kept in this file into the shared store."""
    legacy = str(stored.get("api_key") or "").strip()
    if legacy and not credentials.load()["api_key"]:
        credentials.save(
            api_key=legacy,
            base_url=str(stored.get("base_url") or "") or None,
            web_url=str(stored.get("web_url") or "") or None,
        )
        log(f"moved API key from {path()} to {credentials.path()}")
    if any(key in stored for key in SHARED_KEYS):
        stored = {k: v for k, v in stored.items() if k not in SHARED_KEYS}
        _write_file(stored)
    return stored


def load() -> dict:
    """Plugin settings plus the shared credentials merged in (read-only view)."""
    values = dict(DEFAULTS)
    values.update(_migrate_legacy_key(_read_file()))
    shared = credentials.load()
    values["api_key"] = credentials.env_api_key() or shared["api_key"]
    values["base_url"] = shared["base_url"] or credentials.DEFAULT_BASE_URL
    values["web_url"] = shared["web_url"] or credentials.DEFAULT_WEB_URL
    return values


def _write_file(values: dict) -> None:
    target = path()
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(values, fh, indent=2, sort_keys=True)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def save(values: dict) -> dict:
    """Persist plugin settings; shared fields go to the credentials file instead."""
    shared_now = credentials.load()
    shared = {
        k: str(values[k]).strip() for k in SHARED_KEYS if k in values and str(values[k]).strip() != shared_now[k]
    }
    # A key that only came from the environment is not ours to persist.
    if shared.get("api_key") and shared["api_key"] == credentials.env_api_key():
        del shared["api_key"]
    if shared:
        credentials.save(
            api_key=shared.get("api_key"),
            base_url=shared.get("base_url"),
            web_url=shared.get("web_url"),
        )
    _write_file({k: v for k, v in values.items() if k not in SHARED_KEYS})
    return load()


def update(**changes) -> dict:
    values = load()
    values.update(changes)
    return save(values)


def work_dir() -> str:
    directory = str(load().get("work_dir") or "").strip()
    if not directory:
        directory = os.path.join(os.path.expanduser("~"), "Movies", "Kaitoi")
    os.makedirs(directory, exist_ok=True)
    return directory


def log(message: str, level: str = "INFO") -> None:
    """Append to the plugin log, then echo to stdout (Resolve's Console).

    Must never raise: it is called from the worker thread, and inside
    Resolve's embedded interpreter ``print`` from a non-main thread can throw.
    The file write comes first so the log is complete even when the echo
    fails.
    """
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] [{level}] [Kaitoi] {message}"
    try:
        with open(log_path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        print(line)
    except Exception:  # noqa: BLE001
        pass
