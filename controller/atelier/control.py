"""Control layer (blueprint section 8).

Makes the instrument playable without a mouse: macros, pages, actions, MIDI/OSC
learn with soft takeover, feedback maps, and an accessibility-friendly status
stream. The controller abstraction is deliberately independent of any specific
hardware; the recommended 32-pad / 8-slider / 8-button / 4-encoder layout is one
binding, not a requirement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .model import Patch
from .params import ParamMetadata


class PickupMode(str, Enum):
    JUMP = "jump"               # value follows controller immediately
    PICKUP = "pickup"           # value only moves once controller crosses it
    RELATIVE = "relative"       # encoder-style increments
    SCALE = "scale"             # soft-takeover scaling toward target


@dataclass
class MacroTarget:
    module_id: str
    param_id: str
    node: int | None = None      # None == global; -1 == all nodes
    depth: float = 1.0
    lo: float = 0.0              # normalised sub-range start
    hi: float = 1.0             # normalised sub-range end
    invert: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Macro:
    """A high-level control routed to one or many ParamSlots (or a scene morph).
    Macro has name, page, range, curve, pickup mode, LED feedback policy."""

    id: str
    name: str
    page: str = "A"
    value: float = 0.0           # normalised 0..1
    targets: list[MacroTarget] = field(default_factory=list)
    pickup: PickupMode = PickupMode.SCALE
    led_policy: str = "value"    # value / activity / off
    morph_pair: tuple[str, str] | None = None   # (sceneA, sceneB) if this is a morph macro

    def apply(self, patch: Patch) -> list[tuple[str, str, int | None, float]]:
        """Write this macro's value into every target's base. Returns the list of
        (module_id, param_id, node, new_base) that changed, for graph diffing."""
        changed: list[tuple[str, str, int | None, float]] = []
        for tg in self.targets:
            v = self.value
            if tg.invert:
                v = 1.0 - v
            pos = tg.lo + (tg.hi - tg.lo) * v
            nodes: list[int | None]
            if tg.node == -1:
                m = patch.modules.get(tg.module_id)
                nodes = list(range(m.node_count)) if m else []
            else:
                nodes = [tg.node]
            for nd in nodes:
                slot = patch.find_slot(tg.module_id, tg.param_id, nd)
                if not slot or slot.locked:
                    continue
                new_base = slot.meta.to_value(pos)
                slot.base = new_base
                changed.append((tg.module_id, tg.param_id, nd, new_base))
        return changed

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["pickup"] = self.pickup.value
        d["targets"] = [t.to_dict() for t in self.targets]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Macro":
        return cls(
            id=d["id"], name=d.get("name", d["id"]), page=d.get("page", "A"),
            value=d.get("value", 0.0),
            targets=[MacroTarget(**t) for t in d.get("targets", [])],
            pickup=PickupMode(d.get("pickup", "scale")),
            led_policy=d.get("led_policy", "value"),
            morph_pair=tuple(d["morph_pair"]) if d.get("morph_pair") else None,
        )


# Pages from section 8: Source, Spectral, Pitch/Time, Resonance, Spatial,
# Modulation, Scene, Utility.
PAGES = ["Source", "Spectral", "PitchTime", "Resonance", "Spatial",
         "Modulation", "Scene", "Utility"]

# Non-continuous actions (section 8: Action contract).
ACTIONS = ["capture", "freeze", "resample", "duplicateModule", "addNode",
           "removeNode", "randomizeConstrained", "panic", "undo", "redo",
           "tapTempo", "selectLane", "selectModule", "selectNode",
           "storeScene", "recallScene", "armDestructive"]


@dataclass
class LearnBinding:
    """A MIDI/OSC learn entry (section 8: LearnMap). Stores source identifier,
    channel, CC/note/path, scaling, pickup, soft-takeover, conflict policy."""

    id: str
    transport: str = "midi"      # midi / osc
    channel: int = 0
    selector: str = ""           # "cc:74" / "note:36" / "/atelier/macro/1"
    target_macro: str | None = None
    target_param: tuple[str, str, int | None] | None = None
    pickup: PickupMode = PickupMode.PICKUP
    scale_lo: float = 0.0
    scale_hi: float = 1.0
    # soft-takeover state (not persisted as authoritative motion)
    _last_hw: float = 0.0
    _engaged: bool = False

    def map_value(self, hw_norm: float, current_norm: float) -> tuple[float, bool]:
        """Apply pickup / soft takeover. Returns (new_norm, should_apply).

        - JUMP: always apply.
        - PICKUP: apply only once hardware crosses the current value.
        - RELATIVE: treat hw as a signed delta (already -1..1).
        - SCALE: continuously scale toward target so there is no jump.
        """
        scaled = self.scale_lo + (self.scale_hi - self.scale_lo) * hw_norm
        if self.pickup is PickupMode.JUMP:
            return scaled, True
        if self.pickup is PickupMode.RELATIVE:
            return max(0.0, min(1.0, current_norm + (hw_norm - 0.5) * 0.1)), True
        if self.pickup is PickupMode.PICKUP:
            crossed = (self._last_hw - current_norm) * (scaled - current_norm) <= 0
            self._last_hw = scaled
            if self._engaged or crossed:
                self._engaged = True
                return scaled, True
            return current_norm, False
        # SCALE / soft takeover
        if not self._engaged:
            self._engaged = abs(scaled - current_norm) < 0.05
            self._last_hw = scaled
            if not self._engaged:
                # move proportionally toward target to close the gap smoothly
                step = (scaled - self._last_hw)
                self._last_hw = scaled
                return max(0.0, min(1.0, current_norm + step * 0.5)), True
        self._last_hw = scaled
        return scaled, True

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        d["pickup"] = self.pickup.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LearnBinding":
        tp = d.get("target_param")
        return cls(
            id=d["id"], transport=d.get("transport", "midi"),
            channel=d.get("channel", 0), selector=d.get("selector", ""),
            target_macro=d.get("target_macro"),
            target_param=tuple(tp) if tp else None,
            pickup=PickupMode(d.get("pickup", "pickup")),
            scale_lo=d.get("scale_lo", 0.0), scale_hi=d.get("scale_hi", 1.0),
        )


class ControlLayer:
    """Holds macros, learn bindings, the active page, and accessibility flags."""

    def __init__(self) -> None:
        self.macros: dict[str, Macro] = {}
        self.bindings: dict[str, LearnBinding] = {}
        self.active_page = "Source"
        self.expert_armed = False         # arms destructive/expert actions
        self.high_contrast = False        # accessibility: dark/high-contrast
        self.large_readout = False
        self.color_only_state = False     # must remain False per accessibility req
        self._learn_target: tuple[str, Any] | None = None  # pending learn assignment

    # -- macros ------------------------------------------------------------ #
    def add_macro(self, macro: Macro) -> None:
        self.macros[macro.id] = macro

    def set_macro(self, patch: Patch, macro_id: str, value: float):
        m = self.macros.get(macro_id)
        if not m:
            return []
        v = max(0.0, min(1.0, value))
        # apply() is DESTRUCTIVE — it overwrites every target's slot.base with the
        # macro-curve position. So a write whose value is unchanged from the macro's
        # current position MUST be a no-op; otherwise re-asserting a macro at its
        # existing value (the ui.js re-syncing its knobs after a patch/scene load,
        # or the initial sync after generation) would snap the freshly generated /
        # recalled param values onto the macro curve and destroy them. Only an
        # actual knob MOVE should drive the targets.
        if abs(v - m.value) < 1e-3:
            return []
        m.value = v
        return m.apply(patch)

    # -- learn ------------------------------------------------------------- #
    def begin_learn(self, kind: str, target: Any) -> None:
        """Arm learn: next incoming MIDI/OSC message binds to ``target``."""
        self._learn_target = (kind, target)

    def handle_incoming(self, transport: str, selector: str, channel: int,
                        value_norm: float, patch: Patch):
        """Route an incoming controller message. If a learn is armed, create the
        binding (with conflict resolution); otherwise drive the bound target with
        soft takeover."""
        if self._learn_target is not None:
            kind, target = self._learn_target
            bid = f"{transport}:{selector}@{channel}"
            # conflict policy: a new learn replaces any existing binding on the
            # same selector (section 8: conflict policy)
            self.bindings = {k: v for k, v in self.bindings.items()
                             if v.selector != selector or v.channel != channel}
            b = LearnBinding(id=bid, transport=transport, channel=channel,
                             selector=selector)
            if kind == "macro":
                b.target_macro = target
            else:
                b.target_param = target
            self.bindings[bid] = b
            self._learn_target = None
            return [("learned", bid)]

        changes = []
        for b in self.bindings.values():
            if b.selector != selector or b.channel != channel or b.transport != transport:
                continue
            if b.target_macro and b.target_macro in self.macros:
                m = self.macros[b.target_macro]
                new_norm, apply_it = b.map_value(value_norm, m.value)
                if apply_it:
                    changes += self.set_macro(patch, b.target_macro, new_norm)
            elif b.target_param:
                mid, pid, node = b.target_param
                slot = patch.find_slot(mid, pid, node)
                if slot and not slot.locked:
                    cur_norm = slot.meta.to_norm(slot.base)
                    new_norm, apply_it = b.map_value(value_norm, cur_norm)
                    if apply_it:
                        slot.base = slot.meta.to_value(new_norm)
                        changes.append((mid, pid, node, slot.base))
        return changes

    # -- serialisation ----------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "macros": [m.to_dict() for m in self.macros.values()],
            "bindings": [b.to_dict() for b in self.bindings.values()],
            "active_page": self.active_page,
            "high_contrast": self.high_contrast,
            "large_readout": self.large_readout,
        }

    def load_dict(self, d: dict[str, Any]) -> None:
        self.macros = {m["id"]: Macro.from_dict(m) for m in d.get("macros", [])}
        self.bindings = {b["id"]: LearnBinding.from_dict(b) for b in d.get("bindings", [])}
        self.active_page = d.get("active_page", "Source")
        self.high_contrast = d.get("high_contrast", False)
        self.large_readout = d.get("large_readout", False)
