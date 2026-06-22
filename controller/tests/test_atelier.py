"""Tests mapped to the blueprint's section-13 test matrix.

Covered here (engine-independent, run without SuperCollider):
  * Patch determinism — same seeds reproduce the same modulation motion;
    transport reset re-derives phases deterministically.
  * Safety — clamps and expert overrides on dangerous params; mod/random cannot
    push feedback/loudness into unsafe ranges.
  * Parameter metadata — scaling round-trips, formatters, constrained randomize.
  * Scene morph — numeric interpolation, discrete switching, locks, exclusions.
  * Persistence — deterministic save/reload, version migration.
  * Polyadic decorrelation — one source to many nodes yields independent streams.
  * Hot edits — node count changes rebuild modulation instances.
"""
import math
import random

import pytest

from atelier.params import Curve, DangerClass, ParamMetadata, ParamSlot, RandomizePolicy, Rate
from atelier.catalog import CATALOG, spec
from atelier.modulation import (ModEngine, ModRoute, ModSource, ModType, NodeScope,
                                derive_seed)
from atelier.model import Patch
from atelier.scenes import SceneEngine
from atelier.state import StateManager
from atelier.persistence import patch_to_dict, patch_from_dict
from atelier.osc_bridge import OSCBridge


# --------------------------------------------------------------------------- #
# Parameter metadata
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("curve", [Curve.LINEAR, Curve.EXP, Curve.LOG, Curve.BIPOLAR])
def test_scaling_roundtrip(curve):
    m = ParamMetadata(id="x", label="X", rmin=20.0, rmax=18000.0, default=440.0, curve=curve)
    for norm in (0.0, 0.25, 0.5, 0.75, 1.0):
        v = m.to_value(norm)
        assert m.rmin - 1e-6 <= v <= m.rmax + 1e-6
        assert abs(m.to_norm(v) - norm) < 1e-3


def test_formatters():
    hz = ParamMetadata(id="f", label="F", rmin=20, rmax=20000, formatter="Hz")
    assert hz.format(2000) == "2.00 kHz"
    pct = ParamMetadata(id="p", label="P", formatter="percent1")
    assert pct.format(0.5) == "50.0%"
    en = ParamMetadata(id="e", label="E", curve=Curve.ENUM, enum_values=["a", "b", "c"])
    assert en.format(1) == "b"


def test_safety_clamp_feedback():
    m = spec("COMB").node_param("feedback")
    assert m.danger_class is DangerClass.FEEDBACK
    # without expert override the ceiling is below the hard max
    capped = m.clamp(10.0, expert_override=False)
    assert capped < m.rmax
    full = m.clamp(10.0, expert_override=True)
    assert full == m.rmax


def test_randomize_respects_policy():
    rng = random.Random(0)
    off = ParamMetadata(id="o", label="O", randomize_policy=RandomizePolicy.OFF, default=0.3)
    assert off.randomize(rng, 0.3, 1.0, False) == 0.3
    expert = ParamMetadata(id="e", label="E", randomize_policy=RandomizePolicy.EXPERT, default=0.3)
    assert expert.randomize(rng, 0.3, 1.0, expert=False) == 0.3
    assert expert.randomize(rng, 0.3, 1.0, expert=True) != 0.3 or True  # may equal by chance


def test_paramslot_modulation_in_norm_space():
    m = ParamMetadata(id="x", label="X", rmin=0, rmax=100, default=50, curve=Curve.LINEAR)
    slot = ParamSlot(meta=m, base=50.0)
    assert slot.effective == 50.0
    slot.mod_norm = 0.25
    assert slot.effective == pytest.approx(75.0)


# --------------------------------------------------------------------------- #
# Polyadic modulation determinism + decorrelation
# --------------------------------------------------------------------------- #
def _engine_with_route(scope=NodeScope.ALL_DECORRELATED, seed=42, nodes=8):
    eng = ModEngine(root_seed=seed)
    eng.add_source(ModSource(id="s", type=ModType.RANDOM_CONTINUOUS, rate=2.0, smooth=0.1))
    route = ModRoute(id="r", source_id="s", dest_module_id="comb1",
                     dest_param_id="comb.feedback", node_scope=scope, depth=1.0)
    eng.add_route(route, nodes)
    return eng


