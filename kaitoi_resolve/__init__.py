"""Kaitoi bridge for DaVinci Resolve Studio.

Take the clip under the playhead, write a prompt, and get a generated video
back on the timeline — the clip's exact range is exported, sent through a
Kaitoi node graph, and laid on a track above the original.

Modules:
    api             stdlib-only Kaitoi REST client
    config          ~/.kaitoi_resolve/config.json (API key, defaults)
    history         ~/.kaitoi_resolve/history.json (recent generations)
    graphs          model presets + inline run-graph construction
    resolve_bridge  everything that talks to Resolve
    pipeline        export → upload → run → download → import, off the UI thread
    ui              the Fusion UIManager panel
"""

__version__ = "0.1.0"
