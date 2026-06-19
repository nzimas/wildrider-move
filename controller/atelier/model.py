"""Patch data model (blueprint sections 2, 3 and the save hierarchy in 9).

This is the authoritative state. On load the DSP graph is *built from* this; on
edit this is updated first, then the graph is diffed. Nothing here knows about
SuperCollider or any GUI.

Save hierarchy (section 9):
    Patch -> Global -> Buffers -> Lanes -> Modules -> Nodes -> ParamVectors
          -> ModSources -> ModRoutes -> Scenes -> ControllerMaps -> Metadata
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .catalog import ModuleSpec, spec
from .params import ParamSlot


class LaneMode(str, Enum):
    SERIAL = "serial"
    PARALLEL = "parallel"
    FX = "fx"
    SEND = "send"


@dataclass
class BufferRef:
    """Sample reference by URI + checksum (section 2: 'sample references by URI
    + checksum'). Rolling/named/file buffers all live here."""

    id: str
    uri: str = ""
    checksum: str = ""
    frames: int = 0
    channels: int = 1
    sample_rate: int = 48000
    kind: str = "file"   # file / named / rolling / capture

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SpatialBus:
    """N-channel spatial field (section 3)."""

    n: int = 2
    layout_name: str = "stereo"   # mono/stereo/quad/5.1-as-N/octo/free-N
    speaker_angles: list[float] = field(default_factory=lambda: [-30.0, 30.0])
    elevation: list[float] = field(default_factory=list)
    normalisation_mode: str = "equalPower"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class FeedbackEdge:
    """Feedback is stored separately from ordinary lane order so it can be
    audited and safety-limited (section 3). Always contains a delay."""

    id: str
    source_module: str
    dest_module: str
    delay_samples_min: int = 64
    gain_limit: float = 0.85
    safety_limiter: bool = True
    armed: bool = False   # disabled by default; must be explicitly armed

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Recorder:
    id: str
    target: str = "master"           # master / lane:<id> / module:<id> / bus:<n>
    tap: str = "post"               # pre / post fader
    rolling_buffer_seconds: float = 30.0
    armed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ModuleInstance:
    """A module placed in a lane. Holds global parameter slots plus a vector of
    per-node parameter slots (section: 'Every module must support multiple
    nodes')."""

    def __init__(self, mid: str, module_type: str, lane_id: str,
                 node_count: int | None = None):
        self.id = mid
        self.type = module_type
        self.lane_id = lane_id
        self.spec: ModuleSpec = spec(module_type)
        self.bypass = False
        self.wet_dry = 1.0
        self.color = "#6fa8dc"
        self.x = 0.0          # canvas position (free-form modular patching)
        self.y = 0.0
        n = node_count if node_count is not None else 1
        self.node_count = max(1, min(n, self.spec.max_nodes))
        self.global_slots: dict[str, ParamSlot] = {
            p.id: ParamSlot(meta=p) for p in self.spec.global_params
        }
        self.node_slots: list[dict[str, ParamSlot]] = []
        for _ in range(self.node_count):
            self.node_slots.append(self._fresh_node())
        # Per-module LFO bank: one LFO per modulatable parameter.
        # Disabled by default; each entry holds the UI/DSP settings for that param.
        self.per_module_lfos_enabled: bool = False
        self.per_module_lfos: dict[str, dict[str, Any]] = {
            p.id: {"enabled": False, "shape": "sine", "rate": 0.5, "depth": 0.3}
            for p in self.spec.node_params + self.spec.global_params
            if p.modulatable
        }

    def _fresh_node(self) -> dict[str, ParamSlot]:
        return {p.id: ParamSlot(meta=p) for p in self.spec.node_params}

    # -- hot edits --------------------------------------------------------- #
    def set_node_count(self, n: int) -> None:
        n = max(1, min(n, self.spec.max_nodes))
        while len(self.node_slots) < n:
            self.node_slots.append(self._fresh_node())
        del self.node_slots[n:]
        self.node_count = n

    def node_positions(self) -> list[float]:
        """Spatial positions per node, for spatially-weighted modulation."""
        out = []
        for nd in self.node_slots:
            pan = nd.get(f"{self.type.lower()}.pan") or nd.get(f"{self.type.lower()}.spatialPos")
            out.append(pan.effective if pan else 0.0)
        return out

    def short_pid(self, pid: str) -> str:
        return pid.split(".", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "lane_id": self.lane_id,
            "bypass": self.bypass,
            "wet_dry": self.wet_dry,
            "color": self.color,
            "x": self.x,
            "y": self.y,
            "node_count": self.node_count,
            "global": {k: v.base for k, v in self.global_slots.items()},
            "global_locks": [k for k, v in self.global_slots.items() if v.locked],
            "nodes": [
                {k: s.base for k, s in nd.items()} for nd in self.node_slots
            ],
            "node_locks": [
                [k for k, s in nd.items() if s.locked] for nd in self.node_slots
            ],
            "per_module_lfos_enabled": self.per_module_lfos_enabled,
            "per_module_lfos": {k: dict(v) for k, v in self.per_module_lfos.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModuleInstance":
        m = cls(d["id"], d["type"], d["lane_id"], d.get("node_count", 1))
        m.bypass = d.get("bypass", False)
        m.wet_dry = d.get("wet_dry", 1.0)
        m.color = d.get("color", m.color)
        m.x = d.get("x", 0.0)
        m.y = d.get("y", 0.0)
        for k, v in d.get("global", {}).items():
            if k in m.global_slots:
                m.global_slots[k].base = v
        for k in d.get("global_locks", []):
            if k in m.global_slots:
                m.global_slots[k].locked = True
        for i, nd in enumerate(d.get("nodes", [])):
            if i >= len(m.node_slots):
                break
            for k, v in nd.items():
                if k in m.node_slots[i]:
                    m.node_slots[i][k].base = v
        for i, locks in enumerate(d.get("node_locks", [])):
            if i < len(m.node_slots):
                for k in locks:
                    if k in m.node_slots[i]:
                        m.node_slots[i][k].locked = True
        m.per_module_lfos_enabled = d.get("per_module_lfos_enabled", False)
        saved = d.get("per_module_lfos", {})
        for k in m.per_module_lfos:
            if k in saved:
                m.per_module_lfos[k].update(saved[k])
        return m


@dataclass
class Lane:
    id: str
    name: str
    mode: LaneMode = LaneMode.SERIAL
    module_ids: list[str] = field(default_factory=list)
    gain: float = 1.0
    input_bus: str = "input"
    output_bus: str = "master"
    spatial_map: str = "stereo"
    headroom_db: float = -12.0   # -12 dB nominal headroom per lane (section 3)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["mode"] = self.mode.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lane":
        return cls(
            id=d["id"], name=d["name"], mode=LaneMode(d.get("mode", "serial")),
            module_ids=list(d.get("module_ids", [])), gain=d.get("gain", 1.0),
            input_bus=d.get("input_bus", "input"), output_bus=d.get("output_bus", "master"),
            spatial_map=d.get("spatial_map", "stereo"),
            headroom_db=d.get("headroom_db", -12.0),
        )


class Patch:
    """Top-level save unit (section 3 routing table)."""

    SAVE_VERSION = 2

    def __init__(self, channel_count: int = 2, sample_rate: int = 48000,
                 block_size: int = 256, tempo: float = 120.0, root_seed: int = 1):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.channel_count = channel_count
        self.tempo = tempo
        self.root_seed = root_seed
        self.name = "untitled"
        self.expert_override = False   # arms unsafe ranges for randomize/mod
        self.lanes: dict[str, Lane] = {}
        self.modules: dict[str, ModuleInstance] = {}
        self.buffers: dict[str, BufferRef] = {
            b: BufferRef(id=b, kind="named") for b in ("A", "B", "C", "D")
        }
        self.spatial = SpatialBus(n=channel_count)
        # Free-form modular routing: directed edges src.output -> dst.input.
        # Modules with no outgoing audio edge are terminal (summed to master).
        self.connections: list[dict[str, str]] = []   # [{"id","src","dst"}]
        self.feedback_edges: dict[str, FeedbackEdge] = {}
        self.recorders: dict[str, Recorder] = {
            "main": Recorder(id="main", target="master")
        }
        # modulation + scenes are owned by their engines but serialised here
        self.mod_sources: list[dict[str, Any]] = []
        self.mod_routes: list[dict[str, Any]] = []
        self.scenes: list[dict[str, Any]] = []
        self.controller_maps: list[dict[str, Any]] = []
        self.macros: list[dict[str, Any]] = []

    # -- editing helpers --------------------------------------------------- #
    def add_lane(self, lane: Lane) -> Lane:
        self.lanes[lane.id] = lane
        return lane

    def add_module(self, m: ModuleInstance) -> ModuleInstance:
        self.modules[m.id] = m
        if m.lane_id in self.lanes and m.id not in self.lanes[m.lane_id].module_ids:
            self.lanes[m.lane_id].module_ids.append(m.id)
        return m

    def remove_module(self, mid: str) -> None:
        m = self.modules.pop(mid, None)
        if m and m.lane_id in self.lanes:
            ln = self.lanes[m.lane_id]
            if mid in ln.module_ids:
                ln.module_ids.remove(mid)
        # drop any connections touching it
        self.connections = [c for c in self.connections
                            if c["src"] != mid and c["dst"] != mid]

    # -- connection graph -------------------------------------------------- #
    def can_output(self, mid: str) -> bool:
        m = self.modules.get(mid)
        return bool(m and m.spec.is_audio)            # VIZ has no output

    def can_input(self, mid: str) -> bool:
        m = self.modules.get(mid)
        return bool(m and m.spec.insert_capable)      # GEN is a pure source

    def connection_legal(self, src: str, dst: str) -> tuple[bool, str]:
        if src == dst:
            return False, "cannot connect a module to itself"
        if src not in self.modules or dst not in self.modules:
            return False, "unknown module"
        if not self.can_output(src):
            return False, f"{src} has no audio output"
        if not self.can_input(dst):
            return False, f"{dst} has no audio input"
        if any(c["src"] == src and c["dst"] == dst for c in self.connections):
            return False, "already connected"
        if self._creates_cycle(src, dst):
            return False, "would create a feedback cycle (use armed feedback edges)"
        return True, ""

    def _creates_cycle(self, src: str, dst: str) -> bool:
        # adding src->dst cycles iff src is already reachable from dst
        adj: dict[str, list[str]] = {}
        for c in self.connections:
            adj.setdefault(c["src"], []).append(c["dst"])
        stack, seen = [dst], set()
        while stack:
            n = stack.pop()
            if n == src:
                return True
            if n in seen:
                continue
            seen.add(n)
            stack.extend(adj.get(n, []))
        return False

    def add_connection(self, src: str, dst: str, cid: str | None = None) -> dict | None:
        ok, _ = self.connection_legal(src, dst)
        if not ok:
            return None
        c = {"id": cid or f"c_{src}_{dst}", "src": src, "dst": dst}
        self.connections.append(c)
        return c

    def remove_connection(self, cid: str) -> None:
        self.connections = [c for c in self.connections if c["id"] != cid]

    def topo_order(self) -> list[str]:
        """Kahn topological sort of module ids (sources first). Any module in a
        residual cycle is appended at the end so it still gets built."""
        ids = list(self.modules)
        indeg = {m: 0 for m in ids}
        adj: dict[str, list[str]] = {m: [] for m in ids}
        for c in self.connections:
            if c["src"] in indeg and c["dst"] in indeg:
                adj[c["src"]].append(c["dst"])
                indeg[c["dst"]] += 1
        queue = [m for m in ids if indeg[m] == 0]
        order: list[str] = []
        while queue:
            n = queue.pop(0)
            order.append(n)
            for d in adj[n]:
                indeg[d] -= 1
                if indeg[d] == 0:
                    queue.append(d)
        order += [m for m in ids if m not in order]    # residual (cycles)
        return order

    def terminals(self) -> list[str]:
        """Audio modules with no outgoing edge — summed to master."""
        has_out = {c["src"] for c in self.connections}
        return [mid for mid in self.modules
                if self.can_output(mid) and mid not in has_out]

    def incoming(self, mid: str) -> list[str]:
        return [c["src"] for c in self.connections if c["dst"] == mid]

    def node_counts(self) -> dict[str, int]:
        return {mid: m.node_count for mid, m in self.modules.items()}

    def find_slot(self, module_id: str, param_id: str, node: int | None) -> ParamSlot | None:
        m = self.modules.get(module_id)
        if not m:
            return None
        if node is None:
            return m.global_slots.get(param_id)
        if 0 <= node < len(m.node_slots):
            return m.node_slots[node].get(param_id)
        return None
