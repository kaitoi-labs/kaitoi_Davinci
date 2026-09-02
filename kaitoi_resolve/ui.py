"""The Kaitoi panel, built with Fusion's UIManager.

Three tabs — Generate, History, Settings. The window is pumped by our own
loop (``disp.StepLoop()`` + a short sleep) rather than ``disp.RunLoop()``:
in Resolve 21 a ``ui.Timer`` never fires inside ``RunLoop`` for a Python
script, so nothing could update the panel or notice Cancel. Each pass of the
loop dispatches pending UI events, advances the job (export polling and the
final import happen here, on the thread that owns Resolve), repaints the
progress strip, and sleeps briefly so the network worker thread gets the GIL.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from typing import Any

from . import api, config, credentials, graphs, history, pipeline, resolve_bridge

WINDOW_ID = "KaitoiResolveWin"
# Loop cadence. Idle: dispatch events every 50 ms. While the network worker
# runs: sleep longer, since ``time.sleep`` releases the GIL and that is when
# the worker thread actually gets to execute Python.
IDLE_SLEEP_SECONDS = 0.05
WORKER_YIELD_SECONDS = 0.1
SPINNER = "◐◓◑◒"  # renders clearly in Resolve's UI font; braille glyphs come out as specks

MODE_LABELS = [
    ("v2v", "Video → Video   (restyle / edit the clip itself)"),
    ("i2v", "Image → Video   (first frame seeds a new shot)"),
]
PLACEMENT_LABELS = [
    ("new_track", "New Kaitoi track above the clip"),
    ("append", "Append to end of timeline"),
    ("pool", "Media pool only (don't touch the timeline)"),
]


def _open_path(path: str) -> None:
    """Hand a file to the OS viewer/player."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:  # noqa: BLE001
        config.log(f"could not open {path}: {exc}", "WARN")