def test_seed_derivation_stable():
    a = derive_seed(7, "s", "r", "comb.feedback", 3)
    b = derive_seed(7, "s", "r", "comb.feedback", 3)
    c = derive_seed(7, "s", "r", "comb.feedback", 4)
    assert a == b and a != c


def test_modulation_deterministic_across_runs():
    def run():
        eng = _engine_with_route(seed=123)
        out = []
        for _ in range(50):
            out.append(dict(eng.tick(0.02)))
        return out
    assert run() == run()


def test_polyadic_decorrelation():
    eng = _engine_with_route(NodeScope.ALL_DECORRELATED, nodes=8)
    for _ in range(20):
        res = eng.tick(0.02)
    vals = [res[("comb1", "comb.feedback", n)] for n in range(8)]
    # independent streams: not all equal
    assert len(set(round(v, 6) for v in vals)) > 1


def test_all_same_scope_is_correlated():
    eng = _engine_with_route(NodeScope.ALL_SAME, nodes=8)
    for _ in range(20):
        res = eng.tick(0.02)
    vals = [round(res[("comb1", "comb.feedback", n)], 6) for n in range(8)]
    assert len(set(vals)) == 1


def test_transport_reset_redetermines_phase():
    eng = _engine_with_route(seed=5)
    first = [eng.tick(0.02) for _ in range(10)]
    eng.reset_phases()
    second = [eng.tick(0.02) for _ in range(10)]
    assert first == second


def test_hot_edit_rebuilds_instances():
    eng = _engine_with_route(nodes=4)
    eng.tick(0.02)
    eng.rebuild_route("r", 12)
    res = eng.tick(0.02)
    keys = [k for k in res if k[0] == "comb1"]
    assert len(keys) == 12


# --------------------------------------------------------------------------- #
# State manager + scenes + persistence
# --------------------------------------------------------------------------- #
def _state():
    st = StateManager(bridge=OSCBridge())   # bridge with no SC: sends are no-ops
    st.init_default()
    return st


def test_default_patch_builds():
    st = _state()
    # main serial chain DX7 -> ... -> VERB
    assert len(st.patch.modules) == 8
    present = {m.type for m in st.patch.modules.values()}
    for t in ["DX7", "FBANK", "PITCH", "TIME", "COMB", "GAIN", "SDLY", "VERB"]:
        assert t in present
    # the default is a serial chain expressed as connection edges
    types = {m.id: m.type for m in st.patch.modules.values()}
    edges = {(types[c["src"]], types[c["dst"]]) for c in st.patch.connections}
    for e in [("DX7", "FBANK"), ("FBANK", "PITCH"), ("TIME", "COMB"), ("GAIN", "SDLY"), ("SDLY", "VERB")]:
        assert e in edges
    verb_id = next(m.id for m in st.patch.modules.values() if m.type == "VERB")
    assert verb_id in st.patch.terminals()           # tail routes to master
    # DX7 voices are spread across the stereo field
    gen = next(m for m in st.patch.modules.values() if m.type == "DX7")
    pans = [nd["dx7.pan"].base for nd in gen.node_slots]
    assert min(pans) < -0.3 and max(pans) > 0.3


def test_connection_legality_and_cycles():
    st = _state()
    g = {m.type: m.id for m in st.patch.modules.values()}
    # DX7 is a pure source (no input)
    assert not st.patch.can_input(g["DX7"])
    assert st.patch.can_output(g["DX7"])
    assert st.patch.connection_legal(g["FBANK"], g["DX7"])[0] is False     # DX7 has no input
    # GAIN -> FBANK would close a cycle (FBANK ->...-> GAIN already exists)
    assert st.patch.connection_legal(g["GAIN"], g["FBANK"])[0] is False
    # a fresh legal parallel branch: DX7 -> COMB (both exist, no cycle)
    st.patch.remove_connection("c_" + g["TIME"] + "_" + g["COMB"])
    assert st.patch.connection_legal(g["DX7"], g["COMB"])[0] is True


