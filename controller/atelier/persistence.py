"""Deterministic patch save/load + migration (blueprint sections 9 & 1).

Patch recall must be deterministic unless the user chooses live randomness;
random sources therefore store explicit seeds (handled by the modulation engine's
``derive_seed``). The save hierarchy mirrors section 9:

    Patch -> Global -> Buffers -> Lanes -> Modules -> Nodes -> ParamVectors
          -> ModSources -> ModRoutes -> Scenes -> ControllerMaps -> Metadata
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .model import BufferRef, FeedbackEdge, Lane, ModuleInstance, Patch, Recorder, SpatialBus
from .modulation import ModRoute, ModSource, ModType, NodeScope, Transform
from .state import StateManager


def patch_to_dict(state: StateManager) -> dict[str, Any]:
    p = state.patch
    return {
        "version": Patch.SAVE_VERSION,
        "global": {
            "name": p.name,
            "sample_rate": p.sample_rate,
            "block_size": p.block_size,
            "channel_count": p.channel_count,
            "tempo": p.tempo,
            "root_seed": p.root_seed,
            "expert_override": p.expert_override,
        },
        "buffers": [b.to_dict() for b in p.buffers.values()],
        "spatial": p.spatial.to_dict(),
        "lanes": [ln.to_dict() for ln in p.lanes.values()],
        "connections": list(p.connections),
        "midi_connections": list(p.midi_connections),
        "seq": state.seq.to_dict(),
        "modules": [m.to_dict() for m in p.modules.values()],
        "feedback_edges": [fb.to_dict() for fb in p.feedback_edges.values()],
        "recorders": [r.to_dict() for r in p.recorders.values()],
        "mod_sources": [s.to_dict() for s in state.mod.sources.values()],
        "mod_routes": [r.to_dict() for r in state.mod.routes.values()],
        "scenes": state.scenes.to_list(),
        "control": state.control.to_dict(),
    }


def save_patch(state: StateManager, path: str) -> None:
    with open(path, "w") as f:
        json.dump(patch_to_dict(state), f, indent=2)


# --------------------------------------------------------------------------- #
# Migrations: each transforms a dict from version N to N+1 (section 9 example:
# v1 feedback 0..1 -> v2 0..0.98).
# --------------------------------------------------------------------------- #
def _migrate_v1_to_v2(d: dict[str, Any]) -> dict[str, Any]:
    for m in d.get("modules", []):
        if m.get("type") == "COMB":
            for nd in m.get("nodes", []):
                if "comb.feedback" in nd:
                    nd["comb.feedback"] = min(0.98, float(nd["comb.feedback"]) * 0.98)
    d["version"] = 2
    return d


MIGRATIONS: dict[int, Callable[[dict], dict]] = {1: _migrate_v1_to_v2}


def _migrate(d: dict[str, Any]) -> dict[str, Any]:
    v = int(d.get("version", 1))
    while v < Patch.SAVE_VERSION and v in MIGRATIONS:
        d = MIGRATIONS[v](d)
        v = int(d.get("version", v + 1))
    return d


def patch_from_dict(state: StateManager, d: dict[str, Any]) -> None:
    d = _migrate(d)
    g = d.get("global", {})
    p = Patch(
        channel_count=g.get("channel_count", 2),
        sample_rate=g.get("sample_rate", 48000),
        block_size=g.get("block_size", 256),
        tempo=g.get("tempo", 120.0),
        root_seed=g.get("root_seed", 1),
    )
    p.name = g.get("name", "untitled")
    p.expert_override = g.get("expert_override", False)

    p.buffers = {b["id"]: BufferRef(**b) for b in d.get("buffers", [])} or p.buffers
    if "spatial" in d:
        p.spatial = SpatialBus(**d["spatial"])
    for ln in d.get("lanes", []):
        p.add_lane(Lane.from_dict(ln))
    for md in d.get("modules", []):
        p.modules[md["id"]] = ModuleInstance.from_dict(md)
    p.connections = list(d.get("connections", []))
    p.midi_connections = list(d.get("midi_connections", []))
    p.feedback_edges = {fb["id"]: FeedbackEdge(**fb) for fb in d.get("feedback_edges", [])}
    p.recorders = {r["id"]: Recorder(**r) for r in d.get("recorders", [])} or p.recorders

    state.patch = p
    state.mod.sources.clear()
    state.mod.routes.clear()
    state.mod._instances.clear()
    state.mod.root_seed = p.root_seed
    for s in d.get("mod_sources", []):
        state.mod.add_source(_source_from_dict(s))
    for r in d.get("mod_routes", []):
        route = _route_from_dict(r)
        m = p.modules.get(route.dest_module_id)
        state.mod.add_route(route, m.node_count if m else 1)
        if m:
            state.mod.rebuild_route(route.id, m.node_count, m.node_positions())
    # Rebuild per-module LFO sources/routes from module settings.
    state.per_module_mod.sources.clear()
    state.per_module_mod.routes.clear()
    state.per_module_mod._instances.clear()
    for m in p.modules.values():
        state._rebuild_per_module_lfos(m)
    state.scenes.load_list(d.get("scenes", []))
    state.seq.load_dict(d.get("seq", {}))
    if "control" in d:
        state.control.load_dict(d["control"])
    # rebuild the DSP graph from the freshly loaded authoritative state
    state.build_graph()


def load_patch(state: StateManager, path: str) -> None:
    with open(path) as f:
        patch_from_dict(state, json.load(f))


def _source_from_dict(s: dict[str, Any]) -> ModSource:
    from .modulation import Distribution
    return ModSource(
        id=s["id"], type=ModType(s["type"]), label=s.get("label", ""),
        rate=s.get("rate", 0.5), depth=s.get("depth", 1.0), smooth=s.get("smooth", 0.2),
        phase=s.get("phase", 0.0), shape=s.get("shape", "sine"),
        distribution=Distribution(s.get("distribution", "uniform")),
        correlation=s.get("correlation", 0.0), drift=s.get("drift", 0.0),
        bipolar=s.get("bipolar", True), points=[tuple(p) for p in s.get("points", [])],
        loop=s.get("loop", True), duration=s.get("duration", 2.0),
        analysis_feature=s.get("analysis_feature", "rms"),
        analysis_source=s.get("analysis_source", "master"),
        response=s.get("response", 0.1), macro_value=s.get("macro_value", 0.0),
        division=s.get("division", 4.0), swing=s.get("swing", 0.0),
        seed_salt=s.get("seed_salt", 0), is_lfo=s.get("is_lfo", False),
    )


def _route_from_dict(r: dict[str, Any]) -> ModRoute:
    tr = r.get("transform", {})
    return ModRoute(
        id=r["id"], source_id=r["source_id"], dest_module_id=r["dest_module_id"],
        dest_param_id=r["dest_param_id"], node_scope=NodeScope(r.get("node_scope", "allDecorrelated")),
        selected_nodes=list(r.get("selected_nodes", [])), depth=r.get("depth", 0.5),
        polarity=r.get("polarity", 1), transform=Transform(**tr) if tr else Transform(),
        enable=r.get("enable", True), seed_offset=r.get("seed_offset", 0),
    )