class Panel:
    def __init__(self, session: resolve_bridge.Session) -> None:
        self.session = session
        self.settings = config.load()
        self.clip: dict[str, Any] | None = None
        self.job: pipeline.Job | None = None
        self.last_result_path: str | None = None

        self.ui, self.disp = session.ui_manager()
        self.spinner_index = 0
        self.closed = False
        self.win = self.disp.AddWindow(
            {
                "ID": WINDOW_ID,
                "WindowTitle": "Kaitoi — generate from the timeline",
                "Geometry": [200, 150, 680, 620],
            },
            self._layout(),
        )
        self.items = self.win.GetItems()
        self._bind()
        self._load_settings_into_ui()
        self._refresh_models()
        self._refresh_history()
        self.refresh_clip(quiet=True)
        self._render_progress(None)

    # ── layout ──────────────────────────────────────────────────────────────

    def _layout(self):
        ui = self.ui
        return ui.VGroup(
            [
                ui.TabBar({"ID": "Tabs", "Weight": 0.0}),
                ui.Stack(
                    {"ID": "Pages", "Weight": 1.0},
                    [self._generate_page(), self._history_page(), self._settings_page()],
                ),
                # Progress strip: one line, fixed height, never overlaps the status row.
                ui.Label({"ID": "Steps", "Text": "", "Weight": 0.0, "MinimumSize": [0, 22], "Alignment": {"AlignHCenter": True}}),
                ui.HGroup(
                    {"Weight": 0.0},
                    [
                        ui.Label({"ID": "Status", "Text": "Ready.", "Weight": 1.0, "WordWrap": True, "MinimumSize": [0, 36]}),
                        ui.Button({"ID": "OpenResult", "Text": "Open result", "Enabled": False, "Weight": 0.0}),
                    ],
                ),
            ]
        )

    def _generate_page(self):
        ui = self.ui
        return ui.VGroup(
            [
                ui.HGroup(
                    {"Weight": 0.0},
                    [
                        ui.Label({"Text": "Clip", "Weight": 0.0, "MinimumSize": [70, 0]}),
                        ui.Label({"ID": "ClipInfo", "Text": "—", "Weight": 1.0, "WordWrap": True, "MinimumSize": [0, 36]}),
                        ui.Button({"ID": "RefreshClip", "Text": "Refresh", "Weight": 0.0, "MinimumSize": [80, 0]}),
                    ],
                ),
                ui.HGroup(
                    {"Weight": 0.0},
                    [
                        ui.Label({"Text": "Mode", "Weight": 0.0, "MinimumSize": [70, 0]}),
                        ui.ComboBox({"ID": "Mode", "Weight": 1.0}),
                    ],
                ),
                ui.HGroup(
                    {"Weight": 0.0},
                    [
                        ui.Label({"Text": "Model", "Weight": 0.0, "MinimumSize": [70, 0]}),
                        ui.ComboBox({"ID": "Model", "Weight": 1.0}),
                    ],
                ),
                ui.Label({"Text": "Prompt", "Weight": 0.0}),
                ui.TextEdit(
                    {
                        "ID": "Prompt",
                        "Weight": 1.0,
                        "MinimumSize": [0, 90],
                        "PlaceholderText": "Describe what should change: "
                        "\"restyle as a rainy neon night, keep the camera move\"",
                    }
                ),
                ui.HGroup(
                    {"Weight": 0.0},
                    [
                        ui.Label({"Text": "Place", "Weight": 0.0, "MinimumSize": [70, 0]}),
                        ui.ComboBox({"ID": "Placement", "Weight": 1.0}),
                    ],
                ),
                ui.HGroup(
                    {"Weight": 0.0},
                    [
                        ui.CheckBox(
                            {
                                "ID": "UseSource",
                                "Text": "Send original media instead of rendering the clip range",
                                "Checked": False,
                                "Weight": 1.0,
                            }
                        )
                    ],
                ),
                ui.HGroup(
                    {"Weight": 0.0},
                    [
                        ui.Button({"ID": "Generate", "Text": "Generate", "Weight": 1.0, "MinimumSize": [0, 32]}),
                        ui.Button({"ID": "Cancel", "Text": "Cancel", "Enabled": False, "Weight": 0.0, "MinimumSize": [90, 32]}),
                    ],
                ),
            ]
        )

    def _history_page(self):
        ui = self.ui
        return ui.VGroup(
            [
                ui.Tree({"ID": "History", "Weight": 1.0, "AlternatingRowColors": True}),
                ui.HGroup(
                    {"Weight": 0.0},
                    [
                        ui.Button({"ID": "HistoryOpen", "Text": "Open selected", "Weight": 1.0}),
                        ui.Button({"ID": "HistoryImport", "Text": "Re-import to timeline", "Weight": 1.0}),
                        ui.Button({"ID": "HistoryClear", "Text": "Clear", "Weight": 0.0}),
                    ],
                ),
            ]
        )

    def _settings_page(self):
        ui = self.ui

        def row(label, widget):
            return ui.HGroup(
                {"Weight": 0.0},
                [ui.Label({"Text": label, "Weight": 0.0, "MinimumSize": [140, 0]}), widget],
            )

        return ui.VGroup(
            [
                row("API key", ui.LineEdit({"ID": "ApiKey", "EchoMode": "Password", "Weight": 1.0})),
                row("API base URL", ui.LineEdit({"ID": "BaseUrl", "Weight": 1.0})),
                row("Work folder", ui.LineEdit({"ID": "WorkDir", "Weight": 1.0})),
                row("Export max height", ui.LineEdit({"ID": "MaxHeight", "Weight": 1.0})),
                row("Export quality", ui.ComboBox({"ID": "Quality", "Weight": 1.0})),
                row("Run timeout (s)", ui.LineEdit({"ID": "RunTimeout", "Weight": 1.0})),
                row("Poll interval (s)", ui.LineEdit({"ID": "PollInterval", "Weight": 1.0})),
                ui.HGroup(
                    {"Weight": 0.0},
                    [
                        ui.Button({"ID": "SaveSettings", "Text": "Save", "Weight": 1.0}),
                        ui.Button({"ID": "TestConnection", "Text": "Test connection", "Weight": 1.0}),
                        ui.Button({"ID": "OpenStudio", "Text": "Open Studio", "Weight": 0.0}),
                    ],
                ),
                ui.Label(
                    {
                        "ID": "SettingsHint",
                        "Text": self._settings_hint(),
                        "Weight": 1.0,
                        "WordWrap": True,
                    }
                ),
            ]
        )

    @staticmethod
    def _settings_hint() -> str:
        return (
            f"API key: {credentials.source()}  (shared by all Kaitoi plugins; "
            f"{', '.join(credentials.ENV_KEYS)} in the environment override it)\n"
            f"Plugin settings, history and log: {config.APP_DIR}"
        )

    # ── wiring ──────────────────────────────────────────────────────────────

    def _bind(self) -> None:
        events = self.win.On
        getattr(events, WINDOW_ID).Close = self._on_close
        events.Tabs.CurrentChanged = self._on_tab
        events.RefreshClip.Clicked = lambda ev: self.refresh_clip()
        events.Mode.CurrentIndexChanged = lambda ev: self._refresh_models()
        events.Generate.Clicked = self._on_generate
        events.Cancel.Clicked = self._on_cancel
        events.OpenResult.Clicked = lambda ev: self._open_last_result()
        events.HistoryOpen.Clicked = lambda ev: self._history_action("open")
        events.HistoryImport.Clicked = lambda ev: self._history_action("import")
        events.HistoryClear.Clicked = lambda ev: (history.clear(), self._refresh_history())
        events.SaveSettings.Clicked = lambda ev: self._save_settings()
        events.TestConnection.Clicked = lambda ev: self._test_connection()
        events.OpenStudio.Clicked = lambda ev: webbrowser.open(
            str(self.settings.get("web_url") or "https://studio.kaitoi.io")
        )

        tabs = self.win.GetItems()["Tabs"]
        for title in ("Generate", "History", "Settings"):
            tabs.AddTab(title)

    def _load_settings_into_ui(self) -> None:
        items = self.items
        for key, label in MODE_LABELS:
            items["Mode"].AddItem(label)
        for key, label in PLACEMENT_LABELS:
            items["Placement"].AddItem(label)
        for quality in ("Least", "Low", "Medium", "High", "Best"):
            items["Quality"].AddItem(quality)

        items["Mode"].CurrentIndex = self._index_of(MODE_LABELS, self.settings.get("last_mode", "v2v"))
        items["Placement"].CurrentIndex = self._index_of(
            PLACEMENT_LABELS, self.settings.get("last_placement", "new_track")
        )
        qualities = ["Least", "Low", "Medium", "High", "Best"]
        wanted = str(self.settings.get("export_quality", "Medium"))
        items["Quality"].CurrentIndex = qualities.index(wanted) if wanted in qualities else 2

        items["Prompt"].PlainText = str(self.settings.get("last_prompt", ""))
        items["ApiKey"].Text = str(self.settings.get("api_key", ""))
        items["BaseUrl"].Text = str(self.settings.get("base_url", ""))
        items["WorkDir"].Text = str(self.settings.get("work_dir", ""))
        items["MaxHeight"].Text = str(self.settings.get("export_max_height", 720))
        items["RunTimeout"].Text = str(self.settings.get("run_timeout_seconds", 900))
        items["PollInterval"].Text = str(self.settings.get("poll_interval_seconds", 3.0))

    @staticmethod
    def _index_of(pairs: list[tuple[str, str]], key: str) -> int:
        for index, (candidate, _) in enumerate(pairs):
            if candidate == key:
                return index
        return 0

    @property
    def mode(self) -> str:
        return MODE_LABELS[int(self.items["Mode"].CurrentIndex)][0]

    @property
    def placement(self) -> str:
        return PLACEMENT_LABELS[int(self.items["Placement"].CurrentIndex)][0]

    @property
    def model_key(self) -> str:
        presets = graphs.MODELS_BY_MODE[self.mode]
        index = int(self.items["Model"].CurrentIndex)
        return presets[index][0] if 0 <= index < len(presets) else presets[0][0]

    def _refresh_models(self) -> None:
        """Repopulate the model list for the active mode, keeping the last pick."""
        combo = self.items["Model"]
        combo.Clear()
        mode = self.mode
        for key, node_type in graphs.MODELS_BY_MODE[mode]:
            combo.AddItem(f"{key.replace('_', ' ').title()}   ({node_type.rsplit('/', 1)[-1]})")
        remembered = str(self.settings.get(f"last_{mode}_model", ""))
        keys = [key for key, _ in graphs.MODELS_BY_MODE[mode]]
        combo.CurrentIndex = keys.index(remembered) if remembered in keys else 0

    # ── actions ─────────────────────────────────────────────────────────────

    def status(self, text: str) -> None:
        self.items["Status"].Text = text

    def _render_progress(self, snapshot: dict[str, Any] | None, *, failed: bool = False) -> None:
        """Draw the stage strip + spinner from a job snapshot (None = idle)."""
        if snapshot is None:
            self.items["Steps"].Text = "   ›   ".join(f"○ {name}" for name in pipeline.STAGES_SHORT)
            return
        stage = int(snapshot.get("stage", -1))
        running = bool(snapshot.get("running"))
        parts = []
        for index, name in enumerate(pipeline.STAGES_SHORT):
            if index < stage or (index == stage and not running and not failed):
                mark = "✓"
            elif index == stage:
                mark = "✗" if failed else SPINNER[self.spinner_index % len(SPINNER)]
            else:
                mark = "○"
            parts.append(f"{mark} {name}")
        self.items["Steps"].Text = "   ›   ".join(parts)
        if running:
            self.spinner_index += 1
            elapsed = int(snapshot.get("elapsed", 0))
            clock = f"{elapsed // 60}m{elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"
            self.status(f"{SPINNER[self.spinner_index % len(SPINNER)]} {snapshot['status']}   [{clock}]")

    def refresh_clip(self, *, quiet: bool = False) -> None:
        try:
            self.clip = resolve_bridge.current_clip(self.session)
            self.items["ClipInfo"].Text = resolve_bridge.describe_clip(self.clip)
            if not quiet:
                self.status("Clip picked up from the playhead.")
        except resolve_bridge.ResolveError as exc:
            self.clip = None
            self.items["ClipInfo"].Text = "—"
            self.status(str(exc))

    def _on_generate(self, _event=None) -> None:
        if self.job is not None and self.job.running:
            self.status("A generation is already running.")
            return
        self.refresh_clip(quiet=True)
        if self.clip is None:
            return
        prompt_text = str(self.items["Prompt"].PlainText or "").strip()
        if not prompt_text:
            self.status("Write a prompt first.")
            return
        if not str(self.settings.get("api_key", "")).strip():
            self.status("No API key — add one in the Settings tab.")
            return

        mode = self.mode
        self.settings = config.update(
            last_mode=mode,
            last_placement=self.placement,
            last_prompt=prompt_text,
            **{f"last_{mode}_model": self.model_key},
        )
        self.last_result_path = None
        self.items["OpenResult"].Enabled = False
        self.items["Generate"].Enabled = False
        self.items["Cancel"].Enabled = True

        job = pipeline.Job(
            self.session,
            self.clip,
            mode=mode,
            model_key=self.model_key,
            prompt_text=prompt_text,
            placement=self.placement,
            use_source_file=bool(self.items["UseSource"].Checked),
            settings=self.settings,
        )
        job.start()
        self.job = job
        self._render_progress(job.poll())

    def _on_cancel(self, _event=None) -> None:
        if self.job is not None and self.job.running:
            self.job.cancel()
            self.items["Cancel"].Enabled = False
            self.status("Cancelling...")

    def _on_tick(self) -> bool:
        """One heartbeat: advance the job, repaint, finish it when it lands.

        Returns True while a job is in its network phase (caller sleeps longer).
        """
        job = self.job
        if job is None:
            return False
        job.step()  # drives the export phase; Resolve calls stay on this thread
        snapshot = job.poll()
        if snapshot["running"]:
            self._render_progress(snapshot)
            return job.phase == "network"

        # The worker is done — exactly one of these branches applies.
        self.job = None
        self.items["Generate"].Enabled = True
        self.items["Cancel"].Enabled = False
        if snapshot["error"]:
            self._render_progress(snapshot, failed=True)
            first_line = snapshot["error"].splitlines()[0]
            if "\n" in snapshot["error"]:
                config.log(f"job error detail:\n{snapshot['error']}", "ERROR")
                self.status(f"{first_line}  (full traceback in {config.log_path()})")
            else:
                self.status(first_line)
            return False
        if not snapshot["result"]:
            self._render_progress(snapshot, failed=True)
            self.status("Finished with no result.")
            return False
        try:
            self._render_progress(job.poll())  # "Place on timeline" spinning
            placed = job.finish_on_main()
            self.last_result_path = job.result["path"] if job.result else None
            self.items["OpenResult"].Enabled = bool(self.last_result_path)
            where = (
                f"on V{placed['track_index']}"
                if placed and placed.get("track_index")
                else placed.get("placement", "media pool") if placed else "media pool"
            )
            self._render_progress(job.poll())  # all stages ticked
            self.status(f"Done in {int(snapshot['elapsed'])}s — placed {where}.")
            self._refresh_history()
        except resolve_bridge.ResolveError as exc:
            self._render_progress(job.poll(), failed=True)
            self.status(f"Generated, but not placed: {exc}")
        return False

    def _open_last_result(self) -> None:
        if self.last_result_path:
            _open_path(self.last_result_path)

    # ── history ─────────────────────────────────────────────────────────────

    def _refresh_history(self) -> None:
        tree = self.items["History"]
        tree.Clear()
        tree.ColumnCount = 4
        tree.SetHeaderLabels(["When", "Model", "Clip", "Prompt"])
        self._history_rows = history.load()
        for entry in self._history_rows:
            row = tree.NewItem()
            row.Text[0] = str(entry.get("at", ""))
            row.Text[1] = str(entry.get("model", ""))
            row.Text[2] = str(entry.get("clip", ""))
            row.Text[3] = str(entry.get("prompt", ""))[:120]
            tree.AddTopLevelItem(row)

    def _selected_history(self) -> dict | None:
        tree = self.items["History"]
        selected = tree.SelectedItems()
        if not selected:
            return None
        # SelectedItems() is a 1-based map; the first entry is the current row.
        first = selected[1] if 1 in selected else list(selected.values())[0]
        for entry in getattr(self, "_history_rows", []):
            if str(entry.get("at", "")) == first.Text[0] and str(entry.get("model", "")) == first.Text[1]:
                return entry
        return None

    def _history_action(self, action: str) -> None:
        entry = self._selected_history()
        if not entry:
            self.status("Select a history row first.")
            return
        path = entry.get("path")
        if not path or not os.path.isfile(path):
            self.status("That result is no longer on disk.")
            return
        if action == "open":
            _open_path(path)
            return
        self.refresh_clip(quiet=True)
        if self.clip is None:
            return
        try:
            media_item = resolve_bridge.import_media(self.session, path)
            placed = resolve_bridge.place_result(
                self.session, media_item, self.clip, placement=self.placement
            )
            track = placed.get("track_index")
            self.status(f"Re-imported {os.path.basename(path)}" + (f" on V{track}." if track else "."))
        except resolve_bridge.ResolveError as exc:
            self.status(str(exc))

    # ── settings ────────────────────────────────────────────────────────────

    def _save_settings(self) -> None:
        items = self.items
        try:
            max_height = int(float(items["MaxHeight"].Text or 720))
            run_timeout = float(items["RunTimeout"].Text or 900)
            poll_interval = float(items["PollInterval"].Text or 3.0)
        except ValueError:
            self.status("Max height, timeout and poll interval must be numbers.")
            return
        self.settings = config.update(
            api_key=str(items["ApiKey"].Text or "").strip(),
            base_url=str(items["BaseUrl"].Text or "").strip() or credentials.DEFAULT_BASE_URL,
            work_dir=str(items["WorkDir"].Text or "").strip(),
            export_max_height=max_height,
            export_quality=["Least", "Low", "Medium", "High", "Best"][int(items["Quality"].CurrentIndex)],
            run_timeout_seconds=run_timeout,
            poll_interval_seconds=poll_interval,
        )
        items["SettingsHint"].Text = self._settings_hint()
        self.status(f"Saved. API key in {credentials.path()}, plugin settings in {config.path()}")

    def _test_connection(self) -> None:
        try:
            cfg = api.Config(
                api_key=str(self.items["ApiKey"].Text or "").strip(),
                base_url=str(self.items["BaseUrl"].Text or "").strip(),
                timeout=30,
            )
            credits = api.account_credits(cfg)
            balance = credits.get("balanceCents")
            self.status(
                "Connected."
                + (f" Balance: {balance / 100:.2f} credits." if isinstance(balance, (int, float)) else "")
            )
        except api.KaitoiAPIError as exc:
            self.status(f"Connection failed: {exc}")

    # ── window ──────────────────────────────────────────────────────────────

    def _on_tab(self, event) -> None:
        self.items["Pages"].CurrentIndex = event["Index"]

    def _on_close(self, _event=None) -> None:
        if self.job is not None and self.job.running:
            self.job.cancel()
            self.job.step()  # aborts a running export now, while we still own the thread
        self.closed = True

    def show(self) -> None:
        """Show the window and pump it until closed. Blocks the caller."""
        self.win.Show()
        try:
            while not self.closed:
                self.disp.StepLoop()  # dispatch pending clicks/close events
                if self.closed:
                    break
                worker_busy = self._on_tick()
                time.sleep(WORKER_YIELD_SECONDS if worker_busy else IDLE_SLEEP_SECONDS)
        finally:
            self.win.Hide()


def launch(session: resolve_bridge.Session | None = None) -> None:
    """Open the panel. Blocks until the window is closed."""
    session = session or resolve_bridge.connect()
    Panel(session).show()
