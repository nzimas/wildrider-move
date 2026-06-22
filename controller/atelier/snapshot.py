"""UI snapshot serialisation.

Produces the full machine-describable picture the web UI needs to render itself
with no hard-coded knowledge of any module: the catalog metadata plus the live
patch / modulation / scene / control state.
"""
from __future__ import annotations

from typing import Any

from .catalog import CATALOG
from .modulation import ModType
from .state import StateManager


def lfos_dict(state: StateManager) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sid in state.lfo_ids():
        src = state.mod.sources[sid]
        rt = state.mod.routes.get(f"{sid}_rt")
        if src.type is ModType.RANDOM_STEPPED:
            shape = "sh"
        elif src.shape == "tri":
            shape = "triangle"
        else:
            shape = "sine"
        out.append({
            "id": sid, "label": src.label, "shape": shape, "rate": src.rate,
            "depth": rt.depth if rt else 0.5,
            "target": f"{rt.dest_module_id}|{rt.dest_param_id}" if rt else "",
        })
    return out


def mod_targets(state: StateManager) -> list[dict[str, str]]:
    """Every modulatable (module, param) in the patch — for LFO target menus."""
    out: list[dict[str, str]] = []
    for mid, mod in state.patch.modules.items():
        for p in mod.spec.node_params + mod.spec.global_params:
            if p.modulatable:
                out.append({"value": f"{mid}|{p.id}", "label": f"{mid}.{p.label}"})
    return out


def catalog_dict() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for t, m in CATALOG.items():
        out[t] = {
            "type": t,
            "role": m.role,
            "node_meaning": m.node_meaning,
            "insert_capable": m.insert_capable,
            "generative_capable": m.generative_capable,
            "max_nodes": m.max_nodes,
            "is_audio": m.is_audio,
            "cpu_per_node": m.cpu_per_node,
            "gestures": m.gestures,
            "node_params": [p.to_dict() for p in m.node_params],
            "global_params": [p.to_dict() for p in m.global_params],
        }
    return out


def module_state(state: StateManager, mid: str) -> dict[str, Any]:
    m = state.patch.modules[mid]
    return {
        "id": m.id, "type": m.type, "lane_id": m.lane_id,
        "bypass": m.bypass, "wet_dry": m.wet_dry, "color": m.color,
        "x": m.x, "y": m.y,
        "has_input": m.spec.insert_capable, "has_output": m.spec.is_audio,
        "node_count": m.node_count,
        "per_module_lfos_enabled": m.per_module_lfos_enabled,
        "per_module_lfos": {k: dict(v) for k, v in m.per_module_lfos.items()},
        "global": {
            pid: {"base": s.base, "effective": s.effective, "display": s.display(),
                  "norm": s.meta.to_norm(s.base), "locked": s.locked}
            for pid, s in m.global_slots.items()
        },
        "nodes": [
            {pid: {"base": s.base, "effective": s.effective, "display": s.display(),
                   "norm": s.meta.to_norm(s.base), "locked": s.locked}
             for pid, s in nd.items()}
            for nd in m.node_slots
        ],
    }


def full_snapshot(state: StateManager) -> dict[str, Any]:
    p = state.patch
    return {
        "catalog": catalog_dict(),
        "global": {
            "name": p.name, "sample_rate": p.sample_rate, "block_size": p.block_size,
            "channel_count": p.channel_count, "tempo": p.tempo,
            "root_seed": p.root_seed, "expert_override": p.expert_override,
        },
        "spatial": p.spatial.to_dict(),
        "lanes": [ln.to_dict() for ln in p.lanes.values()],
        "connections": list(p.connections),
        "midi_connections": list(p.midi_connections),
        "seq": state.seq.to_dict(),
        "modules": [module_state(state, mid) for mid in p.modules],
        "feedback_edges": [fb.to_dict() for fb in p.feedback_edges.values()],
        "recorders": [r.to_dict() for r in p.recorders.values()],
        "mod_sources": [s.to_dict() for s in state.mod.sources.values()],
        "mod_routes": [r.to_dict() for r in state.mod.routes.values()],
        "lfos": lfos_dict(state),
        "mod_targets": mod_targets(state),
        "scenes": state.scenes.to_list(),
        "active_scene": state.scenes.active,
        "control": state.control.to_dict(),
        "lfos_enabled": getattr(state, "lfos_enabled", True),
        "buffers": [b.to_dict() for b in p.buffers.values()],
        "engine": {
            "connected": state.bridge.connected,
            "cpu": state.bridge.cpu,
            "cpu_warn": state.cpu_warn,
            "meters": state.bridge.meters,
        },
    }
