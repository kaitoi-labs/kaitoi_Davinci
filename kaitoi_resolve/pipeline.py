"""The end-to-end job: timeline clip → Kaitoi → timeline.

Steps, in order:

1. export the clip's timeline range (or take its source file);
2. upload it to Kaitoi;
3. build the run graph from the chosen model's live schema;
4. submit the run and poll it;
5. download the result and import it back onto the timeline.

Threading rule: **Resolve is only ever called from the UI thread**; a Resolve
call from another thread blocks while the UI thread is in ``RunLoop()`` and
deadlocks when the script runs inside Resolve. So step 1 (a Deliver render) is
started on the UI thread and advanced from the panel's timer via
:meth:`Job.step`; steps 2-4 are pure network and run on a worker thread; step
5 returns to the UI thread through :meth:`Job.finish_on_main`.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Any

from . import api, config, graphs, history, resolve_bridge


# Displayed in the panel; ``Job.stage`` indexes into this list.
STAGES = (
    "Export clip",
    "Upload asset",
    "Prepare graph",
    "Run model",
    "Download result",
    "Place on timeline",
)


class Job:
    """One generation, from clip to placed result.

    Started with :meth:`start`, polled with :meth:`poll` from a UI timer, and
    completed on the UI thread with :meth:`finish_on_main`.
    """

    def __init__(
        self,
        session: resolve_bridge.Session,
        clip: dict[str, Any],
        *,
        mode: str,
        model_key: str,
        prompt_text: str,
        placement: str = "new_track",
        use_source_file: bool = False,
        settings: dict | None = None,
    ) -> None:
        self.session = session
        self.clip = clip
        self.mode = mode
        self.model_key = model_key
        self.prompt_text = prompt_text
        self.placement = placement
        self.use_source_file = use_source_file
        self.settings = settings or config.load()

        self.status = "idle"
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.placed: dict[str, Any] | None = None
        self.run_id: str | None = None
        self.started_at = 0.0
        # "export" (UI thread, tick-driven) → "network" (worker) → "done"
        self.phase = "idle"
        self.stage = -1  # index into STAGES; -1 before start
        self.local_source: str | None = None

        self._export: resolve_bridge.ExportJob | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ── lifecycle (UI thread) ───────────────────────────────────────────────

    def start(self) -> None:
        """Kick off the job. Fast: queues the export (or skips it) and returns."""
        if self.phase != "idle":
            raise RuntimeError("job already started")
        self.started_at = time.monotonic()
        self._set_stage(0, "Starting...")
        media_kind = "video" if self.mode == "v2v" else "image"
        try:
            if self.use_source_file and media_kind == "video":
                self.local_source = resolve_bridge.export_clip_source(self.clip)
                self._set_status(f"Using source file {os.path.basename(self.local_source)}")
                self._start_worker()
                return
            self._export = resolve_bridge.start_export(
                self.session,
                self.clip,
                kind=media_kind,
                out_dir=config.work_dir(),
                max_height=int(self.settings.get("export_max_height", 720)),
                video_format=str(self.settings.get("export_format", "mp4")),
                video_codec=str(self.settings.get("export_codec", "H264")),
                quality=str(self.settings.get("export_quality", "Medium")),
            )
        except resolve_bridge.ResolveError as exc:
            self._fail(str(exc))
            return
        self.phase = "export"
        self._set_status(f"Exporting clip ({self._export.width}x{self._export.height})...")

    def step(self) -> None:
        """Advance the export. Call from the UI timer; no-op outside that phase."""
        if self.phase != "export" or self._export is None:
            return
        export = self._export
        try:
            if self._stop.is_set():
                resolve_bridge.abort_export(self.session, export)
                self._fail("Cancelled.", level="WARN")
                return
            state, percent = resolve_bridge.poll_export(self.session, export)
            if state not in resolve_bridge.RENDER_DONE:
                timeout = float(self.settings.get("export_timeout_seconds", 600))
                if time.monotonic() - export.started_at > timeout:
                    resolve_bridge.abort_export(self.session, export)
                    self._fail(f"Clip export timed out after {timeout:.0f}s.")
                    return
                self._set_status(f"{STAGES[0]}: rendering range... {percent}%", log=False)
                return
            self.local_source = resolve_bridge.finish_export(self.session, export)
        except resolve_bridge.ResolveError as exc:
            self._fail(str(exc))
            return
        self._start_worker()

    def _start_worker(self) -> None:
        self.phase = "network"
        self._thread = threading.Thread(target=self._run, name="kaitoi-job", daemon=True)
        self._thread.start()

    def _fail(self, message: str, level: str = "ERROR") -> None:
        with self._lock:
            self.error = message
        self.phase = "done"
        config.log(f"job failed: {message}", level)

    def cancel(self) -> None:
        """Ask the job to stop. Cheap and non-blocking: safe from the UI thread.

        The worker notices the flag at its next checkpoint (export poll, run
        poll, between steps) and cancels the remote run itself, so no network
        call happens on the UI thread.
        """
        self._stop.set()
        self._set_status("Cancelling...")

    @property
    def running(self) -> bool:
        if self.phase == "export":
            return True
        if self.phase == "network":
            return self._thread is not None and self._thread.is_alive()
        return False

    def poll(self) -> dict[str, Any]:
        """Snapshot for the UI: safe to call from the timer every tick."""
        with self._lock:
            return {
                "status": self.status,
                "stage": self.stage,
                "error": self.error,
                "result": self.result,
                "running": self.running,
                "elapsed": time.monotonic() - self.started_at if self.started_at else 0.0,
            }

    # ── worker ──────────────────────────────────────────────────────────────

    def _config(self) -> api.Config:
        return api.Config(
            api_key=str(self.settings.get("api_key", "")),
            base_url=str(self.settings.get("base_url", "")),
            timeout=float(self.settings.get("request_timeout_seconds", 120)),
        )

    def _set_status(self, text: str, *, log: bool = True) -> None:
        with self._lock:
            self.status = text
        if log:
            config.log(text)

    def _set_stage(self, stage: int, text: str) -> None:
        with self._lock:
            self.stage = stage
        self._set_status(f"{STAGES[stage]}: {text}")

    def _run(self) -> None:
        """Worker thread: steps 2-5 minus the import. Never touches Resolve."""
        try:
            cfg = self._config()
            external_id = str(self.settings.get("external_id") or "davinci_resolve_plugin")
            local_source = self.local_source
            if not local_source:
                raise api.KaitoiAPIError(0, "NO_SOURCE", "no exported clip to send")
            self._check_stop()

            # 2. Upload.
            size_mb = os.path.getsize(local_source) / 1024 / 1024
            self._set_stage(1, f"sending {os.path.basename(local_source)} ({size_mb:.1f} MB)...")
            upload = api.upload_file(cfg, local_source, external_id=external_id, on_status=self._set_status)
            file_id = upload["fileId"]
            self._check_stop()

            # 3. Build the graph from the model's live schema.
            self._set_stage(2, "reading node schemas...")
            node_type = graphs.model_node_type(self.mode, self.model_key)
            built = graphs.build_graph(
                cfg, mode=self.mode, node_type=node_type, prompt_text=self.prompt_text
            )
            overrides = graphs.build_overrides(built["loader_pin"], file_id)
            config.log(f"graph: {graphs.describe(built)}")
            self._check_stop()

            # 4. Submit and poll.
            self._set_stage(3, f"submitting to {built['node_title']}...")
            run = api.create_run(
                cfg,
                built["graph"],
                target_node_ids=[built["target"]],
                input_overrides=overrides,
                external_id=external_id,
            )
            run_id = run.get("id")
            if not run_id:
                raise api.KaitoiAPIError(0, "BAD_RESPONSE", "run response carried no id")
            self.run_id = run_id
            run = api.wait_for_run(
                cfg,
                run_id,
                timeout=float(self.settings.get("run_timeout_seconds", 900)),
                poll_interval=float(self.settings.get("poll_interval_seconds", 3.0)),
                on_progress=lambda r: self._set_status(
                    f"{STAGES[3]}: {built['node_title']} — {r.get('status')} ({self._elapsed()})",
                    log=False,
                ),
                should_stop=self._stop.is_set,
            )
            if run.get("status") != "succeeded":
                raise api.KaitoiAPIError(0, "RUN_FAILED", api.run_error_detail(run))

            # 5. Download.
            output_kind = built["output_kind"]
            file_out = api.find_output_file_id(run, built["target"])
            if not file_out:
                raise api.KaitoiAPIError(
                    0, "NO_OUTPUT", f"run {run_id} produced no downloadable file"
                )
            config.log(f"run {run_id} succeeded in {self._elapsed()}; credits used: {run.get('creditsUsed')}")
            self._set_stage(4, "fetching generated file...")
            url = api.get_download_url(cfg, file_out)
            # File ids already carry their extension ("<hash>.mp4"); only add
            # one when they do not.
            extension = "" if os.path.splitext(file_out)[1] else (".mp4" if output_kind == "video" else ".png")
            local_result = os.path.join(
                config.work_dir(), f"kaitoi_{run_id}_{file_out}{extension}"
            )
            api.download_to(local_result, url, timeout=float(self.settings.get("request_timeout_seconds", 120)) * 4)

            with self._lock:
                self.result = {
                    "run_id": run_id,
                    "path": local_result,
                    "kind": output_kind,
                    "model": built["node_title"],
                    "node_type": node_type,
                    "source": local_source,
                }
            self._set_stage(5, f"importing {os.path.basename(local_result)}...")
        except api.KaitoiAPIError as exc:
            with self._lock:
                self.error = "Cancelled." if exc.code == "ABORTED" else str(exc)
            config.log(f"job failed: {exc}", "WARN" if exc.code == "ABORTED" else "ERROR")
            if exc.code == "ABORTED":
                self._cancel_remote()
        except resolve_bridge.ResolveError as exc:
            with self._lock:
                self.error = str(exc)
            config.log(f"job failed: {exc}", "ERROR")
        except Exception:  # noqa: BLE001 - a worker thread must never die silently
            with self._lock:
                self.error = "Unexpected error:\n" + traceback.format_exc()
            config.log(f"job crashed:\n{traceback.format_exc()}", "ERROR")
        finally:
            self.phase = "done"

    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - self.started_at)
        return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise api.KaitoiAPIError(0, "ABORTED", "stopped by user")

    def _cancel_remote(self) -> None:
        """Cancel the live Kaitoi run, if one was submitted. Worker thread only."""
        run_id = self.run_id
        if not run_id:
            return
        try:
            api.cancel_run(self._config(), run_id)
            config.log(f"cancelled run {run_id}")
        except api.KaitoiAPIError as exc:
            config.log(f"remote cancel failed for {run_id}: {exc}", "WARN")

    # ── UI thread ───────────────────────────────────────────────────────────

    def finish_on_main(self) -> dict[str, Any] | None:
        """Import the finished result and place it. Call from the UI thread.

        Media pool and timeline edits are done here rather than on the worker
        because they mutate the project the user is looking at.
        """
        if not self.result:
            return None
        path = self.result["path"]
        self._set_stage(5, "importing into the media pool...")
        media_item = resolve_bridge.import_media(self.session, path)
        placed = resolve_bridge.place_result(
            self.session, media_item, self.clip, placement=self.placement
        )
        self.placed = placed
        history.add(
            {
                "run_id": self.result["run_id"],
                "mode": self.mode,
                "model": self.result["model"],
                "node_type": self.result["node_type"],
                "prompt": self.prompt_text,
                "clip": self.clip["name"],
                "timeline": self.clip["timeline_name"],
                "record_frame": self.clip["start"],
                "placement": placed.get("placement"),
                "track_index": placed.get("track_index"),
                "path": path,
            }
        )
        return placed
