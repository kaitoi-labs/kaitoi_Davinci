"""Everything that talks to DaVinci Resolve.

Responsibilities, in the order the plugin uses them:

1. connect to the running Resolve instance (inside Resolve *or* from a shell);
2. find the clip the user means — the one under the playhead;
3. export that clip's exact timeline range to a file small enough to upload;
4. import a generated file back and lay it on the timeline non-destructively.

Nothing here knows about Kaitoi; nothing in :mod:`api` knows about Resolve.
"""

from __future__ import annotations

import glob
import os
import sys
import time
from typing import Any, Callable

from . import config

# Default install locations of the Resolve scripting module, used only when the
# plugin runs from an external shell (inside Resolve the module is already on
# the path and `resolve` is injected as a global).
_API_PATHS = {
    "darwin": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
    "win32": os.path.expandvars(r"%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"),
    "linux": "/opt/resolve/Developer/Scripting",
}
_LIB_PATHS = {
    "darwin": "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
    "win32": r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
    "linux": "/opt/resolve/libs/Fusion/fusionscript.so",
}

RENDER_DONE = ("Complete", "Failed", "Cancelled", "Canceled")


class ResolveError(RuntimeError):
    pass


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _import_scripting_module():
    try:
        import DaVinciResolveScript as dvr  # type: ignore

        return dvr
    except ImportError:
        pass
    key = _platform_key()
    api_path = os.environ.get("RESOLVE_SCRIPT_API") or _API_PATHS[key]
    lib_path = os.environ.get("RESOLVE_SCRIPT_LIB") or _LIB_PATHS[key]
    os.environ.setdefault("RESOLVE_SCRIPT_API", api_path)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", lib_path)
    modules = os.path.join(api_path, "Modules")
    if modules not in sys.path:
        sys.path.append(modules)
    try:
        import DaVinciResolveScript as dvr  # type: ignore

        return dvr
    except ImportError as exc:
        raise ResolveError(
            "Could not load the DaVinci Resolve scripting module. Is Resolve running, "
            "and is scripting enabled in Preferences > System > General?"
        ) from exc


class Session:
    """A live handle on Resolve: app, project manager, project, timeline."""

    def __init__(self, resolve: Any, dvr: Any) -> None:
        self.resolve = resolve
        self.dvr = dvr

    # ── objects ─────────────────────────────────────────────────────────────

    @property
    def project(self):
        project = self.resolve.GetProjectManager().GetCurrentProject()
        if project is None:
            raise ResolveError("No project is open in Resolve.")
        return project

    @property
    def timeline(self):
        timeline = self.project.GetCurrentTimeline()
        if timeline is None:
            raise ResolveError("No timeline is open. Open one on the Edit page and try again.")
        return timeline

    @property
    def media_pool(self):
        return self.project.GetMediaPool()

    def ui_manager(self):
        """Fusion's UIManager + a dispatcher, used to build the plugin window."""
        fusion = self.resolve.Fusion()
        if fusion is None:
            raise ResolveError("Fusion UI is unavailable; cannot build the panel.")
        ui = fusion.UIManager
        return ui, self.dvr.UIDispatcher(ui)


def connect(resolve: Any = None) -> Session:
    """Attach to the running Resolve, from inside it or from an external shell.

    ``resolve`` is the handle Resolve injects into a menu script's globals;
    pass it through when available. Otherwise (external shell, Console) the
    scripting module is asked for the running instance.
    """
    dvr = _import_scripting_module()
    if resolve is None:
        resolve = getattr(sys.modules.get("__main__"), "resolve", None)
    if resolve is None:
        resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise ResolveError("DaVinci Resolve is not running, or scripting access is disabled.")
    return Session(resolve, dvr)


# ── Timecode / frames ───────────────────────────────────────────────────────