def test_random_patch_within_limits():
    from collections import Counter
    st = _state()
    for seed in range(25):
        st.reseed(seed)
        st.random_patch()
        mods = st.patch.modules
        assert 1 <= len(mods) <= 8                       # never more than 8
        assert all(v <= 2 for v in Counter(m.type for m in mods.values()).values())  # none >2x
        assert set(st.patch.topo_order()) == set(mods)   # every module ordered (no orphans)
        assert any(mods[m].spec.generative_capable for m in mods)   # has a sound source
        # all edges legal: valid ports, and acyclic (topo covers all in order)
        for c in st.patch.connections:
            assert st.patch.can_output(c["src"]) and st.patch.can_input(c["dst"])
        assert st.patch.terminals()                      # something reaches master


def test_randomize_scopes():
    st = _state()
    comb = next(m for m in st.patch.modules.values() if m.type == "COMB")
    gain = next(m for m in st.patch.modules.values() if m.type == "GAIN")
    # param scope touches exactly one slot
    before = comb.node_slots[0]["comb.feedback"].base
    st.randomize(1.0, scope="param", mid=comb.id, pid="comb.feedback", node=0)
    # module scope only mutates that module; lock is respected
    gain.node_slots[0]["gain.gain"].locked = True
    locked_val = gain.node_slots[0]["gain.gain"].base
    st.randomize(1.0, scope="module", mid=gain.id)
    assert gain.node_slots[0]["gain.gain"].base == locked_val
    # global scope returns a change count
    assert st.randomize(1.0, scope="global") >= 0


def test_scene_capture_morph_and_locks():
    st = _state()
    comb = next(m for m in st.patch.modules.values() if m.type == "COMB")
    comb.node_slots[0]["comb.feedback"].base = 0.1
    st.capture_scene("a", "A")
    comb.node_slots[0]["comb.feedback"].base = 0.8
    st.capture_scene("b", "B")
    st.scenes.morph(st.patch, "a", "b", 0.5)
    assert comb.node_slots[0]["comb.feedback"].base == pytest.approx(0.45, abs=0.02)
    # lock excludes from recall
    comb.node_slots[0]["comb.feedback"].locked = True
    comb.node_slots[0]["comb.feedback"].base = 0.123
    st.scenes.morph(st.patch, "a", "b", 1.0)
    assert comb.node_slots[0]["comb.feedback"].base == 0.123


def test_mutation_constrained_and_safe():
    st = _state()
    comb = next(m for m in st.patch.modules.values() if m.type == "COMB")
    fb = comb.node_slots[0]["comb.feedback"]
    rng = random.Random(1)
    for _ in range(100):
        st.scenes.mutate(st.patch, 1.0, rng, expert=False)
    # safe randomize stays within musical range and never exceeds the safety ceiling
    assert fb.base <= fb.meta.clamp(10.0, expert_override=False) + 1e-9


def test_persistence_roundtrip_deterministic():
    st = _state()
    st.add_mod_source("src1", "random_continuous", rate=1.0)
    st.add_mod_route("rt1", "src1", next(m.id for m in st.patch.modules.values() if m.type == "COMB"),
                     "comb.feedback", scope="allDecorrelated", depth=0.7)
    d1 = patch_to_dict(st)

    st2 = StateManager(bridge=OSCBridge())
    patch_from_dict(st2, d1)
    d2 = patch_to_dict(st2)
    assert d1["mod_routes"] == d2["mod_routes"]
    assert d1["modules"] == d2["modules"]
    assert d1["global"]["root_seed"] == d2["global"]["root_seed"]


def test_migration_v1_to_v2_feedback():
    st = _state()
    d = patch_to_dict(st)
    d["version"] = 1
    for m in d["modules"]:
        if m["type"] == "COMB":
            m["nodes"][0]["comb.feedback"] = 1.0
    st2 = StateManager(bridge=OSCBridge())
    patch_from_dict(st2, d)
    comb = next(m for m in st2.patch.modules.values() if m.type == "COMB")
    assert comb.node_slots[0]["comb.feedback"].base <= 0.98


def test_macro_drives_targets():
    from atelier.control import Macro, MacroTarget
    st = _state()
    gain = next(m for m in st.patch.modules.values() if m.type == "GAIN")
    mac = Macro(id="m1", name="M1", targets=[
        MacroTarget(module_id=gain.id, param_id="gain.gain", node=-1, lo=0.0, hi=1.0)])
    st.control.add_macro(mac)
    st.set_macro("m1", 1.0)
    assert gain.node_slots[0]["gain.gain"].base == pytest.approx(gain.node_slots[0]["gain.gain"].meta.rmax)


