# Kaitoi — DaVinci Resolve Studio Bridge

A DaVinci Resolve Studio script (Python) that connects the clip under the
playhead with the [Kaitoi REST API](https://api.studio.kaitoi.io/api/v1/docs).

Park the playhead on a clip, open the panel, write a prompt, press
**Generate**: the clip's exact timeline range is exported, sent through a
Kaitoi video model, and the result lands on a new track above the original.

Licensed under the [Apache License, Version 2.0](LICENSE).

- **Video → Video**: restyle or edit the clip itself (Ray 3.2 Edit, Lucy Edit,
  Kling O3 Edit, LTX-2 V2V, Render-to-Real, Luma Ray 3 Modify), plus a free
  **Reverse Selftest** preset that runs a local node so the whole
  export → upload → run → import path can be checked without spending credits
- **Image → Video**: the clip's first frame seeds a new shot (Seedance 2,
  Hailuo 2.3, Kling O3, LTX 2.3 Pro, Veo 3.1 Fast, Luma Ray 3)
- **Non-destructive placement**: result goes on a `Kaitoi N` video track at
  the same record frame, into a `Kaitoi` media-pool bin; the source clip is
  untouched. Or append to the end, or media pool only.
- **History**: last 50 generations, re-openable and re-importable.
- **One API key for every Kaitoi plugin**: stored in
  `~/.kaitoi/credentials.json` and shared with the SketchUp plugin.

> Resolve is scripted through its Python API (`DaVinciResolveScript`), and the
> panel is built with Fusion's `UIManager`, so everything ships as plain Python
> with no third-party packages. Resolve **Studio** is required: the free
> edition has no external scripting and no UIManager access from Python.

---

## Install

```bash
cd kaitoi_resolve
./install.sh            # symlink into Resolve's Scripts/Edit folder (dev-friendly)
./install.sh --copy     # or copy the files instead
./install.sh --uninstall
```

The launcher is linked into the per-user scripts folder:

| OS      | Folder |
|---------|--------|
| macOS   | `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/` |
| Linux   | `~/.local/share/DaVinciResolve/Fusion/Scripts/Edit/` |
| Windows | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit\` |

Then:

1. **Restart Resolve** (it scans the Scripts folders at startup).
2. Enable scripting: *Preferences > System > General > External scripting
   using: Local* (needed for running from a terminal; menu scripts work
   regardless).
3. Open a timeline, park the playhead on a clip, and run
   **Workspace > Scripts > Edit > Kaitoi Video**.
4. First time: **Settings** tab → paste your Kaitoi API key → **Save**, then
   **Test connection**.

When creating the API key in the Kaitoi dashboard, grant it the **files**
and **runs** read/write scopes (it uploads clips, creates runs and downloads
results) plus node-type read access; `account_credits:read` is optional and
only feeds **Test connection**'s balance readout.

### From a terminal

The same launcher drives a running Resolve from a shell, which is handy while
developing:

```bash
python3 "Kaitoi Video.py"           # open the panel
python3 "Kaitoi Video.py" --check   # print the clip under the playhead, exit
```

---

## Panel

Three tabs: **Generate**, **History**, **Settings**.

### Generate
- **Clip**: the topmost enabled video item under the playhead
  (`Timeline.GetCurrentVideoItem()` first, then a playhead-frame scan).
  **Refresh** re-reads it.
- **Mode**: Video → Video or Image → Video.
- **Model**: presets per mode (see `graphs.py`); the last pick is remembered.
- **Prompt**: free text.
- **Place**: new Kaitoi track above the clip (default), append to the end of
  the timeline, or media pool only.
- **Send original media**: skip the export and upload the clip's source file
  as-is (fast, but ignores trims, grades and effects).
- **Generate** / **Cancel**: cancel stops the export (Resolve render), the
  polling, or the remote run — whichever is in flight — within about a
  second; the status bar shows *Cancelled.*

The status bar mirrors each step; **Open result** hands the downloaded file to
the OS player.

### History
One row per finished run (time, model, clip, prompt). **Open selected** plays
the file; **Re-import to timeline** places it again under the current
playhead clip using the selected placement; **Clear** empties the list (files
on disk are kept).

### Settings
API key and base URL (shared store), work folder, export max height and
quality, run timeout, poll interval. **Test connection** calls
`GET /account/credits`; **Open Studio** opens the web app.

---

## How a generation runs

```
timeline clip ──1. export──▶ ~/Movies/Kaitoi/kaitoi_src_*.mp4|png
              ──2. upload──▶ POST /files/uploads → PUT signed URL → …/complete
              ──3. graph ───▶ GET /node-types/{loader,text_prompt,model}
              ──4. run ─────▶ POST /runs (inline graph + inputOverrides) → poll
              ──5. import ──▶ download → media pool "Kaitoi" bin → track "Kaitoi N"