def timecode_to_frame(timecode: str, fps: float) -> int:
    """Absolute frame number for a Resolve timecode string.

    A ``;`` separator marks drop-frame timecode, where two frame numbers are
    skipped each minute except every tenth minute — so the arithmetic differs
    from non-drop and must be handled, not approximated.
    """
    if not timecode:
        raise ValueError("empty timecode")
    drop = ";" in timecode
    parts = timecode.replace(";", ":").split(":")
    if len(parts) != 4:
        raise ValueError(f"unparseable timecode: {timecode}")
    hours, minutes, seconds, frames = (int(p) for p in parts)
    nominal = int(round(fps))
    total = ((hours * 3600) + (minutes * 60) + seconds) * nominal + frames
    if drop and nominal in (30, 60):
        dropped_per_minute = 2 if nominal == 30 else 4
        elapsed_minutes = hours * 60 + minutes
        total -= dropped_per_minute * (elapsed_minutes - elapsed_minutes // 10)
    return total


def frame_to_timecode(frame: int, fps: float) -> str:
    nominal = max(1, int(round(fps)))
    frames = int(frame) % nominal
    total_seconds = int(frame) // nominal
    return "%02d:%02d:%02d:%02d" % (
        total_seconds // 3600,
        (total_seconds // 60) % 60,
        total_seconds % 60,
        frames,
    )


def timeline_fps(timeline) -> float:
    try:
        return float(timeline.GetSetting("timelineFrameRate"))
    except (TypeError, ValueError):
        return 24.0


def timeline_resolution(timeline) -> tuple[int, int]:
    try:
        width = int(timeline.GetSetting("timelineResolutionWidth"))
        height = int(timeline.GetSetting("timelineResolutionHeight"))
        if width > 0 and height > 0:
            return width, height
    except (TypeError, ValueError):
        pass
    return 1920, 1080


# ── Clip selection ──────────────────────────────────────────────────────────


def _item_track_index(timeline, item) -> int | None:
    """Which video track an item sits on (Resolve does not expose this directly)."""
    target_id = item.GetUniqueId()
    for index in range(1, int(timeline.GetTrackCount("video")) + 1):
        for candidate in timeline.GetItemListInTrack("video", index) or []:
            if candidate.GetUniqueId() == target_id:
                return index
    return None


def _item_at_frame(timeline, frame: int) -> tuple[Any, int] | tuple[None, None]:
    """Topmost video item covering ``frame`` — the one the viewer is showing."""
    for index in range(int(timeline.GetTrackCount("video")), 0, -1):
        if timeline.GetIsTrackEnabled("video", index) is False:
            continue
        for item in timeline.GetItemListInTrack("video", index) or []:
            if item.GetStart() <= frame < item.GetEnd():
                return item, index
    return None, None


def current_clip(session: Session) -> dict[str, Any]:
    """The clip the user means: the one under the playhead on the top track.

    The playhead timecode is converted to a frame and the enabled video tracks
    are scanned from the top down, which is exactly what the viewer displays
    (so after a generation the new Kaitoi track is what gets picked, allowing
    iteration). ``Timeline.GetCurrentVideoItem()`` is only a fallback: on
    some pages it answers with the item on the *selected* track rather than
    the visible one, and on others it raises.
    """
    timeline = session.timeline
    fps = timeline_fps(timeline)
    item = None
    track_index = None

    playhead = None
    try:
        timecode = timeline.GetCurrentTimecode()
        if timecode:
            playhead = timecode_to_frame(timecode, fps)
    except Exception:  # noqa: BLE001
        playhead = None

    if playhead is not None:
        item, track_index = _item_at_frame(timeline, playhead)

    if item is None:
        try:
            item = timeline.GetCurrentVideoItem()
        except Exception:  # noqa: BLE001 - the API raises on some pages
            item = None
        if item is not None:
            track_index = _item_track_index(timeline, item)

    if item is None:
        raise ResolveError(
            "No clip under the playhead. Park the playhead over a clip on the Edit "
            "or Color page, then press Refresh."
        )

    media_item = item.GetMediaPoolItem()
    return {
        "item": item,
        "track_index": track_index or 1,
        "name": item.GetName(),
        "start": int(item.GetStart()),
        "end": int(item.GetEnd()),
        "duration": int(item.GetDuration()),
        "source_start": int(item.GetSourceStartFrame()),
        "source_end": int(item.GetSourceEndFrame()),
        "fps": fps,
        "timeline_name": timeline.GetName(),
        "file_path": media_item.GetClipProperty("File Path") if media_item else None,
        "playhead": playhead,
    }


def describe_clip(clip: dict[str, Any]) -> str:
    seconds = clip["duration"] / max(clip["fps"], 1.0)
    return (
        f"{clip['name']}  ·  V{clip['track_index']}  ·  "
        f"{frame_to_timecode(clip['start'], clip['fps'])} → "
        f"{frame_to_timecode(clip['end'], clip['fps'])}  "
        f"({clip['duration']} frames, {seconds:.1f}s @ {clip['fps']:g}fps)"
    )


# ── Export (timeline → file) ────────────────────────────────────────────────


def _scaled_size(timeline, max_height: int) -> tuple[int, int]:
    width, height = timeline_resolution(timeline)
    if max_height <= 0 or height <= max_height:
        return width, height
    scale = max_height / float(height)
    # Even dimensions: H.264 chroma subsampling rejects odd sizes.
    return max(2, int(round(width * scale)) // 2 * 2), max(2, int(max_height) // 2 * 2)


def _select_render_format(project, video_format: str, codec: str) -> bool:
    """Select a Deliver format/codec, coping with Resolve's cold Deliver state.

    Until the Deliver page has been touched once in a session,
    ``GetCurrentRenderFormatAndCodec`` reports ``unknown`` and the first
    ``SetCurrentRenderFormatAndCodec`` call is refused even for a valid pair.
    Querying the codec list (or loading a preset) initialises that state, after
    which the same call succeeds.
    """
    if project.SetCurrentRenderFormatAndCodec(video_format, codec):
        return True
    codecs = project.GetRenderCodecs(video_format) or {}
    if project.SetCurrentRenderFormatAndCodec(video_format, codec):
        return True
    if codecs and codec not in codecs.values():
        valid = ", ".join(sorted(codecs.values()))
        raise ResolveError(f"Codec '{codec}' is not available for {video_format}. Valid: {valid}")
    project.LoadRenderPreset("H.264 Master")
    return bool(project.SetCurrentRenderFormatAndCodec(video_format, codec))


# Quality label -> H.264 bit rate in kb/s at 1080p; scaled by output height.
_QUALITY_KBPS = {"Least": 2000, "Low": 4000, "Medium": 8000, "High": 16000, "Best": 32000}


def _apply_render_settings(project, settings: dict[str, Any], height: int) -> bool:
    """``SetRenderSettings`` with a tolerant ``VideoQuality``.

    The scripting docs allow quality as a label ("Medium") or a bit rate, but
    Resolve 21 refuses the label for H.264 in MP4 and rejects the whole dict.
    Retry with an equivalent bit rate, then without the key (Resolve's own
    automatic quality) so an unexpected codec still exports.
    """
    if project.SetRenderSettings(settings):
        return True
    quality = settings.get("VideoQuality")
    if isinstance(quality, str):
        kbps = _QUALITY_KBPS.get(quality, _QUALITY_KBPS["Medium"])
        kbps = max(500, int(kbps * min(1.0, height / 1080.0)))
        if project.SetRenderSettings({**settings, "VideoQuality": kbps}):
            return True
    if "VideoQuality" in settings:
        remaining = {k: v for k, v in settings.items() if k != "VideoQuality"}
        return bool(project.SetRenderSettings(remaining))
    return False


class ExportJob:
    """An in-flight Deliver render of one clip range.

    Created by :func:`start_export`, advanced by :func:`poll_export`, closed by
    :func:`finish_export` (or :func:`abort_export`). Split this way so a UI
    timer can drive it: **every Resolve call must happen on the thread that
    owns the script**, because calls from a worker thread block while the UI
    thread sits in ``UIDispatcher.RunLoop()`` (and deadlock when the script
    runs inside Resolve). Network work belongs on a worker; this does not.
    """

    def __init__(self, *, kind: str, out_dir: str, name: str, extension: str, width: int, height: int) -> None:
        self.kind = kind
        self.out_dir = out_dir
        self.name = name
        self.extension = extension
        self.width = width
        self.height = height
        self.job_id: str | None = None
        self.previous_page: str | None = None
        self.previous_format: dict[str, Any] = {}
        self.previous_mode: int | None = None
        self.started_at = time.monotonic()
        self.last_status: dict[str, Any] = {}
        self.closed = False


def start_export(
    session: Session,
    clip: dict[str, Any],
    *,
    kind: str = "video",
    out_dir: str | None = None,
    max_height: int = 720,
    video_format: str = "mp4",
    video_codec: str = "H264",
    quality: str = "Medium",
) -> ExportJob:
    """Queue and start a render of the clip's timeline range; returns quickly.

    ``kind="video"`` renders the whole range as a single clip; ``kind="image"``
    renders only its first frame as a PNG. The range comes from the timeline,
    so trims, grade and effects above the clip are baked in. On any failure the
    Deliver page is restored before raising.
    """
    project = session.project
    timeline = session.timeline
    out_dir = out_dir or config.work_dir()
    os.makedirs(out_dir, exist_ok=True)

    width, height = _scaled_size(timeline, max_height)
    job = ExportJob(
        kind=kind,
        out_dir=out_dir,
        name=f"kaitoi_src_{time.strftime('%Y%m%d_%H%M%S')}_{clip['start']}",
        extension="png" if kind == "image" else video_format,
        width=width,
        height=height,
    )
    mark_in = clip["start"]
    mark_out = clip["start"] if kind == "image" else clip["end"] - 1
    settings: dict[str, Any] = {
        "SelectAllFrames": False,
        "MarkIn": int(mark_in),
        "MarkOut": int(mark_out),
        "TargetDir": out_dir,
        "CustomName": job.name,
        "UniqueFilenameStyle": 1,  # suffix, so a re-run never silently overwrites
        "ExportVideo": True,
        "ExportAudio": False,
        "FormatWidth": width,
        "FormatHeight": height,
    }
    if kind == "video":
        settings["VideoQuality"] = quality

    # Render settings only take reliably from the Deliver page: queued from
    # another page Resolve has refused the job and, once, gone down with it.
    job.previous_page = session.resolve.GetCurrentPage()
    session.resolve.OpenPage("deliver")
    job.previous_format = project.GetCurrentRenderFormatAndCodec() or {}
    job.previous_mode = project.GetCurrentRenderMode()
    try:
        if kind == "image":
            ok = _select_render_format(project, "png", "RGB8")
        else:
            ok = _select_render_format(project, video_format, video_codec)
        if not ok:
            raise ResolveError(
                f"Resolve rejected render format/codec "
                f"({'png/RGB8' if kind == 'image' else f'{video_format}/{video_codec}'})."
            )
        project.SetCurrentRenderMode(1)  # 1 = single clip (one file for the range)
        if not _apply_render_settings(project, settings, height):
            raise ResolveError("Resolve rejected the render settings for this clip range.")
        job.job_id = project.AddRenderJob()
        if not job.job_id:
            raise ResolveError("Could not queue a render job for the clip.")
        if not project.StartRendering([job.job_id], isInteractiveMode=False):
            raise ResolveError("Resolve refused to start the export render.")
    except Exception:
        _restore_after_export(session, job)
        raise
    return job


def poll_export(session: Session, job: ExportJob) -> tuple[str, int]:
    """``(state, percent)`` of the render; state is in RENDER_DONE when over."""
    status = session.project.GetRenderJobStatus(job.job_id) or {}
    job.last_status = status
    return str(status.get("JobStatus", "Rendering")), int(status.get("CompletionPercentage", 0) or 0)


def finish_export(session: Session, job: ExportJob) -> str:
    """Restore the Deliver page and return the rendered file's path.

    Call once the state from :func:`poll_export` is terminal. Raises if the
    render did not complete.
    """
    state = str(job.last_status.get("JobStatus", "Rendering"))
    try:
        if state != "Complete":
            raise ResolveError(f"Clip export {state.lower()}: {job.last_status.get('Error') or 'no detail'}")
    finally:
        _restore_after_export(session, job)
    return _find_rendered_file(job.out_dir, job.name, job.extension)


def abort_export(session: Session, job: ExportJob) -> None:
    """Stop a running render and restore the Deliver page."""
    try:
        session.project.StopRendering()
    finally:
        _restore_after_export(session, job)


def _restore_after_export(session: Session, job: ExportJob) -> None:
    if job.closed:
        return
    job.closed = True
    project = session.project
    if job.job_id:
        project.DeleteRenderJob(job.job_id)
    previous = job.previous_format
    if previous.get("format") and previous["format"] != "unknown":
        project.SetCurrentRenderFormatAndCodec(previous["format"], previous.get("codec", ""))
    if job.previous_mode is not None:
        project.SetCurrentRenderMode(job.previous_mode)
    if job.previous_page and job.previous_page != "deliver":
        session.resolve.OpenPage(job.previous_page)


def render_clip(
    session: Session,
    clip: dict[str, Any],
    *,
    kind: str = "video",
    out_dir: str | None = None,
    max_height: int = 720,
    video_format: str = "mp4",
    video_codec: str = "H264",
    quality: str = "Medium",
    timeout: float = 600.0,
    on_status: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Blocking convenience around start/poll/finish, for scripts and tests.

    Do not call this from a worker thread while a UI loop is running; use the
    three-step form from the UI timer instead.
    """
    job = start_export(
        session,
        clip,
        kind=kind,
        out_dir=out_dir,
        max_height=max_height,
        video_format=video_format,
        video_codec=video_codec,
        quality=quality,
    )
    if on_status:
        on_status(f"Exporting clip ({job.width}x{job.height})...")
    deadline = time.monotonic() + timeout
    while True:
        state, percent = poll_export(session, job)
        if state in RENDER_DONE:
            break
        if on_status:
            on_status(f"Exporting clip... {percent}%")
        if should_stop is not None and should_stop():
            abort_export(session, job)
            raise ResolveError("Clip export cancelled.")
        if time.monotonic() > deadline:
            abort_export(session, job)
            raise ResolveError(f"Clip export timed out after {timeout:.0f}s.")
        time.sleep(0.5)
    return finish_export(session, job)


def _find_rendered_file(out_dir: str, name: str, extension: str) -> str:
    """Locate the file Resolve just wrote.

    Resolve appends its own suffix (frame numbers for stills, a uniqueness
    counter for clips), so the exact filename is not knowable in advance —
    match on the prefix and take the newest.
    """
    matches = sorted(
        glob.glob(os.path.join(out_dir, f"{name}*")),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    matches = [m for m in matches if os.path.isfile(m)]
    if not matches:
        raise ResolveError(
            f"Render reported success but no file matching '{name}*.{extension}' "
            f"appeared in {out_dir}."
        )
    return matches[0]


def export_clip_source(clip: dict[str, Any]) -> str:
    """Use the clip's original media file, skipping the render entirely.

    Fast, and lossless relative to the source, but it ignores trims, grades and
    everything else the timeline adds — so it is only offered as an explicit
    choice, and refuses when the file is missing.
    """
    path = clip.get("file_path")
    if not path or not os.path.isfile(path):
        raise ResolveError("This clip has no readable source file; export the range instead.")
    return path


# ── Import (file → timeline) ────────────────────────────────────────────────


BIN_NAME = "Kaitoi"
TRACK_PREFIX = "Kaitoi"


def _ensure_bin(session: Session):
    """Return the media pool's ``Kaitoi`` bin, creating it once if needed."""
    media_pool = session.media_pool
    root = media_pool.GetRootFolder()
    for folder in root.GetSubFolderList() or []:
        if folder.GetName() == BIN_NAME:
            return folder
    return media_pool.AddSubFolder(root, BIN_NAME) or root


def import_media(session: Session, path: str):
    """Import ``path`` into the Kaitoi bin and return its MediaPoolItem."""
    media_pool = session.media_pool
    previous = media_pool.GetCurrentFolder()
    target = _ensure_bin(session)
    try:
        media_pool.SetCurrentFolder(target)
        items = media_pool.ImportMedia([path])
    finally:
        if previous:
            media_pool.SetCurrentFolder(previous)
    if not items:
        raise ResolveError(f"Resolve could not import {os.path.basename(path)}.")
    return items[0]


def _clip_frames(media_item, fallback: int) -> int:
    try:
        frames = int(media_item.GetClipProperty("Frames") or 0)
    except (TypeError, ValueError):
        frames = 0
    # Stills report a single frame; give them the slot's length instead.
    return frames if frames > 1 else max(1, fallback)


def _free_kaitoi_track(session: Session, start: int, end: int) -> int | None:
    """An existing Kaitoi track with nothing in ``[start, end)``, if any."""
    timeline = session.timeline
    for index in range(1, int(timeline.GetTrackCount("video")) + 1):
        name = timeline.GetTrackName("video", index) or ""
        if not name.startswith(TRACK_PREFIX):
            continue
        busy = any(
            item.GetStart() < end and item.GetEnd() > start
            for item in timeline.GetItemListInTrack("video", index) or []
        )
        if not busy:
            return index
    return None


def place_result(
    session: Session,
    media_item,
    clip: dict[str, Any],
    *,
    placement: str = "new_track",
) -> dict[str, Any]:
    """Put a generated clip into the timeline.

    ``new_track`` (default) lays it on a Kaitoi track above the source at the
    same record frame: the original clip is untouched, and toggling the track
    off restores the previous cut. ``append`` drops it at the end of the
    timeline; ``pool`` leaves it in the media pool only.
    """
    if placement == "pool":
        return {"placement": "pool", "track_index": None}

    media_pool = session.media_pool
    timeline = session.timeline
    frames = _clip_frames(media_item, clip["duration"])

    if placement == "append":
        appended = media_pool.AppendToTimeline([media_item])
        if not appended:
            raise ResolveError("Resolve refused to append the result to the timeline.")
        return {"placement": "append", "track_index": None, "frames": frames}

    record_frame = clip["start"]
    track_index = _free_kaitoi_track(session, record_frame, record_frame + frames)
    if track_index is None:
        if not timeline.AddTrack("video"):
            raise ResolveError("Could not add a video track for the result.")
        track_index = int(timeline.GetTrackCount("video"))
        timeline.SetTrackName("video", track_index, f"{TRACK_PREFIX} {track_index}")

    appended = media_pool.AppendToTimeline(
        [
            {
                "mediaPoolItem": media_item,
                # endFrame is exclusive here (a 120-frame clip placed with
                # endFrame 119 lands as 119 frames), unlike TimelineItem.GetEnd.
                "startFrame": 0,
                "endFrame": frames,
                "trackIndex": track_index,
                "recordFrame": record_frame,
                "mediaType": 1,  # video only
            }
        ]
    )
    if not appended:
        raise ResolveError(
            f"Resolve refused to place the result on track V{track_index} at frame {record_frame}."
        )
    return {"placement": "new_track", "track_index": track_index, "frames": frames}
