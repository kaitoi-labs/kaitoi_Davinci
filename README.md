# Kaitoi for DaVinci Resolve Studio

> **AI changed creation. Kaitoi changes production.**

Kaitoi Studio is a hybrid intelligence platform for creative work — a visual canvas where you design workflows that adapt to your vision, then scale them into real production pipelines.

This plugin brings Kaitoi's video models into the DaVinci Resolve timeline. Park the playhead on a clip, open the panel, describe the change, press **Generate**. The clip's exact range is exported, sent through a Kaitoi model, and the result lands on a new track above the original — graded, trimmed, effects and all, without leaving Resolve.

---

## What You Can Do

### Restyle or edit a clip (Video → Video)
The clip itself goes to the model. Motion is preserved; look, subject or style change with the prompt.

| Preset | Kaitoi node |
|---|---|
| Ray 3.2 Video Edit | `builtin/third_party/fal/ray35_v2v` |
| Lucy Edit Fast | `builtin/third_party/fal/lucy_edit_fast` |
| Kling O3 Edit | `builtin/third_party/fal/kling_o3_standard_v2v_edit` |
| LTX-2 Video-to-Video | `builtin/third_party/fal/ltx_2_19b_video_to_video` |
| Render to Real | `builtin/third_party/fal/ltx_render_to_real` |
| Luma Ray 3 Modify | `builtin/third_party/lumalabs/ray3_modify_video` |
| Reverse Selftest (free, local) | `builtin/video/video_reverse` |

### Generate a new shot from a frame (Image → Video)
The clip's first frame seeds a freshly generated shot.

| Preset | Kaitoi node |
|---|---|
| Seedance 2.0 | `builtin/third_party/fal/seedance_2_i2v` |
| Hailuo 2.3 Fast Pro | `builtin/third_party/fal/hailuo_23_fast_pro` |
| Kling O3 | `builtin/third_party/fal/kling_o3_standard_i2v` |
| LTX Video 2.3 Pro | `builtin/third_party/fal/ltx_video_23_pro` |
| Veo 3.1 Fast | `builtin/third_party/fal/google_veo31_fast_i2v` |
| Luma Ray 3 | `builtin/third_party/lumalabs/ray3_video` |

Any other Kaitoi node type with a video or image input and a prompt works too: presets are one line each in `graphs.py`, and the graph is wired from the node's live schema, not hard-coded pin names.

### Keep the cut intact
- **New Kaitoi track above the clip** (default): the result sits at the same record frame on a `Kaitoi N` track, in a `Kaitoi` media-pool bin. Toggle the track to compare; the source clip is never touched.
- **Append to the end** of the timeline, or **media pool only**.
- **History**: the last 50 generations, with prompt, model and file; re-open or re-import any of them.
- **Cancel** stops the export, the polling or the remote run within about a second.

### One key, every Kaitoi plugin
The API key is stored once in `~/.kaitoi/credentials.json` and shared with the other Kaitoi plugins (SketchUp, …).

---

## Why This Matters

**The timeline stays the source of truth.** What gets sent is the clip as it plays — trims, grade, Fusion effects, everything above it — rendered from the Deliver page. What comes back is placed non-destructively, so iteration is a matter of picking another model or prompt and pressing Generate again on the new track.

**Any Kaitoi model can be wired in.** The plugin reads each node's published schema at run time and connects the loader's output to the model's first video or image input and the prompt to its prompt pin. Adding a model is a one-line preset, no pin names to learn.

**It is a Kaitoi workflow, not a one-off API call.** Every generation is an inline Kaitoi graph: loader → prompt → model. Point the plugin at a saved Studio project instead (`template_project_id`) and the same panel drives a pipeline you designed visually.

---

## Quick Start

