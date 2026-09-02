"""Model presets and inline run-graph construction.

Two video pipelines, both driven by one timeline clip plus a prompt:

- **V2V** — the clip's rendered video → ``load_video`` → model → new video.
- **I2V** — the clip's first frame → ``load_image`` → model → new video.

Pin names differ per model (``inputVideo`` / ``startImage`` / ``firstFrame``…),
so nothing is hardcoded: the builder reads each node's published schema and
wires the first pin of the right type, filling every remaining declared pin
with its catalog default. Adding a model here is therefore a one-line change.
"""

from __future__ import annotations

from typing import Any

from . import api

LOAD_VIDEO_NODE = "builtin/loaders/load_video"
LOAD_IMAGE_NODE = "builtin/loaders/load_image"
TEXT_PROMPT_NODE = "builtin/utils/text_prompt"

# Video → Video: the clip itself is restyled / edited by the prompt.
V2V_MODELS: list[tuple[str, str]] = [
    ("RAY35", "builtin/third_party/fal/ray35_v2v"),
    ("LUCY_EDIT_FAST", "builtin/third_party/fal/lucy_edit_fast"),
    ("KLING_O3_EDIT", "builtin/third_party/fal/kling_o3_standard_v2v_edit"),
    ("LTX2_V2V", "builtin/third_party/fal/ltx_2_19b_video_to_video"),
    ("RENDER_TO_REAL", "builtin/third_party/fal/ltx_render_to_real"),
    ("RAY3_MODIFY", "builtin/third_party/lumalabs/ray3_modify_video"),
    # Free, local, no provider key: reverses the clip. Proves the whole
    # export → upload → run → import path without spending credits.
    ("REVERSE_SELFTEST", "builtin/video/video_reverse"),
]

# Image → Video: the clip's first frame seeds a freshly generated shot.
I2V_MODELS: list[tuple[str, str]] = [
    ("SEEDANCE2", "builtin/third_party/fal/seedance_2_i2v"),
    ("HAILUO_23", "builtin/third_party/fal/hailuo_23_fast_pro"),
    ("KLING_O3_I2V", "builtin/third_party/fal/kling_o3_standard_i2v"),
    ("LTX23_PRO", "builtin/third_party/fal/ltx_video_23_pro"),
    ("VEO31_FAST", "builtin/third_party/fal/google_veo31_fast_i2v"),
    ("RAY3", "builtin/third_party/lumalabs/ray3_video"),
]

MODELS_BY_MODE = {"v2v": V2V_MODELS, "i2v": I2V_MODELS}

LOADER_NODE_ID = "load"
PROMPT_NODE_ID = "prompt"
TARGET_NODE_ID = "gen"


class GraphBuildError(RuntimeError):
    pass


def model_node_type(mode: str, key: str) -> str:
    """Resolve a preset key ("RAY35") to its node type, or pass a raw type through."""
    for name, node_type in MODELS_BY_MODE.get(mode, []):
        if name == key:
            return node_type
    if "/" in key:  # user typed a node type directly
        return key
    raise GraphBuildError(f"Unknown {mode} model preset: {key}")


def build_graph(
    config: api.Config,
    *,
    mode: str,
    node_type: str,
    prompt_text: str,
) -> dict[str, Any]:
    """Build the inline run graph plus everything the caller needs to run it.

    Returns ``{graph, target, loader_pin, media_kind, node_title}``.
    ``loader_pin`` is the loader input the uploaded file is bound to through
    ``inputOverrides``; ``media_kind`` is "video" or "image".

    Network calls happen here (three node-schema lookups), so call it off the
    UI thread.
    """
    if mode not in ("v2v", "i2v"):
        raise GraphBuildError(f"Unsupported mode: {mode}")
    media_kind = "video" if mode == "v2v" else "image"
    loader_type = LOAD_VIDEO_NODE if mode == "v2v" else LOAD_IMAGE_NODE

    loader_schema = api.get_node_type(config, loader_type)
    prompt_schema = api.get_node_type(config, TEXT_PROMPT_NODE)
    model_schema = api.get_node_type(config, node_type)

    loader_in = api.find_input_pin(loader_schema, media_kind)
    loader_out = api.find_output_pin(loader_schema, media_kind)
    prompt_out = api.find_output_pin(prompt_schema, "string", default="outputPrompt") or "outputPrompt"
    model_media_pin = api.find_input_pin(model_schema, media_kind)
    model_prompt_pin = api.find_prompt_pin(model_schema)

    if not loader_in or not loader_out:
        raise GraphBuildError(f"{loader_type} exposes no {media_kind} pins")
    if not model_media_pin:
        raise GraphBuildError(
            f"{node_type} has no {media_kind} input — pick a {mode.upper()} model that accepts one"
        )
    # Nodes without a prompt pin (plain processors such as the reverse
    # self-test) are still runnable; the prompt is simply not wired.
    wired = {model_media_pin, model_prompt_pin} if model_prompt_pin else {model_media_pin}
    model_inputs = api.autofill_node_inputs(model_schema, skip_pins=wired)

    nodes = [
        {"id": LOADER_NODE_ID, "type": loader_type, "inputs": {}},
        {"id": TARGET_NODE_ID, "type": node_type, "inputs": model_inputs},
    ]
    connections = [{"from": [LOADER_NODE_ID, loader_out], "to": [TARGET_NODE_ID, model_media_pin]}]
    if model_prompt_pin:
        nodes.insert(
            1,
            {
                "id": PROMPT_NODE_ID,
                "type": TEXT_PROMPT_NODE,
                "inputs": {"inputPrompt": {"type": "string", "value": prompt_text}},
            },
        )
        connections.append({"from": [PROMPT_NODE_ID, prompt_out], "to": [TARGET_NODE_ID, model_prompt_pin]})
    graph = {"nodes": nodes, "connections": connections}
    return {
        "graph": graph,
        "target": TARGET_NODE_ID,
        "loader_pin": loader_in,
        "media_kind": media_kind,
        "node_title": model_schema.get("title") or node_type,
        "output_kind": "video" if api.find_output_pin(model_schema, "video") else "image",
    }


def build_overrides(loader_pin: str, file_id: str) -> dict[str, dict[str, Any]]:
    """Bind the uploaded clip to the loader node for this run."""
    return {LOADER_NODE_ID: {loader_pin: {"type": "file", "fileId": file_id}}}


def describe(built: dict[str, Any]) -> str:
    graph = built["graph"]
    wires = ", ".join(f"{c['from'][0]}.{c['from'][1]}→{c['to'][0]}.{c['to'][1]}" for c in graph["connections"])
    return f"{built['node_title']} [{wires}]"
