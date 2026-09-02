"""Tiny urllib-based Kaitoi REST client.

DaVinci Resolve runs scripts with a plain system Python interpreter and no
guaranteed third-party packages, so this module is stdlib only. It covers
exactly the endpoints the plugin needs: file upload, run create, run poll,
run cancel, node-type schema lookup, download URL, download bytes.

Errors raise :class:`KaitoiAPIError` carrying the server-provided code and
message so the UI can show something actionable.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

USER_AGENT = "kaitoi-resolve-plugin/0.1"


class KaitoiAPIError(RuntimeError):
    def __init__(self, status: int, code: str | None, message: str, details: Any = None) -> None:
        super().__init__(f"[{status}] {code or ''} {message}".strip())
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class Config:
    """Connection settings for one API session."""

    def __init__(self, api_key: str, base_url: str, timeout: float = 120.0) -> None:
        if not api_key:
            raise KaitoiAPIError(0, "NO_API_KEY", "Set an API key in the Kaitoi > Settings tab.")
        self.api_key = api_key
        self.base_url = (base_url or "https://api.studio.kaitoi.io").rstrip("/")
        self.timeout = float(timeout)


def _request(
    config: Config,
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    json_body: Any = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    url = config.base_url + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    send_headers = {
        "Authorization": f"Bearer {config.api_key}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    payload = body
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        send_headers["Content-Type"] = "application/json"
    if headers:
        send_headers.update(headers)
    req = urllib.request.Request(url, data=payload, method=method, headers=send_headers)
    try:
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - non-JSON error body
            parsed = None
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            raise KaitoiAPIError(
                exc.code, err.get("code"), err.get("message") or exc.reason, err.get("details")
            ) from exc
        raise KaitoiAPIError(exc.code, None, exc.reason or "request failed") from exc
    except urllib.error.URLError as exc:
        raise KaitoiAPIError(0, "NETWORK", str(exc.reason)) from exc


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": "idem_" + secrets.token_hex(16)}


# ── Files ───────────────────────────────────────────────────────────────────


SMALL_UPLOAD_LIMIT = 25 * 1024 * 1024


def upload_file(
    config: Config,
    path: str,
    *,
    external_id: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Upload ``path`` and return its file record (``fileId`` …).

    Uses the direct-to-storage session flow (``POST /files/uploads`` → signed
    ``PUT`` → ``complete``), which has no per-request size cap and handles
    multipart sessions for long clips. Falls back to the legacy small-file
    route only if the session endpoint is unavailable and the file fits it.
    """
    try:
        return upload_file_session(config, path, external_id=external_id, on_status=on_status)
    except KaitoiAPIError as exc:
        if exc.status in (404, 405, 501) and os.path.getsize(path) <= SMALL_UPLOAD_LIMIT:
            return upload_file_small(config, path, external_id=external_id)
        raise