**1. Get a Kaitoi Studio account** at [kaitoi.io/studio](https://kaitoi.io/studio) and create an API key in Kaitoi Studio (files and runs read/write scopes; `account_credits:read` is optional).

**2. Install the plugin.**

```bash
git clone https://github.com/kaitoi-labs/kaitoi-resolve.git
cd kaitoi-resolve
./install.sh            # symlink into Resolve's Scripts/Edit folder
./install.sh --copy     # or copy the files instead
```

The launcher goes into the per-user scripts folder:

| OS | Folder |
|---|---|
| macOS | `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/` |
| Linux | `~/.local/share/DaVinciResolve/Fusion/Scripts/Edit/` |
| Windows | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit\` |

**3. Restart Resolve**, open a timeline, park the playhead on a clip, and run **Workspace → Scripts → Edit → Kaitoi Video**.

**4. Paste your API key** in the **Settings** tab, **Save**, **Test connection**.

**5. Verify.** Pick **Reverse Selftest** as the model, type any prompt, **Generate**. It runs a free local node and puts the reversed clip on a new track — the whole path, no credits spent.

### From a terminal

The same launcher drives a running Resolve from a shell, handy while developing:

```bash
python3 "Kaitoi Video.py"           # open the panel
python3 "Kaitoi Video.py" --check   # print the clip under the playhead and exit
```

---

## Example Usage

**"Make it rain."**
> Video → Video, Ray 3.2 Video Edit. Prompt: *"same shot at night in heavy rain, wet asphalt reflections, keep the camera move"*

The clip is exported at 720p, uploaded, run, and the edit lands on `Kaitoi 2` at the same frame. Toggle the track to compare.

**"Turn this frame into a shot."**
> Image → Video, Seedance 2.0. Prompt: *"a cat jumps into the lake, slow motion, golden hour"*

The first frame of the clip seeds a new 10-second shot, placed above the original.

**"Iterate."**
> Leave the playhead where it is and Generate again with another prompt or model.

The topmost Kaitoi track is what the viewer shows, so it is what gets picked up next — each pass builds on the last.

---

## The panel

Three tabs: **Generate**, **History**, **Settings**.

**Generate** — Clip (topmost enabled video item under the playhead, with Refresh), Mode, Model, Prompt, Place, and *Send original media* (skip the export and upload the source file as-is: fast, but ignores trims, grade and effects). While a job runs, the status bar shows a spinner, the current step and the elapsed time, and a stage strip tracks progress:

```
✓ Export   ›   ✓ Upload   ›   ◐ Graph   ›   ○ Run   ›   ○ Download   ›   ○ Place
```

**History** — one row per finished run. *Open selected* plays the file; *Re-import to timeline* places it again under the current playhead clip; *Clear* empties the list (files on disk are kept).

**Settings** — API key and base URL (shared store), work folder, export max height and quality, run timeout, poll interval. *Test connection* reads the credit balance; *Open Studio* opens the web app.

---

## How a generation runs

```
timeline clip ──1. export──▶ ~/Movies/Kaitoi/kaitoi_src_*.mp4|png
              ──2. upload──▶ POST /files/uploads → PUT signed URL → …/complete
              ──3. graph ───▶ GET /node-types/{loader, text_prompt, model}
              ──4. run ─────▶ POST /runs (inline graph + inputOverrides) → poll
              ──5. import ──▶ download → media pool "Kaitoi" bin → track "Kaitoi N"
```

1. **Export** — a Deliver-page render of exactly the clip's record range (single-clip mode, video only, scaled to `export_max_height`, H.264 MP4; a PNG of the first frame for Image → Video). The Deliver page is restored afterwards.
2. **Upload** — direct-to-storage session; no size cap, multipart handled.
3. **Graph** — built from each node's live schema; every unwired input gets its catalog default.
4. **Run** — inline graph with the uploaded file bound through `inputOverrides`, polled until terminal.
5. **Import** — the result goes into the media pool and onto the timeline.

Threading: Resolve is only ever called from the script's main thread. The panel pumps its own loop (`UIDispatcher.StepLoop()` every 50 ms): each pass dispatches clicks, advances the export, repaints the stage strip and yields to the worker thread that does the network steps (2–4).

---

## Configuration

| Item | Value |
|---|---|
| API | `https://api.studio.kaitoi.io/api/v1` ([docs](https://api.studio.kaitoi.io/api/v1/docs)) |
| Auth | Bearer API key, created in Kaitoi Studio |
| Key store | `~/.kaitoi/credentials.json` (mode 0600) — `api_key`, `base_url`, `web_url`; shared by all Kaitoi plugins |
| Overrides | `KAITOI_API_KEY` or `KAITOI_API` in the environment; `KAITOI_HOME` relocates `~/.kaitoi` |
| Plugin settings | `~/.kaitoi_resolve/config.json` |
| History / log | `~/.kaitoi_resolve/history.json`, `~/.kaitoi_resolve/plugin.log` (also echoed to Resolve's Console) |
| Work folder | `~/Movies/Kaitoi/` — exported sources and downloaded results |

Optional: run a Studio-authored project instead of the inline graph by setting `template_project_id` and `template_target_node_id` in `config.json`.

---

## Requirements

- **DaVinci Resolve Studio** — developed and tested on 21.0 (macOS); earlier versions with Python scripting and `UIManager` should work but are untested. The free edition has no external scripting and no Python access to UIManager.
- **Python 3.8+** on the path Resolve uses for scripts. Standard library only — no packages to install.
- A [Kaitoi Studio](https://kaitoi.io/studio) account with an API key. Runs cost Kaitoi credits; the **Reverse Selftest** preset is free.

---

## Architecture

```
Kaitoi Video.py            # launcher: Workspace › Scripts › Edit entry; also a CLI
install.sh                 # link/copy into Resolve's Scripts/Edit
kaitoi_resolve/
├─ api.py                  # stdlib urllib client: files, runs, node types, credits
├─ credentials.py          # ~/.kaitoi/credentials.json (shared key store)
├─ config.py               # ~/.kaitoi_resolve/config.json + log()
├─ history.py              # ~/.kaitoi_resolve/history.json
├─ graphs.py               # model presets + inline run-graph builder
├─ resolve_bridge.py       # connect, pick clip, export range, import & place
├─ pipeline.py             # Job: export (UI thread) → upload/run/download (worker)
└─ ui.py                   # Fusion UIManager panel, StepLoop-driven
```

`resolve_bridge` knows nothing about Kaitoi; `api` knows nothing about Resolve; `pipeline` is the only module that touches both.

### Resolve scripting notes

Learned on Resolve 21 and handled in `resolve_bridge.py` / `ui.py`:

- `SetCurrentRenderFormatAndCodec` is refused until the Deliver state has been initialised once per session; the bridge warms it up and retries.
- `VideoQuality` must be an integer (0 = automatic, else kb/s) for H.264 in MP4; the documented string levels fail the whole `SetRenderSettings` call.
- `AddRenderJob` only works reliably from the Deliver page; the bridge switches pages around the export and back.
- `AppendToTimeline`'s `endFrame` is exclusive, unlike `TimelineItem.GetEnd()`.
- Menu scripts are `exec`'d without `__file__`; the launcher finds its package through the Scripts/Edit symlink.
- A `ui.Timer` never fires inside `UIDispatcher.RunLoop()` for Python; the panel uses `StepLoop()` in its own loop instead.
- Resolve calls from a worker thread block under `RunLoop` and deadlock inside Resolve; every Resolve call stays on the main thread.

### Troubleshooting

- **Run fails in ~2 s on the model node with `'NoneType' object has no attribute 'startswith'`, 0 credits charged** — that node's provider is not configured for the account. Try another preset, or check the Kaitoi dashboard. Reverse Selftest confirms the plugin itself is fine.
- **Panel does not appear when launched from a terminal** — an earlier script still holds Resolve's UI dispatcher; close its window or restart Resolve.
- **Every scripting call returns `None`** — Resolve is behind a modal dialog (for example *Cache Location Update Required* when the cache folder is on an offline volume). Dismiss it.

### Limitations

- Video first: both modes produce video; audio is neither exported nor generated yet.
- The generated clip keeps the model's duration; it is placed at the source's record frame, not conformed to its length.
- The panel does not pre-check the credit balance.

---

## Links

- **Product:** [kaitoi.io/studio](https://kaitoi.io/studio)
- **Company:** [kaitoi.io/labs](https://kaitoi.io/labs)
- **API docs:** [api.studio.kaitoi.io/api/v1/docs](https://api.studio.kaitoi.io/api/v1/docs)
- **MCP server:** [github.com/kaitoi-labs/kaitoi-mcp](https://github.com/kaitoi-labs/kaitoi-mcp)
- **Discord:** [discord.gg/3A5YfXnCH](https://discord.gg/3A5YfXnCH)

---

## About Kaitoi Labs

Kaitoi Labs, Inc. is a San Francisco–based team building hybrid intelligence tools for creative production. Our background spans AI, filmmaking, VFX, and product design. We believe the most powerful tools don't replace intuition — they amplify it.

## License

The contents of this repository are released under the [Apache License, Version 2.0](./LICENSE). Access to Kaitoi Studio and its API is governed by the Kaitoi Studio terms of service. DaVinci Resolve is a trademark of Blackmagic Design.