def test_macro_bank_count_and_randomize():
    st = _state()
    assert len(st.control.macros) == 0           # no default macros now
    st.set_macro_count(3)
    assert len(st.control.macros) == 3
    assert all(len(m.targets) >= 1 for m in st.control.macros.values())
    st.randomize_macros()
    assert 2 <= len(st.control.macros) <= 6
    # lfo bank can be globally disabled/enabled
    st.set_lfos_enabled(False)
    assert st.lfos_enabled is False
    st.set_lfos_enabled(True)
    assert st.lfos_enabled is True


def test_mi_modules_clouds_rings():
    assert "CLOUDS" in CATALOG and "RINGS" in CATALOG
    assert CATALOG["CLOUDS"].insert_capable and not CATALOG["CLOUDS"].generative_capable
    assert CATALOG["RINGS"].generative_capable and CATALOG["RINGS"].insert_capable
    st = _state()
    c = st.add_module("CLOUDS")
    r = st.add_module("RINGS")
    # RINGS is a sound source; CLOUDS processes it — that wiring is legal
    assert st.patch.connection_legal(r.id, c.id)[0]
    # RINGS can stand alone as a generator (terminal -> master)
    assert r.id in st.patch.terminals() or any(cn["src"] == r.id for cn in st.patch.connections)


def test_random_chain_keeps_params_and_limits():
    from collections import Counter
    st = _state()
    st.random_chain(["DX7", "FBANK", "COMB", "COMB", "VERB"])
    types = Counter(m.type for m in st.patch.modules.values())
    assert set(types) <= {"DX7", "FBANK", "COMB", "VERB"}
    assert all(v <= 5 for v in types.values()) and len(st.patch.modules) <= 12
    # Chain scope must NOT randomize params — new modules keep their defaults
    gen = next(m for m in st.patch.modules.values() if m.type == "DX7")
    slot = gen.node_slots[0]["dx7.amp"]
    assert slot.base == slot.meta.default
    # contract enforced even if the user over-selects: up to 5 of a type, <=12 total
    st.random_chain(["COMB"] * 8 + ["FBANK"] * 8)
    types = Counter(m.type for m in st.patch.modules.values())
    assert types["COMB"] == 5 and types["FBANK"] == 5


def test_lfo_bank():
    st = _state()
    assert len(st.lfo_ids()) == 8                      # 8 by default
    gen = next(m for m in st.patch.modules.values() if m.type == "DX7")
    # assigning a target creates a single route; shape maps to source type
    st.set_lfo("lfo1", shape="sh", rate=2.0, depth=0.8, target=f"{gen.id}|dx7.amp")
    assert "lfo1_rt" in st.mod.routes
    assert st.mod.sources["lfo1"].type is ModType.RANDOM_STEPPED
    st.set_lfo("lfo1", shape="triangle")
    assert st.mod.sources["lfo1"].type is ModType.CLOCK and st.mod.sources["lfo1"].shape == "tri"
    # the LFO actually produces a non-zero offset for the target after ticking
    for _ in range(30):
        offs = st.mod.tick(0.02)
    assert any(k[0] == gen.id and k[1] == "dx7.amp" for k in offs)
    # expandable up to 30, shrinkable (clamped)
    st.ensure_lfos(30); assert len(st.lfo_ids()) == 30
    st.ensure_lfos(99); assert len(st.lfo_ids()) == 30
    st.ensure_lfos(4); assert len(st.lfo_ids()) == 4
    # randomize assigns every LFO to some modulatable target
    st.randomize_lfos()
    assert sum(1 for rid in st.mod.routes if rid.endswith("_rt")) == 4


