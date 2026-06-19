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
    id: str
    name: str = ""
    color: str = "#b6d7a8"
    description: str = ""
    tags: list[str] = field(default_factory=list)
    transition_time: float = 0.5            # seconds; morph time when recalled
    scope: CaptureScope = CaptureScope.ALL
    # captured values: module_id -> {"bypass":..,"wet_dry":..,"node_count":..,
    #                                "global":{pid:val}, "nodes":[{pid:val},...]}
    modules: dict[str, dict[str, Any]] = field(default_factory=dict)
    # parameter ids explicitly excluded from morphing into/out of this scene
    exclusions: list[str] = field(default_factory=list)
    discrete_policy: DiscretePolicy = DiscretePolicy.THRESHOLD

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
            modules=d.get("modules", {}),
            exclusions=list(d.get("exclusions", [])),
            discrete_policy=DiscretePolicy(d.get("discrete_policy", "threshold")),
        )


class SceneEngine:
    """Owns the performance bank (>= 16 scenes) and morph/mutation logic."""

    def __init__(self) -> None:
        self.scenes: dict[str, Scene] = {}
        self.order: list[str] = []

    # -- capture / recall -------------------------------------------------- #
    def capture(self, patch: Patch, scene_id: str, name: str = "",
                scope: CaptureScope = CaptureScope.ALL,
                module_filter: set[str] | None = None) -> Scene:
        sc = Scene(id=scene_id, name=name or scene_id, scope=scope)
        for mid, m in patch.modules.items():
            if module_filter is not None and mid not in module_filter:
                continue
            entry: dict[str, Any] = {
                "bypass": m.bypass, "wet_dry": m.wet_dry,
                "node_count": m.node_count, "global": {}, "nodes": [],
            }
            for pid, slot in m.global_slots.items():
                if slot.locked:           # snapshot locks excluded from storage
                    continue
                if self._capturable(pid, scope, scene_id):
                    entry["global"][pid] = slot.base
            for nd in m.node_slots:
                nrec: dict[str, float] = {}
                for pid, slot in nd.items():
                    if slot.locked:
                        continue
                    if self._capturable(pid, scope, scene_id):
                        nrec[pid] = slot.base
                entry["nodes"].append(nrec)
            sc.modules[mid] = entry
        self.scenes[scene_id] = sc
        if scene_id not in self.order:
            self.order.append(scene_id)
        return sc

    def _capturable(self, pid: str, scope: CaptureScope, scene_id: str) -> bool:
        return True  # scope filtering for params handled at capture call site

    def recall(self, patch: Patch, scene_id: str) -> None:
        """Instant recall (transition_time == 0 path). Locked slots are skipped."""
        sc = self.scenes.get(scene_id)
        if not sc:
            return
        self._apply_values(patch, sc, weight=1.0, base_scene=None, t=1.0)

    # -- morph ------------------------------------------------------------- #
    def morph(self, patch: Patch, scene_a: str, scene_b: str, t: float) -> int:
        """Interpolate from scene A to scene B at position t in [0,1].

        Numeric params interpolate (respecting per-param curve in normalised
        space); discrete/enum/trigger params switch by the scene's discrete
        policy. Excluded and locked params are held. Returns the count of slots
        updated."""
        sa = self.scenes.get(scene_a)
        sb = self.scenes.get(scene_b)
        if not sb:
            return 0
        t = max(0.0, min(1.0, t))
        updated = 0
        for mid, m in patch.modules.items():
            ea = sa.modules.get(mid) if sa else None
            eb = sb.modules.get(mid)
            if not eb:
                continue
            # node_count / bypass are structural — switch at threshold
            if "node_count" in eb and t >= 0.5:
                if eb["node_count"] != m.node_count:
                    m.set_node_count(eb["node_count"])
            if "bypass" in eb:
                m.bypass = eb["bypass"] if t >= 0.5 else (ea["bypass"] if ea else m.bypass)
            if "wet_dry" in eb:
                a = ea["wet_dry"] if ea and "wet_dry" in ea else m.wet_dry
                m.wet_dry = a + (eb["wet_dry"] - a) * t
            updated += self._morph_group(m.global_slots, ea, eb, "global", t, sb)
            for i, nd in enumerate(m.node_slots):
                na = ea["nodes"][i] if ea and i < len(ea.get("nodes", [])) else None
                nb = eb["nodes"][i] if i < len(eb.get("nodes", [])) else None
                if nb is None:
                    continue
                updated += self._morph_node(nd, na, nb, t, sb)
        return updated

    def _morph_group(self, slots, ea, eb, key, t, sb) -> int:
        a_vals = ea.get(key, {}) if ea else {}
        b_vals = eb.get(key, {})
        return self._morph_slots(slots, a_vals, b_vals, t, sb)

    def _morph_node(self, slots, na, nb, t, sb) -> int:
        return self._morph_slots(slots, na or {}, nb or {}, t, sb)

    def _morph_slots(self, slots, a_vals, b_vals, t, sb) -> int:
        n = 0
        for pid, slot in slots.items():
            if slot.locked or self._excluded(pid, sb):
                continue
            if pid not in b_vals:
                continue
            target = b_vals[pid]
            source = a_vals.get(pid, slot.base)
            meta = slot.meta
            if meta.curve is Curve.ENUM or meta.rate in (Rate.DISCRETE, Rate.TRIGGER):
                slot.base = self._switch_discrete(source, target, t, sb.discrete_policy)
            else:
                # interpolate in normalised space for perceptual evenness
                na_ = meta.to_norm(source)
                nb_ = meta.to_norm(target)
                slot.base = meta.to_value(na_ + (nb_ - na_) * t)
            n += 1
        return n

    @staticmethod
    def _switch_discrete(a, b, t, policy: DiscretePolicy):
        if policy is DiscretePolicy.HOLD_SOURCE:
            return a if t < 1.0 else b
        if policy is DiscretePolicy.HOLD_TARGET:
            return b if t > 0.0 else a
        if policy is DiscretePolicy.NEAREST:
            return a if t < 0.5 else b
        if policy is DiscretePolicy.RANDOM_BETWEEN:
            return a if random.random() > t else b
        return a if t < 0.5 else b  # THRESHOLD

    @staticmethod
    def _excluded(pid: str, scene: Scene) -> bool:
        if any(pid.endswith(s) for s in HARD_EXCLUDED_SUFFIXES):
            return True
        return pid in scene.exclusions

    def _apply_values(self, patch: Patch, sc: Scene, weight, base_scene, t) -> None:
        for mid, entry in sc.modules.items():
            m = patch.modules.get(mid)
            if not m:
                continue
            if "node_count" in entry:
                m.set_node_count(entry["node_count"])
            m.bypass = entry.get("bypass", m.bypass)
            m.wet_dry = entry.get("wet_dry", m.wet_dry)
            for pid, val in entry.get("global", {}).items():
                if pid in m.global_slots and not m.global_slots[pid].locked \
                        and not self._excluded(pid, sc):
                    m.global_slots[pid].base = val
            for i, nrec in enumerate(entry.get("nodes", [])):
                if i >= len(m.node_slots):
                    break
                for pid, val in nrec.items():
                    slot = m.node_slots[i].get(pid)
                    if slot and not slot.locked and not self._excluded(pid, sc):
                        slot.base = val

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

    def to_list(self) -> list[dict[str, Any]]:
        return [self.scenes[sid].to_dict() for sid in self.order if sid in self.scenes]

    def load_list(self, data: list[dict[str, Any]]) -> None:
        self.scenes.clear()
        self.order.clear()
        for d in data:
            sc = Scene.from_dict(d)
            self.scenes[sc.id] = sc
            self.order.append(sc.id)