def _put_bytes(url: str, data: bytes, headers: dict[str, str], *, timeout: float) -> dict[str, str]:
    """PUT raw bytes to a signed storage URL; returns the response headers."""
    req = urllib.request.Request(url, data=data, method="PUT", headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        raise KaitoiAPIError(exc.code, "STORAGE_PUT", f"storage rejected the upload: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise KaitoiAPIError(0, "NETWORK", str(exc.reason)) from exc


def upload_file_session(
    config: Config,
    path: str,
    *,
    external_id: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Direct-to-storage upload: create session, PUT bytes, complete."""
    filename = os.path.basename(path)
    size = os.path.getsize(path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body: dict[str, Any] = {"filename": filename, "contentType": content_type, "sizeBytes": size}
    if external_id:
        body["externalId"] = external_id
    _, raw, _ = _request(config, "POST", "/api/v1/files/uploads", json_body=body, headers=_idem())
    session = json.loads(raw.decode("utf-8"))
    upload_id = session.get("uploadId")
    if not upload_id:
        raise KaitoiAPIError(500, "BAD_RESPONSE", "upload session response missing uploadId")

    put_timeout = max(config.timeout, 60.0 + size / (256 * 1024))  # ≥ 2 Mbit/s worth of patience
    complete: dict[str, Any] = {"actualSizeBytes": size}

    if session.get("storageMethod") == "multipart" and session.get("parts"):
        parts = sorted(session["parts"], key=lambda p: int(p["partNumber"]))
        part_size = int(session.get("partSizeBytes") or -(-size // len(parts)))
        etags = []
        with open(path, "rb") as fh:
            for index, part in enumerate(parts):
                chunk = fh.read(part_size)
                if on_status:
                    on_status(f"Uploading part {index + 1}/{len(parts)}...")
                headers = _put_bytes(part["url"], chunk, dict(part.get("headers") or {}), timeout=put_timeout)
                etag = headers.get("etag")
                if not etag:
                    raise KaitoiAPIError(0, "STORAGE_PUT", f"storage returned no ETag for part {part['partNumber']}")
                etags.append({"partNumber": int(part["partNumber"]), "etag": etag})
        complete["multipartParts"] = etags
        complete["actualSha256"] = _sha256(path)
    else:
        upload = session.get("upload") or {}
        if not upload.get("url"):
            raise KaitoiAPIError(500, "BAD_RESPONSE", "upload session carried no signed URL")
        with open(path, "rb") as fh:
            data = fh.read()
        _put_bytes(upload["url"], data, dict(upload.get("headers") or {}), timeout=put_timeout)

    _, raw, _ = _request(
        config, "POST", f"/api/v1/files/uploads/{urllib.parse.quote(upload_id, safe='')}/complete", json_body=complete
    )
    payload = json.loads(raw.decode("utf-8"))
    if not payload.get("fileId"):
        raise KaitoiAPIError(500, "BAD_RESPONSE", "upload completion missing fileId")
    return payload


def _sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_file_small(config: Config, path: str, *, external_id: str | None = None) -> dict[str, Any]:
    """Legacy ``POST /api/v1/files`` (multipart/form-data), capped at 25 MB."""
    size = os.path.getsize(path)
    if size > SMALL_UPLOAD_LIMIT:
        raise KaitoiAPIError(
            0,
            "FILE_TOO_LARGE",
            f"{os.path.basename(path)} is {size / 1024 / 1024:.1f} MB; the small-upload cap is 25 MB. "
            "Lower the export resolution or shorten the clip.",
        )
    filename = os.path.basename(path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        data = fh.read()
    boundary = "----kaitoi-" + secrets.token_hex(8)
    buf = io.BytesIO()
    if external_id:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(b'Content-Disposition: form-data; name="externalId"\r\n\r\n')
        buf.write(external_id.encode())
        buf.write(b"\r\n")
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    buf.write(f"Content-Type: {content_type}\r\n\r\n".encode())
    buf.write(data)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", **_idem()}
    status, raw, _ = _request(config, "POST", "/api/v1/files", body=buf.getvalue(), headers=headers)
    if status >= 300:
        raise KaitoiAPIError(status, None, "upload failed")
    payload = json.loads(raw.decode("utf-8"))
    if not payload.get("fileId"):
        raise KaitoiAPIError(500, "BAD_RESPONSE", "upload response missing fileId")
    return payload


def get_download_url(config: Config, file_id: str, *, expires_in_seconds: int = 900) -> str:
    _, raw, _ = _request(
        config,
        "GET",
        f"/api/v1/files/{file_id}/download-url",
        query={"expiresInSeconds": expires_in_seconds},
    )
    payload = json.loads(raw.decode("utf-8"))
    url = payload.get("downloadUrl") or payload.get("url")
    if not url:
        raise KaitoiAPIError(500, "BAD_RESPONSE", "download URL missing in response")
    return url


def download_to(path: str, url: str, *, timeout: float = 300.0) -> str:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(path, "wb") as fh:
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    return path


# ── Runs ────────────────────────────────────────────────────────────────────


def create_run(
    config: Config,
    graph: dict[str, Any],
    target_node_ids: list[str],
    *,
    input_overrides: dict[str, dict[str, Any]] | None = None,
    external_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"graph": graph, "targetNodeIds": target_node_ids}
    if input_overrides:
        body["inputOverrides"] = input_overrides
    if external_id:
        body["externalId"] = external_id
    _, raw, _ = _request(config, "POST", "/api/v1/runs", json_body=body, headers=_idem())
    return json.loads(raw.decode("utf-8"))


def create_run_from_project(
    config: Config,
    project_id: str,
    target_node_ids: list[str],
    *,
    input_overrides: dict[str, dict[str, Any]] | None = None,
    external_id: str | None = None,
) -> dict[str, Any]:
    """Run a project's **saved** Studio graph by id, injecting clip inputs."""
    body: dict[str, Any] = {"projectId": project_id, "targetNodeIds": target_node_ids}
    if input_overrides:
        body["inputOverrides"] = input_overrides
    if external_id:
        body["externalId"] = external_id
    _, raw, _ = _request(config, "POST", "/api/v1/runs", json_body=body, headers=_idem())
    return json.loads(raw.decode("utf-8"))


def get_run(config: Config, run_id: str) -> dict[str, Any]:
    _, raw, _ = _request(config, "GET", f"/api/v1/runs/{run_id}")
    return json.loads(raw.decode("utf-8"))


def cancel_run(config: Config, run_id: str) -> dict[str, Any]:
    _, raw, _ = _request(config, "POST", f"/api/v1/runs/{run_id}/cancel")
    return json.loads(raw.decode("utf-8"))


TERMINAL_STATES = ("succeeded", "failed", "canceled", "cancelled")


def wait_for_run(
    config: Config,
    run_id: str,
    *,
    timeout: float,
    poll_interval: float,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        if should_stop is not None and should_stop():
            raise KaitoiAPIError(0, "ABORTED", "stopped by user")
        run = get_run(config, run_id)
        if on_progress is not None:
            on_progress(run)
        if run.get("status") in TERMINAL_STATES:
            return run
        if time.monotonic() > deadline:
            raise KaitoiAPIError(0, "TIMEOUT", f"run {run_id} did not finish in {timeout:.0f}s")
        # Sleep in slices so a cancel is noticed within ~0.5 s, not a full poll.
        wake = time.monotonic() + poll_interval
        while time.monotonic() < wake:
            if should_stop is not None and should_stop():
                raise KaitoiAPIError(0, "ABORTED", "stopped by user")
            time.sleep(min(0.5, max(0.0, wake - time.monotonic())))


def run_error_detail(run: dict[str, Any]) -> str:
    err = run.get("error") or {}
    if not isinstance(err, dict):
        return f"{err} | run={run.get('id')}"
    parts = [err.get("message") or "run did not succeed"]
    if err.get("nodeId"):
        parts.append(f"node={err['nodeId']}")
    if err.get("code"):
        parts.append(f"code={err['code']}")
    parts.append(f"run={run.get('id')}")
    return " | ".join(parts)


def find_output_file_id(run: dict[str, Any], target_node_id: str) -> str | None:
    """Return the first file output produced by ``target_node_id``.

    Output keys come in two shapes: a bare pin name (``"outputVideo"``) for a
    single-target run, or ``"<nodeId>:<pin>"`` for multi-target runs. When a
    node exposes several file pins the lowest-sorting populated one wins, so
    the result is deterministic.
    """
    outputs = run.get("outputs") or {}
    if not isinstance(outputs, dict):
        return None

    def is_file(value: Any) -> bool:
        return isinstance(value, dict) and value.get("type") == "file" and bool(value.get("fileId"))

    matches = [
        (key, value["fileId"])
        for key, value in outputs.items()
        if is_file(value) and (key.startswith(f"{target_node_id}:") or ":" not in key)
    ]
    if matches:
        matches.sort()
        return matches[0][1]
    for value in outputs.values():
        if is_file(value):
            return value["fileId"]
    return None


# ── Node types ──────────────────────────────────────────────────────────────


def get_node_type(config: Config, node_type: str) -> dict[str, Any]:
    """Public schema for a node type (input/output pins, types, defaults)."""
    encoded = urllib.parse.quote(node_type, safe="")
    _, raw, _ = _request(config, "GET", f"/api/v1/node-types/{encoded}")
    return json.loads(raw.decode("utf-8"))


def account_credits(config: Config) -> dict[str, Any]:
    _, raw, _ = _request(config, "GET", "/api/v1/account/credits")
    return json.loads(raw.decode("utf-8"))


# Kaitoi pin type -> run-graph typed-pin discriminator.
_PIN_TYPE_TO_TYPED = {
    "string": "string",
    "text": "string",
    "int": "number",
    "integer": "number",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "json": "json",
    "object": "json",
    "array": "json",
    "list": "json",
}
# Pins fed by a connection or an override — never inline these as literals.
NON_LITERAL_PIN_TYPES = {"image", "video", "audio", "file", "node", "embedding", "model"}


def autofill_node_inputs(node_detail: dict[str, Any], *, skip_pins: set[str]) -> dict[str, Any]:
    """Build typed-pin literals for every declared pin not fed by a connection.

    Inline-graph nodes do **not** inherit catalog defaults at execution time:
    the executor reads every declared input by key, so a missing pin surfaces
    as a bare ``KeyError`` ('seed', 'duration', ...). Emitting a typed pin for
    each unwired input — a ``null`` pin when there is no default — keeps the
    key present without inventing a value.
    """
    inputs: dict[str, Any] = {}
    for pin in node_detail.get("inputs") or []:
        name = pin.get("name")
        if not name or name in skip_pins:
            continue
        pin_type = (pin.get("type") or "").lower()
        default = pin.get("default")
        if default is None or pin_type in NON_LITERAL_PIN_TYPES:
            inputs[name] = {"type": "null", "value": None}
            continue
        inputs[name] = {"type": _PIN_TYPE_TO_TYPED.get(pin_type, "json"), "value": default}
    return inputs


# Preferred prompt pin names, in order. Falls back to the first free-text pin.
_PROMPT_PIN_NAMES = ("prompt", "inputPrompt", "editPrompt", "textPrompt", "text", "description")
# String pins that configure the model rather than describe the shot.
_NON_PROMPT_STRING_PINS = {
    "resolution",
    "duration",
    "aspectRatio",
    "aspect_ratio",
    "quality",
    "model",
    "mode",
    "style",
    "editStrength",
    "negativePrompt",
    "negative_prompt",
}


def find_input_pin(node_detail: dict[str, Any], pin_type: str) -> str | None:
    """First input pin of ``pin_type`` ("video" / "image"), in declaration order."""
    for pin in node_detail.get("inputs") or []:
        if (pin.get("type") or "").lower() == pin_type.lower():
            return pin.get("name")
    return None


def find_prompt_pin(node_detail: dict[str, Any]) -> str | None:
    """The pin that takes the user's prompt.

    Model nodes carry several string pins (``resolution``, ``duration``…), so
    a name-preference pass runs first; only then does it fall back to the first
    string pin that is neither a known knob nor pre-filled with a default.
    """
    pins = node_detail.get("inputs") or []
    by_name = {p.get("name"): p for p in pins}
    for candidate in _PROMPT_PIN_NAMES:
        if candidate in by_name:
            return candidate
    for pin in pins:
        name = pin.get("name") or ""
        if (pin.get("type") or "").lower() not in ("string", "text"):
            continue
        if name in _NON_PROMPT_STRING_PINS:
            continue
        return name
    return None


def find_output_pin(node_detail: dict[str, Any], pin_type: str, *, default: str | None = None) -> str | None:
    """First output pin of ``pin_type``, ignoring the internal ``self`` pin."""
    for pin in node_detail.get("outputs") or []:
        name = pin.get("name")
        if name == "self":
            continue
        if (pin.get("type") or "").lower() == pin_type.lower():
            return name
    return default