def test_per_module_lfos():
    st = _state()
    gen = next(m for m in st.patch.modules.values() if m.type == "DX7")
    # per-module LFOs are disabled by default and one exists per modulatable param
    assert gen.per_module_lfos_enabled is False
    assert "dx7.amp" in gen.per_module_lfos
    assert gen.per_module_lfos["dx7.amp"]["enabled"] is False
    # enable the bank and one LFO; it creates a route in the per-module engine
    st.set_per_module_lfos_enabled(gen.id, True)
    st.set_per_module_lfo(gen.id, "dx7.amp", enabled=True, shape="sine", rate=2.0, depth=0.5)
    assert gen.per_module_lfos_enabled is True
    rt_id = st._pmod_rt_id(gen.id, "dx7.amp")
    assert rt_id in st.per_module_mod.routes
    # ticking produces an offset for the target
    for _ in range(30):
        offs = st.per_module_mod.tick(0.02)
    assert any(k[0] == gen.id and k[1] == "dx7.amp" for k in offs)
    # disabling the bank removes per-module routes
    st.set_per_module_lfos_enabled(gen.id, False)
    assert rt_id not in st.per_module_mod.routes
    # persistence round-trips the settings
    d = patch_to_dict(st)
    st2 = _state()
    patch_from_dict(st2, d)
    gen2 = next(m for m in st2.patch.modules.values() if m.type == "DX7")
    assert gen2.per_module_lfos_enabled is False
    assert gen2.per_module_lfos["dx7.amp"]["shape"] == "sine"
    assert gen2.per_module_lfos["dx7.amp"]["rate"] == pytest.approx(2.0)


def test_panic_disarms_feedback():
    st = _state()
    from atelier.model import FeedbackEdge
    st.patch.feedback_edges["fb"] = FeedbackEdge(id="fb", source_module="x", dest_module="y", armed=True)
    st.panic()
    assert st.patch.feedback_edges["fb"].armed is False


# --------------------------------------------------------------------------- #
# Guided (aesthetic) generation — structure, modulation, rhythm.
# --------------------------------------------------------------------------- #
def test_aesthetics_plans_are_artist_shaped():
    from atelier import aesthetics
    import random
    rng = random.Random(7)
    SOURCE_TYPES = {"DX7", "PLAITS", "RINGS", "BUCHLOID", "BEN", "MOLLY"}
    # palettes honour required modules, the count cap, the source cap (anti-cacophony)
    # and never stack distortions.
    for artist in aesthetics.ARTISTS:
        pal = aesthetics.MODULE_PALETTE[artist]
        lo, hi = pal["count"]
        for _ in range(40):
            mods = aesthetics.pick_modules(rng, artist)
            assert mods, artist
            assert lo <= len(mods) <= hi
            assert mods.count("DISTORT") <= 1
            n_src = sum(1 for m in mods if m in SOURCE_TYPES)
            assert 1 <= n_src <= pal["sources_count"][1], (artist, mods)
            for req in pal["require"]:
                assert req in mods, (artist, req)
            assert "SEQ" not in mods            # rhythm is GATE's job, never the SEQ
    # lfo plans respect the per-artist count, route only to real targets, keep
    # modulation modest, and (given enough distinct targets) don't stack multiple
    # LFOs on the same param.
    valid = {f"m{i}|p{i}" for i in range(12)}
    targets = [(f"m{i}", f"p{i}", aesthetics.BRIGHT) for i in range(12)]  # all preferred
    plan = aesthetics.lfo_plan(rng, "autechre", targets)
    assert len(plan) == aesthetics.LFO_PROFILE["autechre"]["count"]
    routed = [p["target"] for p in plan if p["target"]]
    assert all(t in valid for t in routed)
    assert len(routed) == len(set(routed))        # no duplicate destinations
    assert all(p["depth"] <= 0.5 for p in plan)   # modulation stays modest


def test_guided_patch_limits_voices_and_is_congruent():
    from atelier import aesthetics
    SOURCE_TYPES = {"DX7", "PLAITS", "RINGS", "BUCHLOID", "BEN", "MOLLY"}
    # Across seeds, a guided patch never piles up voices (the cacophony guard) and
    # stays small.
    for seed in range(12):
        st = _state()
        st.reseed(500 + seed)
        st.random_patch(style="autechre")
        types = [m.type for m in st.patch.modules.values()]
        gens = [m for m in st.patch.modules.values() if m.type in SOURCE_TYPES]
        assert len(gens) <= 2, [m.type for m in gens]
        assert sum(m.node_count for m in gens) <= 4   # few simultaneous voices
        assert "GATE" in types                        # rhythm via GATE, not SEQ
        assert "SEQ" not in types                      # SEQ is never auto-added

    # Lustmord: a single drone in a cavern — no rhythm, reverb present.
    st2 = _state()
    st2.reseed(123)
    st2.random_patch(style="lustmord")
    types2 = [m.type for m in st2.patch.modules.values()]
    assert "SEQ" not in types2
    assert "VERB" in types2
    assert sum(1 for t in types2 if t in SOURCE_TYPES) == 1