```

1. **Export**: a Deliver-page render of exactly the clip's record range
   (single-clip mode, video only, scaled to `export_max_height`, H.264 MP4;
   PNG of the first frame for Image → Video). The user's Deliver format,
   codec, mode and page are restored afterwards.
2. **Upload**: direct-to-storage session (no 25 MB cap; multipart handled).
3. **Graph**: built from each node's *live* schema — the loader's output pin
   is wired to the model's first `video`/`image` input, the prompt node to
   the model's prompt pin, and every other declared input is filled with its
   catalog default so the executor never sees a missing key.
4. **Run**: inline graph, the uploaded file bound through `inputOverrides`,
   polled until terminal.
5. **Import**: the result is imported and placed.

Threading: Resolve is only ever called from the UI thread. The panel pumps
its own loop (`UIDispatcher.StepLoop()` every 50 ms) instead of
`RunLoop()`; each pass dispatches clicks, advances the export (1), repaints
the stage strip, and sleeps so the worker gets the GIL. Upload, graph, run
and download (2–4) run on a worker thread that never touches Resolve; the
import (5) is back on the UI thread.

---

## Configuration files

| File | Holds |
|------|-------|
| `~/.kaitoi/credentials.json` (0600) | `api_key`, `base_url`, `web_url` — **shared by all Kaitoi plugins** |
| `~/.kaitoi_resolve/config.json` | plugin settings and last-used UI state |
| `~/.kaitoi_resolve/history.json` | Generations list |
| `~/.kaitoi_resolve/plugin.log` | everything the plugin did (also echoed to Resolve's Console) |
| `~/Movies/Kaitoi/` | exported sources and downloaded results (`work_dir`) |

`KAITOI_API_KEY` (or `KAITOI_API`) in the environment overrides the stored
key without touching the file; `KAITOI_HOME` relocates `~/.kaitoi`.

Optional: run a Studio-authored project instead of the inline graph by
setting `template_project_id` / `template_target_node_id` in `config.json`
(`api.create_run_from_project`).

---

## Architecture

```
Kaitoi Video.py            # launcher: Workspace > Scripts > Edit entry; also a CLI
install.sh                 # link/copy into Resolve's Scripts/Edit
kaitoi_resolve/
├─ api.py                  # stdlib urllib client: files, runs, node types, credits
├─ credentials.py          # ~/.kaitoi/credentials.json (shared key store)
├─ config.py               # ~/.kaitoi_resolve/config.json + log()
├─ history.py              # ~/.kaitoi_resolve/history.json
├─ graphs.py               # model presets + inline run-graph builder
├─ resolve_bridge.py       # connect, pick clip, export range, import & place
├─ pipeline.py             # Job: export → upload → run → download (worker thread)
└─ ui.py                   # Fusion UIManager panel (3 tabs, timer-driven)
```

`resolve_bridge` knows nothing about Kaitoi; `api` knows nothing about
Resolve; `pipeline` is the only module that touches both.

---

## Resolve 21 notes (learned the hard way)

- `SetCurrentRenderFormatAndCodec` is refused until the Deliver state has been
  initialised once per session (`GetCurrentRenderFormatAndCodec` reports
  `unknown`); the bridge warms it up and retries.
- `VideoQuality` must be an integer (0 = automatic, else kb/s) for H.264 in
  MP4 — the documented string levels are rejected and fail the whole
  `SetRenderSettings` call. Labels are mapped to bit rates.
- `AddRenderJob` only works reliably from the Deliver page; the bridge switches
  pages around the export and switches back.
- `AppendToTimeline`'s `endFrame` is exclusive, unlike `TimelineItem.GetEnd()`.
- Menu scripts are `exec`'d without `__file__`; the launcher finds its package
  through the Scripts/Edit symlink instead.
- A project whose cache folder is on an offline volume blocks scripting behind
  a modal *Cache Location Update Required* dialog until dismissed.
- A `ui.Timer` never fires inside `UIDispatcher.RunLoop()` for a Python
  script (tested standalone, with `Start()`, from an external process). The
  panel therefore never uses `RunLoop`; it calls `StepLoop()` in its own
  loop, which dispatches events fine.
- Resolve API calls from a worker thread block while the main thread is in
  `RunLoop()` and deadlock inside Resolve; keep every Resolve call on the
  script's main thread. `time.sleep` in the main loop is what lets a worker
  thread run at all (it releases the GIL).

## Troubleshooting

- **Run fails in ~2 s with `'NoneType' object has no attribute 'startswith'`
  on the model node, 0 credits charged**: the Kaitoi account behind the API
  key has no provider key (fal.ai / Luma …) configured and no credit balance
  for managed billing. Fix in the Kaitoi dashboard, not in the plugin. Use the
  *Reverse Selftest* preset to confirm the plugin itself is fine.
- **Status stuck on one message after Generate, Cancel ignored**: the panel
  loop is not being pumped (an old build used `RunLoop` + a timer that never
  fires). Close the window, re-run the script from the Workspace menu (it
  reloads the package from disk).
- **Panel does not appear when launched from a terminal**: an earlier script
  is still holding Resolve's UI dispatcher; close its window, or restart
  Resolve.

## Limitations

- Video only for now (the Image → Video mode still produces video). Audio is
  not exported or generated.
- The generated clip's length is whatever the model returns; it is placed at
  the source's record frame and not conformed to its duration.
- Runs cost Kaitoi credits; the panel does not pre-check the balance.
