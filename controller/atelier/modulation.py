"""Polyadic modulation engine (blueprint section 6).

The distinguishing idea, taken straight from GRM Atelier's modulation system: a
connection from a source to a destination is **not** a wire carrying one signal
to many places. Each connection is a *derived instance* of the source. The user
sees one source, but every destination — and every node within a destination —
receives an independent, decorrelated, reproducible variant.

Reproducibility is guaranteed by seed derivation:

    derivedSeed = hash(rootSeed, sourceID, routeID, destinationParamID, nodeID)

so that reloading a patch reproduces the exact motion. Modulation is evaluated
at control rate in this process and the summed per-destination value is written
to a SuperCollider control bus (audio-rate routing is reserved for params whose
metadata declares ``rate == audio``).

Modulators sum at a destination, and each connection has a depth in [-2, 2]
(i.e. GRM's -200%..+200%), applied in the parameter's *normalised* space so the
modulation moves the slider relative to its nominal position.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .params import ParamMetadata, ParamSlot


# --------------------------------------------------------------------------- #
class ModType(str, Enum):
    RANDOM_CONTINUOUS = "random_continuous"
    RANDOM_STEPPED = "random_stepped"
    CONTROLLED_MACRO = "controlled_macro"
    ENVELOPE = "envelope"            # programmable envelope / function
    GESTURE = "gesture"             # recorded controller stream
    ANALYSIS = "analysis"           # analysis follower (RMS/centroid/onset/pitch/noisiness)
    CLOCK = "clock"                 # clock / phasor


class NodeScope(str, Enum):
    ALL_SAME = "allSame"                 # every node gets the identical stream
    ALL_DECORRELATED = "allDecorrelated"  # every node gets its own derived stream
    SELECTED = "selected"                # only listed nodes
    ALTERNATING = "alternating"          # even/odd nodes
    WEIGHTED = "spatiallyWeighted"       # depth scaled by node spatial position
    RANDOM_SUBSET = "randomSubset"       # a seeded random subset
    ONE_PER_NODE = "onePerNode"          # exactly one route maps to one node


class Distribution(str, Enum):
    UNIFORM = "uniform"
    GAUSS = "gauss"
    EXP = "exp"
    BIMODAL = "bimodal"


def derive_seed(root_seed: int, *parts: Any) -> int:
    """Stable cross-process seed derivation (Python's hash() is salted, so we use
    a real digest). This is part of the save format — same inputs, same motion."""
    h = hashlib.sha256()
    h.update(str(root_seed).encode())
    for p in parts:
        h.update(b"\x1f")
        h.update(str(p).encode())
    return int.from_bytes(h.digest()[:8], "big")


# --------------------------------------------------------------------------- #
# Transforms applied to a source output before it is summed at a destination.
# --------------------------------------------------------------------------- #
@dataclass
class Transform:
    scale: float = 1.0
    offset: float = 0.0
    rectify: bool = False
    slew: float = 0.0           # 0..1, low-pass on the stream
    quantize_steps: int = 0     # 0 = off
    curve: float = 1.0          # >1 expands, <1 compresses around 0
    wrap: bool = False
    fold: bool = False
    threshold: float = 0.0      # gate below |threshold|
    probability: float = 1.0    # probability gate (chance the value passes per step)

    def apply(self, x: float, rng: random.Random, prev: float) -> float:
        x = x * self.scale + self.offset
        if self.rectify:
            x = abs(x)
        if self.threshold > 0.0 and abs(x) < self.threshold:
            x = 0.0
        if self.curve != 1.0:
            s = -1.0 if x < 0 else 1.0
            x = s * (abs(x) ** self.curve)
        if self.quantize_steps > 0:
            x = round(x * self.quantize_steps) / self.quantize_steps
        if self.wrap:
            x = ((x + 1.0) % 2.0) - 1.0
        elif self.fold:
            while x > 1.0 or x < -1.0:
                if x > 1.0:
                    x = 2.0 - x
                if x < -1.0:
                    x = -2.0 - x
        if self.probability < 1.0 and rng.random() > self.probability:
            x = prev
        if self.slew > 0.0:
            a = self.slew
            x = prev * a + x * (1.0 - a)
        return x

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# --------------------------------------------------------------------------- #
@dataclass
class ModSource:
    """A named generator of control motion (section 6). Shared base settings;
    every route spawns independent instances from it."""

    id: str
    type: ModType
    label: str = ""
    rate: float = 0.5            # Hz for continuous/clock, steps/s for stepped
    depth: float = 1.0           # nominal output magnitude (further scaled per route)
    smooth: float = 0.2          # 0..1 smoothing of the generated stream
    phase: float = 0.0           # starting phase
    shape: str = "sine"          # for clock/lfo style
    distribution: Distribution = Distribution.UNIFORM
    correlation: float = 0.0     # 0 = fully decorrelated nodes, 1 = locked
    drift: float = 0.0
    bipolar: bool = True
    # envelope/gesture data
    points: list[tuple[float, float, float]] = field(default_factory=list)  # (time, value, curve)
    loop: bool = True
    duration: float = 2.0
    # analysis follower
    analysis_feature: str = "rms"   # rms/centroid/onset/pitch/noisiness
    analysis_source: str = "master"
    response: float = 0.1
    # macro
    macro_value: float = 0.0
    # clock
    division: float = 4.0
    swing: float = 0.0
    seed_salt: int = 0
    is_lfo: bool = False         # part of the dedicated LFO bank (vs. free polyadic source)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["type"] = self.type.value
        d["distribution"] = self.distribution.value
        return d


@dataclass
class ModRoute:
    """A connection source -> parameter slot. Stores everything needed to spawn
    one or more ``ModInstance`` objects (one per node in scope)."""

    id: str
    source_id: str
    dest_module_id: str
    dest_param_id: str            # metadata id, e.g. "comb.feedback"
    node_scope: NodeScope = NodeScope.ALL_DECORRELATED
    selected_nodes: list[int] = field(default_factory=list)
    depth: float = 0.5            # -2..2 (GRM -200%..200%), in normalised param space
    polarity: int = 1            # +1 / -1
    transform: Transform = field(default_factory=Transform)
    enable: bool = True
    seed_offset: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["node_scope"] = self.node_scope.value
        d["transform"] = self.transform.to_dict()
        return d


# --------------------------------------------------------------------------- #
class ModInstance:
    """The runtime, per-node derived modulation stream. Holds its own RNG and
    phase seeded from ``derive_seed`` so motion is fully reproducible."""

    __slots__ = ("route", "source", "node_id", "seed", "rng", "phase",
                 "value", "_prev_raw", "_step_clock", "_held", "depth_scale")

    def __init__(self, route: ModRoute, source: ModSource, node_id: int,
                 root_seed: int, depth_scale: float = 1.0):
        self.route = route
        self.source = source
        self.node_id = node_id
        self.seed = derive_seed(root_seed, source.id, route.id,
                                route.dest_param_id, node_id,
                                route.seed_offset, source.seed_salt)
        self.rng = random.Random(self.seed)
        self.phase = source.phase + (self.rng.random() * (1.0 - source.correlation))
        self.value = 0.0
        self._prev_raw = 0.0
        self._step_clock = 0.0
        self._held = self._draw()
        self.depth_scale = depth_scale

    def reset_phase(self) -> None:
        """Transport reset: phases re-derive deterministically (test matrix:
        'modulation phases if transport reset')."""
        self.rng = random.Random(self.seed)
        self.phase = self.source.phase + (self.rng.random() * (1.0 - self.source.correlation))
        self._step_clock = 0.0
        self._prev_raw = 0.0
        self.value = 0.0
        self._held = self._draw()

    def _draw(self) -> float:
        d = self.source.distribution
        if d is Distribution.GAUSS:
            return max(-1.0, min(1.0, self.rng.gauss(0.0, 0.4)))
        if d is Distribution.EXP:
            return min(1.0, self.rng.expovariate(2.0)) * (1 if self.source.bipolar and self.rng.random() < 0.5 else 1)
        if d is Distribution.BIMODAL:
            return self.rng.choice([-1.0, 1.0]) * self.rng.uniform(0.5, 1.0)
        return self.rng.uniform(-1.0, 1.0) if self.source.bipolar else self.rng.random()

    def _raw(self, dt: float, analysis: dict[str, float] | None) -> float:
        s = self.source
        t = s.type
        if t is ModType.RANDOM_CONTINUOUS:
            # smoothed random walk toward fresh targets at `rate`
            self._step_clock += dt * s.rate
            if self._step_clock >= 1.0:
                self._step_clock -= 1.0
                self._held = self._draw()
            a = max(0.0, min(0.999, s.smooth))
            self._prev_raw = self._prev_raw * a + self._held * (1.0 - a)
            return self._prev_raw + (self.rng.random() - 0.5) * 2.0 * s.drift
        if t is ModType.RANDOM_STEPPED:
            self._step_clock += dt * s.rate
            if self._step_clock >= 1.0:
                self._step_clock -= 1.0
                self._held = self._draw()
            a = max(0.0, min(0.999, s.smooth))  # acts as slew
            self._prev_raw = self._prev_raw * a + self._held * (1.0 - a)
            return self._prev_raw
        if t is ModType.CONTROLLED_MACRO:
            target = (s.macro_value * 2.0 - 1.0) if s.bipolar else s.macro_value
            a = max(0.0, min(0.999, s.smooth))   # inertia
            self._prev_raw = self._prev_raw * a + target * (1.0 - a)
            return self._prev_raw
        if t is ModType.CLOCK:
            self.phase = (self.phase + dt * s.rate) % 1.0
            ph = self.phase
            if s.swing and (int(self.phase * s.division) % 2 == 1):
                ph = min(1.0, ph + s.swing * 0.1)
            if s.shape == "saw":
                return 2.0 * ph - 1.0
            if s.shape == "square":
                return 1.0 if ph < 0.5 else -1.0
            if s.shape == "tri":
                return 4.0 * abs(ph - 0.5) - 1.0
            return math.sin(2.0 * math.pi * ph)
        if t is ModType.ENVELOPE:
            return self._eval_envelope(dt)
        if t is ModType.GESTURE:
            return self._eval_envelope(dt)  # gesture is a recorded breakpoint stream
        if t is ModType.ANALYSIS:
            val = 0.0
            if analysis:
                val = analysis.get(f"{s.analysis_source}.{s.analysis_feature}",
                                   analysis.get(s.analysis_feature, 0.0))
            a = max(0.0, min(0.999, 1.0 - s.response))
            self._prev_raw = self._prev_raw * a + val * (1.0 - a)
            return self._prev_raw * 2.0 - 1.0 if s.bipolar else self._prev_raw
        return 0.0

    def _eval_envelope(self, dt: float) -> float:
        s = self.source
        if not s.points:
            return 0.0
        self.phase += dt / max(1e-4, s.duration)
        if self.phase >= 1.0:
            self.phase = (self.phase % 1.0) if s.loop else 1.0
        pts = s.points
        pos = self.phase
        for i in range(len(pts) - 1):
            t0, v0, c0 = pts[i]
            t1, v1, _ = pts[i + 1]
            if t0 <= pos <= t1 and t1 > t0:
                frac = (pos - t0) / (t1 - t0)
                if c0 != 0:  # exponential-ish segment curvature
                    frac = (math.exp(c0 * frac) - 1) / (math.exp(c0) - 1)
                return v0 + (v1 - v0) * frac
        return pts[-1][1]

    def tick(self, dt: float, analysis: dict[str, float] | None) -> float:
        raw = self._raw(dt, analysis)
        x = self.route.transform.apply(raw, self.rng, self.value)
        x *= self.source.depth * self.route.depth * self.route.polarity * self.depth_scale
        # A modulation stream must never emit a non-finite value into the model.
        if not math.isfinite(x):
            x = 0.0
            self._prev_raw = 0.0
        self.value = x
        return x


# --------------------------------------------------------------------------- #
class ModEngine:
    """Owns sources, routes, and the spawned per-node instances. The state
    manager calls :meth:`tick` on a control-rate timer; the engine returns, for
    each affected (module_id, param_id, node_id), the summed normalised offset to
    apply on top of the slot's base position."""

    def __init__(self, root_seed: int = 0):
        self.root_seed = root_seed
        self.sources: dict[str, ModSource] = {}
        self.routes: dict[str, ModRoute] = {}
        self._instances: dict[str, list[ModInstance]] = {}   # route_id -> instances

    # -- editing ----------------------------------------------------------- #
    def add_source(self, src: ModSource) -> None:
        self.sources[src.id] = src

    def remove_source(self, sid: str) -> None:
        self.sources.pop(sid, None)
        for rid in [r.id for r in self.routes.values() if r.source_id == sid]:
            self.remove_route(rid)

    def add_route(self, route: ModRoute, node_count: int) -> None:
        self.routes[route.id] = route
        self.rebuild_route(route.id, node_count)

    def remove_route(self, rid: str) -> None:
        self.routes.pop(rid, None)
        self._instances.pop(rid, None)

    def rebuild_route(self, rid: str, node_count: int,
                      node_positions: list[float] | None = None) -> None:
        """(Re)spawn the per-node instances for a route according to its node
        scope. Called on add and whenever the destination module's node count
        changes (hot edit)."""
        route = self.routes[rid]
        src = self.sources.get(route.source_id)
        if src is None:
            self._instances[rid] = []
            return
        scope = route.node_scope
        node_ids = self._nodes_in_scope(route, node_count)
        insts: list[ModInstance] = []
        for nid in node_ids:
            depth_scale = 1.0
            if scope is NodeScope.WEIGHTED and node_positions and nid < len(node_positions):
                depth_scale = 0.5 + 0.5 * node_positions[nid]
            # ALL_SAME shares a single seed across nodes (node_id pinned to 0)
            seed_node = 0 if scope is NodeScope.ALL_SAME else nid
            inst = ModInstance(route, src, seed_node, self.root_seed, depth_scale)
            # but it still drives the real node id
            inst.node_id = nid
            insts.append(inst)
        self._instances[rid] = insts

    def _nodes_in_scope(self, route: ModRoute, node_count: int) -> list[int]:
        scope = route.node_scope
        alln = list(range(node_count))
        if scope is NodeScope.SELECTED:
            return [n for n in route.selected_nodes if n < node_count]
        if scope is NodeScope.ALTERNATING:
            return alln[::2]
        if scope is NodeScope.ONE_PER_NODE:
            return alln[:1] if node_count else []
        if scope is NodeScope.RANDOM_SUBSET:
            rng = random.Random(derive_seed(self.root_seed, route.id, "subset"))
            k = max(1, node_count // 2)
            return sorted(rng.sample(alln, min(k, node_count))) if node_count else []
        return alln  # ALL_SAME, ALL_DECORRELATED, WEIGHTED

    def reset_phases(self) -> None:
        for insts in self._instances.values():
            for inst in insts:
                inst.reset_phase()

    def set_root_seed(self, seed: int, node_counts: dict[str, int]) -> None:
        self.root_seed = seed
        for rid, route in self.routes.items():
            self.rebuild_route(rid, node_counts.get(route.dest_module_id, 1))

    # -- evaluation -------------------------------------------------------- #
    def tick(self, dt: float, analysis: dict[str, float] | None = None
             ) -> dict[tuple[str, str, int], float]:
        """Return summed normalised offsets keyed by (module_id, param_id, node)."""
        out: dict[tuple[str, str, int], float] = {}
        for rid, insts in self._instances.items():
            route = self.routes[rid]
            if not route.enable:
                continue
            for inst in insts:
                off = inst.tick(dt, analysis)
                key = (route.dest_module_id, route.dest_param_id, inst.node_id)
                out[key] = out.get(key, 0.0) + off
        return out