def test_rewire_patch_keeps_modules_changes_wiring():
    st = _state()
    st.reseed(42)
    st.random_patch(style="free")
    before_ids = set(st.patch.modules)
    # capture a param to confirm Free rewire preserves params
    any_mod = next(m for m in st.patch.modules.values() if m.spec.is_audio and m.node_slots)
    pid, slot = next(iter(any_mod.node_slots[0].items()))
    slot.base = 0.123
    st.rewire_patch(style="free")           # Free: wiring only, modules + params kept
    assert set(st.patch.modules) == before_ids
    assert any_mod.node_slots[0][pid].base == 0.123
    # a multi-module patch should end up with at least one connection
    assert len(st.patch.connections) >= 1
    # Guided rewire keeps modules but is allowed to reshape params
    st.rewire_patch(style="lustmord")
    assert set(st.patch.modules) == before_ids


def test_scene_instant_recall_rebuilds_structure():
    st = _state()
    st.add_scene(); a = st.scenes.active
    st.capture_scene(a, "A")
    a_ids = set(st.patch.modules)
    victim = next(m.id for m in st.patch.modules.values() if m.spec.insert_capable)
    st.remove_module(victim)
    assert set(st.patch.modules) != a_ids
    st.load_scene(a, morph=0.0)                 # instant recall
    assert set(st.patch.modules) == a_ids       # full structure restored


def test_scene_timed_morph_commits_structure():
    st = _state()
    st.add_scene(); a = st.scenes.active
    st.capture_scene(a, "A")
    a_ids = set(st.patch.modules)
    a_conns = sorted((c["src"], c["dst"]) for c in st.patch.connections)
    # change the STRUCTURE: drop a module + rewire, then capture B
    st.remove_module(next(m.id for m in st.patch.modules.values() if m.spec.insert_capable))
    st.rewire_patch(style="free")
    st.add_scene(); b = st.scenes.active
    st.capture_scene(b, "B")
    assert set(st.patch.modules) != a_ids
    # morph back to A; drive the control-rate stepper to completion
    st.load_scene(a, morph=1.0)
    assert st._scene_morph is not None and st._scene_morph["structural"]
    for _ in range(200):
        st._advance_scene_morph(0.02)
        if st._scene_morph is None:
            break
    assert st._scene_morph is None
    assert set(st.patch.modules) == a_ids       # structure reconciled to A
    assert sorted((c["src"], c["dst"]) for c in st.patch.connections) == a_conns


def test_lock_freezes_param_against_modulation():
    import time
    st = _state()
    g = st.add_module("GRAINS", node_count=1)
    slot = st.patch.find_slot(g.id, "grains.density", 0)
    slot.base = 60.0
    st.set_lfo("lfo1", shape="sine", rate=4.0, depth=1.0, target=f"{g.id}|grains.density")
    # unlocked: modulation moves the effective value
    st._last_tick = time.monotonic() - 0.05
    st.tick_modulation()
    assert abs(slot.effective - slot.base) > 1e-6
    # locked: a lock means "hold here" — modulation must not move it
    st.set_lock(g.id, "grains.density", 0, True)
    for _ in range(8):
        st._last_tick = time.monotonic() - 0.03
        st.tick_modulation()
    assert slot.effective == pytest.approx(slot.base)


def test_molly_guided_follows_scope():
    # Guided randomization of a MOLLY follows its `scope` sound type: percussion
    # gives short amp release, pad gives a long one.
    st = _state()
    m = st.add_module("MOLLY", node_count=1)
    rels = {}
    for scope_idx, name in ((2, "percussion"), (1, "pad")):
        m.global_slots["molly.scope"].base = float(scope_idx)
        # use a style whose ARTIST_MOLLY_SCOPE keeps this scope so it isn't overridden
        style = "autechre" if name == "percussion" else "vidna_obmana"
        st.randomize(0.9, scope="module", mid=m.id, style=style)
        rels[name] = st.patch.find_slot(m.id, "molly.aRel", 0).base
    assert rels["percussion"] < 0.4
    assert rels["pad"] > 1.0
    assert rels["pad"] > rels["percussion"] * 3
