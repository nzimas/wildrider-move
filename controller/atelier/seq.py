"""MIDI sequencer engine.

A clock-driven, Turing-machine (shift-register) sequencer that drives downstream
modules over the MIDI/control graph. Each module a SEQ is connected to gets a set
of lanes built automatically from the target's nature:

  * sound generator  -> 1 NOTE lane (pitch, snapped to a per-target root+scale)
                        + 3 CC lanes
  * fx / insert      -> 3 CC lanes

Every lane is an independent shift register with its own PATTERN LENGTH (so lanes
of different lengths run polymetrically against the shared clock) and a MUTATION
amount (0 = locked loop, 1 = fully random each step — the Music-Thing Turing
Machine behaviour). The engine runs in the controller's control-rate loop; it is
the system's clock source (tempo in BPM x clock division).

This is the first sequencing method; the lane API is method-agnostic so other
generators (euclid, ratchet, etc.) can be added later.
"""
from __future__ import annotations

import random
from typing import Any, Callable

MAX_STEPS = 32

SCALES: dict[str, list[int]] = {
    "chromatic": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "pentatonic": [0, 2, 4, 7, 9],
    "minorPent": [0, 3, 5, 7, 10],
    "wholetone": [0, 2, 4, 6, 8, 10],
    "octaves": [0, 12],
    "fifths": [0, 7],
}
SCALE_NAMES = list(SCALES)


