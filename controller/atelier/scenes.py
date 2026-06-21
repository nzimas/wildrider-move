"""Scene, preset and morph layer (blueprint section 7).

Scenes are named snapshots of selected patch state. They are *not* mere presets:
the user morphs between them, excludes parameters, randomizes inside constraints,
and binds morphing to a controller. The scene layer sits above the parameter
graph and below the control surface.

Morph pseudo-flow (section 7):
    controller value -> scene pair resolver -> per-parameter interpolation
        -> safety clamp -> smoothing -> control bus update -> DSP graph

This module produces the *target base values*; smoothing + the actual control
bus write happen in the state manager / OSC bridge.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .params import Curve, Rate
from .model import Patch


class CaptureScope(str, Enum):
    ALL = "all"
    LANE = "lane"
    SELECTED_MODULES = "selectedModules"
    SELECTED_PARAMS = "selectedParams"
    MACRO_VISIBLE = "macroVisible"


class DiscretePolicy(str, Enum):
    NEAREST = "nearest"
    HOLD_SOURCE = "holdSource"
    HOLD_TARGET = "holdTarget"
    RANDOM_BETWEEN = "randomBetween"
    THRESHOLD = "threshold"


# Parameters/aspects that must never morph unless explicitly included (section 7
# exclusions: file paths, buffer contents, dangerous feedback enable, channel
# count, controller mappings).
HARD_EXCLUDED_SUFFIXES = (".captureSource", ".captureMode", ".bufferID",
                          ".engineMode", ".fftSize", ".maxVoices")


@dataclass
class Scene:
    """A scene is a CLIP: a complete, faithful snapshot of the performance at the
    moment of capture — every module, the wiring, the MIDI graph, the LFO bank and
    full modulation topology, the sequencer, positions and all params. Morphing
    between scenes therefore has to reconcile structural change (re-wiring, modules
    added/removed, whole-patch regeneration), not just slide knobs."""
    id: str
    name: str = ""
    color: str = "#b6d7a8"
    description: str = ""
    tags: list[str] = field(default_factory=list)
    transition_time: float = 0.5            # seconds; morph time when recalled
    scope: CaptureScope = CaptureScope.ALL
    # full patch snapshot (the patch_to_dict shape, minus scenes/control). Empty
    # dict = an unfilled slot.
    snapshot: dict[str, Any] = field(default_factory=dict)
    # parameter ids explicitly excluded from morphing into/out of this scene
    exclusions: list[str] = field(default_factory=list)
    discrete_policy: DiscretePolicy = DiscretePolicy.THRESHOLD

    @property
    def filled(self) -> bool:
        return bool(self.snapshot.get("modules"))

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["scope"] = self.scope.value
        d["discrete_policy"] = self.discrete_policy.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Scene":
        return cls(
            id=d["id"], name=d.get("name", ""), color=d.get("color", "#b6d7a8"),
            description=d.get("description", ""), tags=list(d.get("tags", [])),
            transition_time=d.get("transition_time", 0.5),
            scope=CaptureScope(d.get("scope", "all")),
            snapshot=d.get("snapshot", {}),
            exclusions=list(d.get("exclusions", [])),
            discrete_policy=DiscretePolicy(d.get("discrete_policy", "threshold")),
        )


def _excluded(pid: str, exclusions) -> bool:
    if any(pid.endswith(s) for s in HARD_EXCLUDED_SUFFIXES):
        return True
    return pid in exclusions


def _switch_discrete(a, b, t: float, policy: DiscretePolicy):
    if policy is DiscretePolicy.HOLD_SOURCE:
        return a if t < 1.0 else b
    if policy is DiscretePolicy.HOLD_TARGET:
        return b if t > 0.0 else a
    if policy is DiscretePolicy.RANDOM_BETWEEN:
        return a if random.random() > t else b
    return a if t < 0.5 else b   # NEAREST / THRESHOLD


def _interp_group(slots, a_vals, b_vals, t, exclusions, policy) -> int:
    n = 0
    for pid, slot in slots.items():
        if slot.locked or _excluded(pid, exclusions) or pid not in b_vals:
            continue
        target = b_vals[pid]
        source = a_vals.get(pid, slot.base)
        meta = slot.meta
        if meta.curve is Curve.ENUM or meta.rate in (Rate.DISCRETE, Rate.TRIGGER):
            slot.base = _switch_discrete(source, target, t, policy)
        else:
            na = meta.to_norm(source)
            nb = meta.to_norm(target)
            slot.base = meta.to_value(na + (nb - na) * t)
        n += 1
    return n


def interp_params(patch, src: dict, dst: dict, t: float,
                  exclusions=(), policy: DiscretePolicy = DiscretePolicy.THRESHOLD) -> int:
    """Interpolate the params / wet / bypass / canvas position of every module that
    exists in the live patch toward its destination snapshot value. Structural
    differences (modules added/removed, wiring) are NOT handled here — the state
    manager commits those at the morph midpoint. Pure + synchronous (testable)."""
    t = max(0.0, min(1.0, t))
    smods = {m["id"]: m for m in src.get("modules", [])}
    dmods = {m["id"]: m for m in dst.get("modules", [])}
    updated = 0
    for mid, m in patch.modules.items():
        dd = dmods.get(mid)
        if dd is None:
            continue
        sd = smods.get(mid) or {}
        if "wet_dry" in dd:
            a = sd.get("wet_dry", m.wet_dry)
            m.wet_dry = a + (dd["wet_dry"] - a) * t
        if "bypass" in dd:
            m.bypass = dd["bypass"] if t >= 0.5 else sd.get("bypass", m.bypass)
        if "x" in dd and "x" in sd:
            m.x = sd["x"] + (dd["x"] - sd["x"]) * t
            m.y = sd.get("y", m.y) + (dd.get("y", m.y) - sd.get("y", m.y)) * t
        updated += _interp_group(m.global_slots, sd.get("global", {}), dd.get("global", {}),
                                 t, exclusions, policy)
        sn, dn = sd.get("nodes", []), dd.get("nodes", [])
        for i, nd in enumerate(m.node_slots):
            a = sn[i] if i < len(sn) else {}
            b = dn[i] if i < len(dn) else {}
            updated += _interp_group(nd, a, b, t, exclusions, policy)
    return updated


class SceneEngine:
    """Owns the performance bank (>= 16 scenes) and morph/mutation logic."""

    def __init__(self) -> None:
        self.scenes: dict[str, Scene] = {}
        self.order: list[str] = []
        self.active: str | None = None   # currently selected / running scene

    # -- capture / store --------------------------------------------------- #
    def store(self, scene_id: str, name: str, snapshot: dict[str, Any]) -> Scene:
        """Store a full patch snapshot into a scene slot (created if absent)."""
        prev = self.scenes.get(scene_id)
        sc = Scene(id=scene_id, name=name or scene_id, snapshot=snapshot,
                   exclusions=list(prev.exclusions) if prev else [],
                   discrete_policy=prev.discrete_policy if prev else DiscretePolicy.THRESHOLD)
        self.scenes[scene_id] = sc
        if scene_id not in self.order:
            self.order.append(scene_id)
        return sc

    # -- morph (param-only interpolation between two stored scenes) --------- #
    def morph(self, patch: Patch, scene_a: str, scene_b: str, t: float) -> int:
        """Interpolate the live patch's params from scene A to scene B at position
        t. Structural reconciliation (modules/wiring/modulation) is the state
        manager's job; this only slides comparable values (used in tests and as the
        per-tick glide of a timed morph)."""
        sb = self.scenes.get(scene_b)
        if not sb:
            return 0
        sa = self.scenes.get(scene_a)
        src = sa.snapshot if sa else {}
        return interp_params(patch, src, sb.snapshot, t,
                             exclusions=sb.exclusions, policy=sb.discrete_policy)

    # -- constrained mutation (randomizer, section 7 + GRM randomizer) ----- #
    def mutate(self, patch: Patch, amount: float, rng: random.Random,
               module_filter: set[str] | None = None,
               expert: bool = False) -> int:
        """Randomize under metadata constraints. ``amount`` 0 keeps results near
        current, 1.0 is fully random within each param's policy range. Locked and
        modulator topology are preserved (GRM: 'randomization preserves cursor
        topology')."""
        n = 0
        for mid, m in patch.modules.items():
            if module_filter is not None and mid not in module_filter:
                continue
            for slot in m.global_slots.values():
                if slot.locked:
                    continue
                new = slot.meta.randomize(rng, slot.base, amount, expert)
                if new != slot.base:
                    slot.base = new
                    n += 1
            for nd in m.node_slots:
                for slot in nd.values():
                    if slot.locked:
                        continue
                    new = slot.meta.randomize(rng, slot.base, amount, expert)
                    if new != slot.base:
                        slot.base = new
                        n += 1
        return n

    def add_empty(self, scene_id: str, name: str = "") -> Scene:
        """Create an empty scene slot (no captured data yet)."""
        sc = Scene(id=scene_id, name=name or scene_id)
        self.scenes[scene_id] = sc
        if scene_id not in self.order:
            self.order.append(scene_id)
        return sc

    def remove(self, scene_id: str) -> None:
        self.scenes.pop(scene_id, None)
        if scene_id in self.order:
            self.order.remove(scene_id)
        if self.active == scene_id:
            self.active = None

    def to_list(self) -> list[dict[str, Any]]:
        return [self.scenes[sid].to_dict() for sid in self.order if sid in self.scenes]

    def load_list(self, data: list[dict[str, Any]]) -> None:
        self.scenes.clear()
        self.order.clear()
        self.active = None
        for d in data:
            sc = Scene.from_dict(d)
            self.scenes[sc.id] = sc
            self.order.append(sc.id)
