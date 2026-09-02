#!/usr/bin/env python3
"""Kaitoi — generate from the timeline (DaVinci Resolve Studio launcher).

Resolve lists this file under Workspace > Scripts > Edit. It locates the
``kaitoi_resolve`` package next to it (following the symlink ``install.sh``
creates), then opens the panel.

Runs from a terminal too, against the live Resolve instance:

    python3 "Kaitoi Video.py"            # open the panel
    python3 "Kaitoi Video.py" --check    # print the clip under the playhead and exit
"""

import os
import sys

LAUNCHER = "Kaitoi Video.py"
PACKAGE = "kaitoi_resolve"


def _script_dirs():
    """Directories that may hold this launcher (or a symlink to it).

    Resolve ``exec``s menu scripts without ``__file__``, so the launcher cannot
    simply look next to itself: it checks the Scripts/Edit folders Resolve
    scans, following the symlink ``install.sh`` leaves there.
    """
    dirs = []
    if "__file__" in globals():
        dirs.append(os.path.dirname(os.path.realpath(__file__)))
    if sys.argv and sys.argv[0]:
        dirs.append(os.path.dirname(os.path.realpath(sys.argv[0])))
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        dirs += [
            os.path.join(home, "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit"),
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit",
        ]
    elif sys.platform.startswith("win"):
        dirs += [
            os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Edit"),
            os.path.join(os.environ.get("PROGRAMDATA", ""), "Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit"),
        ]
    else:
        dirs += [
            os.path.join(home, ".local/share/DaVinciResolve/Fusion/Scripts/Edit"),
            "/opt/resolve/Fusion/Scripts/Edit",
        ]
    return dirs


def _find_package_dir():
    for directory in _script_dirs():
        if os.path.isdir(os.path.join(directory, PACKAGE)):
            return directory
        launcher = os.path.join(directory, LAUNCHER)
        if os.path.lexists(launcher):
            real = os.path.dirname(os.path.realpath(launcher))
            if os.path.isdir(os.path.join(real, PACKAGE)):
                return real
    raise ImportError(
        f"Cannot find the '{PACKAGE}' package. Run install.sh from the plugin folder "
        f"(looked in: {', '.join(d for d in _script_dirs() if d)})"
    )


_HERE = _find_package_dir()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Resolve reuses one Python interpreter for every script run, so a previously
# imported copy of the package would otherwise mask edits made on disk.
for _name in [m for m in list(sys.modules) if m == "kaitoi_resolve" or m.startswith("kaitoi_resolve.")]:
    del sys.modules[_name]

from kaitoi_resolve import config, resolve_bridge, ui  # noqa: E402


def main(argv):
    try:
        # Inside Resolve the host injects `resolve` into this script's globals.
        session = resolve_bridge.connect(resolve=globals().get("resolve"))
    except resolve_bridge.ResolveError as exc:
        print(f"[Kaitoi] {exc}")
        return 1

    if "--check" in argv:
        clip = resolve_bridge.current_clip(session)
        print(resolve_bridge.describe_clip(clip))
        print(f"source: {clip.get('file_path')}")
        return 0

    config.log(f"panel opened (Resolve {session.resolve.GetVersionString()})")
    ui.launch(session)
    return 0


if "resolve" in globals():
    # Launched from Resolve's Workspace > Scripts menu: no argv, no exit code.
    main([])
elif __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