class Lane:
    """One Turing-machine shift register driving one parameter of one target."""

    def __init__(self, kind: str, target: str, param: str = "", label: str = ""):
        self.kind = kind                 # "note" | "cc"
        self.target = target
        self.param = param               # cc: target param id; note: pitch param id
        self.label = label
        self.enabled = True
        self.length = 8                  # pattern length (steps) — polymeter
        self.mutation = 0.15             # 0 locked loop .. 1 fully random
        self.step = 0
        self.register = [random.random() for _ in range(MAX_STEPS)]
        # cc
        self.lo = 0.0
        self.hi = 1.0                    # value range in the param's normalised space
        # note: PITCH is fixed (the per-target root). The shift-register engine drives
        # rhythm (trigger/rest), VELOCITY and note LENGTH via parallel registers that
        # mutate together. (Melodic pitch sequencing is a future, separate module.)
        self.root = 48
        self.vel_param = ""              # target's velocity param (set on build)
        self.density = 0.75              # rhythm density: chance a step fires
        self.reg_vel = [random.random() for _ in range(MAX_STEPS)]   # velocity register
        self.reg_len = [random.random() for _ in range(MAX_STEPS)]   # gate-length register
        self._gate_off_at: float | None = None
        self._gate_on = False

    def advance(self, mut_scale: float = 1.0) -> None:
        self.step = (self.step + 1) % max(1, int(self.length))
        if random.random() < self.mutation * mut_scale:    # Turing mutation (global-scaled)
            self.register[self.step] = random.random()
            if self.kind == "note":
                self.reg_vel[self.step] = random.random()
                self.reg_len[self.step] = random.random()

    def _val(self) -> float:
        return self.register[self.step % MAX_STEPS]

    def cc_norm(self) -> float:
        return self.lo + self._val() * (self.hi - self.lo)

    def trigger(self, dens_scale: float = 1.0) -> bool:
        """Rhythm: does this step fire? (timing, from the register; pitch is fixed.)"""
        return self._val() < (self.density * dens_scale)

    def velocity(self) -> int:
        """Per-step velocity from the register, in a usable/audible range (48..127 —
        a register value of 0 would otherwise be a near-silent note)."""
        return int(48 + self.reg_vel[self.step % MAX_STEPS] * 79)

    def gate_fraction(self) -> float:
        """Per-step note length (fraction of a step) from the register, with a floor
        so notes have time to sound (0.25..1.0)."""
        return 0.25 + self.reg_len[self.step % MAX_STEPS] * 0.75

    def randomize(self, rng: random.Random) -> None:
        """Generate fresh patterns for instant new rhythms / dynamics / CC."""
        self.register = [rng.random() for _ in range(MAX_STEPS)]
        self.length = rng.randint(3, 16)
        self.mutation = rng.uniform(0.0, 0.4)
        if self.kind == "note":
            self.reg_vel = [rng.random() for _ in range(MAX_STEPS)]
            self.reg_len = [rng.random() for _ in range(MAX_STEPS)]
            self.density = rng.uniform(0.4, 0.95)
        else:
            self.lo = rng.uniform(0.0, 0.4)
            self.hi = rng.uniform(0.55, 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "target": self.target, "param": self.param,
            "label": self.label, "enabled": self.enabled, "length": self.length,
            "mutation": self.mutation, "step": self.step,
            "lo": self.lo, "hi": self.hi,
            "root": self.root, "vel_param": self.vel_param, "density": self.density,
            "register": self.register, "reg_vel": self.reg_vel, "reg_len": self.reg_len,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lane":
        ln = cls(d.get("kind", "cc"), d.get("target", ""), d.get("param", ""), d.get("label", ""))
        for k in ("enabled", "length", "mutation", "lo", "hi", "root", "vel_param",
                  "density", "register", "reg_vel", "reg_len"):
            if k in d:
                setattr(ln, k, d[k])
        return ln


class SeqState:
    def __init__(self) -> None:
        self.tempo = 120.0
        self.division = 4.0          # steps per beat (4 = 16th notes)
        self.running = True
        self.swing = 0.0
        self.accum = 0.0
        self.dens_scale = 1.0        # global density scaler (modulatable, from slot)
        self.mut_scale = 1.0         # global mutation scaler (modulatable, from slot)
        self.targets: dict[str, list[Lane]] = {}   # target_mid -> lanes

    def step_dur(self) -> float:
        return 60.0 / max(1e-6, self.tempo * self.division)


class SeqEngine:
    """Owns every SEQ module's clock + lanes and produces per-step write events."""

    def __init__(self) -> None:
        self.seqs: dict[str, SeqState] = {}
        self.now = 0.0

    # -- lifecycle --------------------------------------------------------- #
    def add(self, mid: str) -> None:
        self.seqs.setdefault(mid, SeqState())

    def remove(self, mid: str) -> None:
        self.seqs.pop(mid, None)

    def set_clock(self, mid: str, **kw) -> None:
        st = self.seqs.get(mid)
        if not st:
            return
        if "tempo" in kw and kw["tempo"] is not None:
            st.tempo = float(kw["tempo"])
        if "division" in kw and kw["division"] is not None:
            st.division = float(kw["division"])
        if "running" in kw and kw["running"] is not None:
            st.running = bool(kw["running"])
        if "swing" in kw and kw["swing"] is not None:
            st.swing = float(kw["swing"])

    def rebuild_targets(self, mid: str, targets: list[dict[str, Any]]) -> None:
        """targets: [{mid, generative, pitch_param, root, cc_params:[(pid,label)]}].
        Lanes are created for newly-connected targets and dropped for removed ones;
        existing targets keep their lanes (so editing a patch doesn't reset them)."""
        st = self.seqs.get(mid)
        if not st:
            return
        keep = {t["mid"] for t in targets}
        for gone in [t for t in st.targets if t not in keep]:
            del st.targets[gone]
        for t in targets:
            tmid = t["mid"]
            if tmid in st.targets:
                continue
            lanes: list[Lane] = []
            ccs = t.get("cc_params", [])[:3]
            if t.get("generative") and t.get("pitch_param"):
                nl = Lane("note", tmid, t["pitch_param"], "note")
                nl.root = int(t.get("root", 48))
                nl.vel_param = t.get("vel_param", "")
                lanes.append(nl)
            for i in range(3):
                pid, label = ccs[i] if i < len(ccs) else ("", f"CC {i + 1}")
                lanes.append(Lane("cc", tmid, pid, label or f"CC {i + 1}"))
            st.targets[tmid] = lanes

    def lane(self, mid: str, target: str, index: int) -> Lane | None:
        st = self.seqs.get(mid)
        if not st or target not in st.targets:
            return None
        lanes = st.targets[target]
        return lanes[index] if 0 <= index < len(lanes) else None

    # -- randomizers (lane / target / whole module) ------------------------ #
    def randomize(self, mid: str, rng: random.Random,
                  target: str | None = None, index: int | None = None) -> int:
        st = self.seqs.get(mid)
        if not st:
            return 0
        n = 0
        for tmid, lanes in st.targets.items():
            if target is not None and tmid != target:
                continue
            for i, ln in enumerate(lanes):
                if index is not None and i != index:
                    continue
                ln.randomize(rng)
                n += 1
        return n

    # -- clock tick -------------------------------------------------------- #
    def tick(self, dt: float) -> list[tuple]:
        """Advance all running clocks; return write events:
          ("cc",   target, param, norm)
          ("note", target, pitch_param, note_or_None)
          ("gate", target, 0|1)
        """
        self.now += dt
        out: list[tuple] = []
        for st in self.seqs.values():
            # close any gates whose hold time elapsed (retrigger between steps)
            for lanes in st.targets.values():
                for ln in lanes:
                    if ln.kind == "note" and ln._gate_on and ln._gate_off_at is not None \
                            and self.now >= ln._gate_off_at:
                        out.append(("gate", ln.target, 0))
                        ln._gate_on = False
            if not st.running:
                continue
            st.accum += dt
            sdur = st.step_dur()
            guard = 0
            while st.accum >= sdur and guard < 8:
                st.accum -= sdur
                guard += 1
                for lanes in st.targets.values():
                    for ln in lanes:
                        if not ln.enabled:
                            continue
                        ln.advance(st.mut_scale)
                        if ln.kind == "cc":
                            if ln.param:
                                out.append(("cc", ln.target, ln.param, ln.cc_norm()))
                        elif ln.trigger(st.dens_scale):     # rhythm: fire the fixed root note
                            out.append(("note", ln.target, ln.param, ln.root))
                            if ln.vel_param:               # per-step velocity (register)
                                out.append(("vel", ln.target, ln.vel_param, ln.velocity()))
                            out.append(("gate", ln.target, 1))
                            ln._gate_on = True
                            ln._gate_off_at = self.now + max(0.02, ln.gate_fraction()) * sdur
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            mid: {
                "tempo": st.tempo, "division": st.division, "running": st.running,
                "swing": st.swing,
                "targets": {t: [ln.to_dict() for ln in lanes] for t, lanes in st.targets.items()},
            } for mid, st in self.seqs.items()
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        self.seqs.clear()
        for mid, sd in (data or {}).items():
            st = SeqState()
            st.tempo = sd.get("tempo", 120.0)
            st.division = sd.get("division", 4.0)
            st.running = sd.get("running", True)
            st.swing = sd.get("swing", 0.0)
            for tmid, lanes in sd.get("targets", {}).items():
                st.targets[tmid] = [Lane.from_dict(ld) for ld in lanes]
            self.seqs[mid] = st
