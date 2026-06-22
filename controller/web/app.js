// Wildrider control surface client.
// Renders the whole instrument from the server's machine-describable snapshot
// (catalog metadata + live patch/modulation/scene/control state) and drives it
// over a websocket. No module knowledge is hard-coded here.
"use strict";

let S = null;            // latest full snapshot
let sel = null;          // selected module id
let selNode = 0;         // selected node index in the detail panel
let ws = null;

// SVG padlock icons — stroke-based to inherit button's currentColor.
// Closed: both shackle arms seat into the body. Open: right arm lifted out.
const SVG_LOCK_CLOSED = `<svg viewBox="0 0 12 14" width="11" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="1" y="6" width="10" height="7" rx="1.5"/><path d="M3 6V4a3 3 0 0 1 6 0v2"/></svg>`;
const SVG_LOCK_OPEN   = `<svg viewBox="0 0 12 14" width="11" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="1" y="6" width="10" height="7" rx="1.5"/><path d="M3 6V4a3 3 0 0 1 6 0V1.5"/></svg>`;

const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};

// ---------------------------------------------------------------- websocket
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (m) => onEvent(JSON.parse(m.data));
  ws.onclose = () => { setConn(false); setTimeout(connect, 1000); };
}
function send(cmd, extra) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(Object.assign({ cmd }, extra || {}))); }

// While a slider is being dragged we must NOT rebuild the detail panel — doing so
// replaces the <input> element and aborts the drag. Suppress re-renders for the
// duration of the drag and run any deferred refresh once the pointer is released.
let dragging = false, pendingRefresh = false;
document.addEventListener("pointerdown", (e) => {
  const t = e.target;
  if (t && t.tagName === "INPUT" && t.type === "range") dragging = true;
}, true);
function endDrag() {
  if (!dragging) return;
  dragging = false;
  if (pendingRefresh) { pendingRefresh = false; refresh(); }
}
document.addEventListener("pointerup", endDrag, true);
document.addEventListener("pointercancel", endDrag, true);

async function refresh() {
  if (dragging) { pendingRefresh = true; return; }   // don't yank the DOM mid-drag
  S = await (await fetch("/api/state")).json(); clearPos(); renderAll();
}
function clearPos() { for (const k in pos) delete pos[k]; }

function onEvent(ev) {
  switch (ev.type) {
    case "snapshot": case "reload":
      if (dragging) { pendingRefresh = true; break; }
      S = ev.snapshot; clearPos(); renderAll();
      if (ev.snapshot && ev.snapshot.recording) paintRecording(ev.snapshot.recording);
      break;
    case "record":
      paintRecording(ev); break;
    case "engine":
      updateEngine(ev); break;
    case "param":
      updateParam(ev); break;
    case "macro": break;
    // scene morph: a clip is being recalled — stream the canvas + panels live.
    case "scene_loading":
      morph = { id: ev.id, t: 0, structural: ev.structural }; renderScenes(); break;
    case "morph_frame":
      applyMorphFrame(ev); break;
    case "scene_loaded":
      morph = null;          // a preceding "reload" carries the authoritative state
      if (S) S.active_scene = ev.id;
      renderScenes(); break;
    // value-echoes for live controls — the UI already reflects the drag locally,
    // so a full re-render would only fight the slider. No-op.
    case "module_wet": case "per_module_lfo": case "lfo_set":
    case "seq_clock": case "seq_lane": break;
    default:
      refresh();   // structural change: pull a fresh authoritative snapshot
  }
}

// Live scene-morph frame: merge the (catalog-free) frame into S and redraw every
// level — canvas (nodes glide / appear / disappear, wiring updates), detail panel
// readouts, modulation and the scene progress. Skipped while dragging a slider.
let morph = null;   // { id, t, structural } while a timed morph is unfolding
function applyMorphFrame(ev) {
  if (!S) return;
  morph = { id: ev.frame.active_scene, t: ev.t, structural: morph && morph.structural };
  if (dragging) { renderScenes(); return; }
  const f = ev.frame;
  for (const k of ["modules", "connections", "midi_connections", "seq",
                   "mod_sources", "mod_routes", "lfos", "mod_targets", "active_scene"]) {
    if (k in f) S[k] = f[k];
  }
  if (f.global) Object.assign(S.global, f.global);
  clearPos();
  renderCanvas(); renderDetail(); renderLFOs(); renderModSources(); renderModRoutes();
  renderScenes();
}

// ---------------------------------------------------------------- topbar
function setConn(ok) {
  const p = $("conn");
  p.textContent = `engine: ${ok ? "online" : "offline"}`;
  p.className = "pill " + (ok ? "good" : "bad");
}
function updateEngine(ev) {
  setConn(ev.connected);
  const cpu = ev.cpu || {};
  const c = $("cpu");
  c.textContent = `cpu: ${Math.round((cpu.peak || 0) * 100)}% / ${cpu.nodes || 0} nodes`;
  c.className = "pill " + (ev.cpu_warn ? "warn" : "");
  renderMeters(ev.meters || []);
}

// ---------------------------------------------------------------- render all
function renderAll() {
  if (!S) return;
  $("seed").textContent = `seed: ${S.global.root_seed}`;
  renderCanvas();
  renderDetail();
  renderScenes();
  renderLFOs();
  renderModSources();
  renderModRoutes();
}

// ---------------------------------------------------------------- node canvas
const NODE_W = 156, PORT_Y = 24 + 7;   // port centre offset within a node
const pos = {};                        // live positions during drag {id:{x,y}}
function nodePos(m) { return pos[m.id] || { x: m.x, y: m.y }; }
function outPt(m) { const p = nodePos(m); return { x: p.x + NODE_W, y: p.y + PORT_Y }; }
function inPt(m) { const p = nodePos(m); return { x: p.x, y: p.y + PORT_Y }; }
function midiInPt(m) { const p = nodePos(m); return { x: p.x + NODE_W / 2, y: p.y }; }   // top-centre socket
const SVGNS = "http://www.w3.org/2000/svg";
function wirePath(a, b) {
  const dx = Math.max(40, Math.abs(b.x - a.x) * 0.5);
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
}

// Module category (for colour-coding): generator (pure voice), midi (SEQ),
// processor (everything else audio).
function moduleCat(type) {
  if (type === "SEQ") return "midi";
  const c = S.catalog[type];
  if (c && c.is_audio && c.generative_capable && !c.insert_capable) return "gen";
  return "proc";
}
// A module's pan param ("pan"/"spatialPos") — may live in node OR global params.
function panInfo(type) {
  const c = S.catalog[type];
  if (!c) return null;
  const isPan = (p) => ["pan", "spatialpos"].includes(p.id.split(".").pop().toLowerCase());
  const n = c.node_params.find(isPan);
  if (n) return { id: n.id, global: false };
  const g = c.global_params.find(isPan);
  if (g) return { id: g.id, global: true };
  return null;
}

// A minimal pan control: a centre baseline with a slidable tick that grows a
// bold tail/bar from centre toward the tick as it moves off-centre (L or R).
function mkPanControl(m, pan, cur) {
  const ctl = el("div", "pan-ctl");
  ctl.append(el("div", "pan-axis"));               // faint full-width baseline
  const bar = el("div", "pan-bar");                // bold tail from centre
  const knob = el("div", "pan-knob");              // the slidable tick
  ctl.append(bar, knob);
  const paint = (v) => {
    const off = v - 0.5;                            // -0.5..0.5
    ctl.classList.toggle("centered", Math.abs(off) < 1e-6);
    knob.style.left = (v * 100) + "%";
    if (off >= 0) { bar.style.left = "50%"; bar.style.right = "auto"; bar.style.width = (off * 100) + "%"; }
    else { bar.style.right = "50%"; bar.style.left = "auto"; bar.style.width = (-off * 100) + "%"; }
  };
  const apply = (v) => {
    if (pan.global) send("set_param_norm", { module: m.id, param: pan.id, node: null, value: v });
    else for (let i = 0; i < m.node_count; i++) send("set_param_norm", { module: m.id, param: pan.id, node: i, value: v });
  };
  const fromX = (clientX) => {
    const r = ctl.getBoundingClientRect();
    let v = (clientX - r.left) / Math.max(1, r.width);
    v = Math.max(0, Math.min(1, v));
    if (Math.abs(v - 0.5) < 0.05) v = 0.5;          // centre detent
    return v;
  };
  const move = (e) => { const v = fromX(e.clientX); paint(v); apply(v); };
  const up = () => {
    dragging = false;
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  ctl.addEventListener("pointerdown", (e) => {
    e.stopPropagation(); e.preventDefault(); dragging = true;
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    move(e);
  });
  ctl.addEventListener("dblclick", (e) => { e.stopPropagation(); paint(0.5); apply(0.5); });
  ctl.title = "module pan — drag the tick (L ◀ ▶ R); double-click to centre";
  paint(cur);
  return ctl;
}

function renderCanvas() {
  const cv = $("canvas");
  // remove old nodes (keep the <svg id=wires>)
  cv.querySelectorAll(".node").forEach((n) => n.remove());
  const byId = {};
  for (const m of S.modules) {
    byId[m.id] = m;
    const p = nodePos(m);
    const node = el("div", "node cat-" + moduleCat(m.type) + (m.id === sel ? " sel" : "") + (m.bypass ? " bypassed" : ""));
    node.style.left = p.x + "px"; node.style.top = p.y + "px"; node.dataset.id = m.id;
    const head = el("div", "nhead");
    head.append(el("span", "ntype", m.type));
    head.append(el("span", "nid", m.id));
    node.append(head);
    const body = el("div", "nbody");
    const r1 = el("div", "nrow"); r1.append(el("span", null, `${m.node_count} node${m.node_count > 1 ? "s" : ""}`));
    r1.append(el("span", null, m.bypass ? "byp" : `${Math.round(m.wet_dry * 100)}% wet`));
    body.append(r1);
    // quick per-module pan slider in the lower part of the box
    const pan = panInfo(m.type);
    if (pan) {
      const cur = pan.global
        ? (m.global[pan.id] || { norm: 0.5 }).norm
        : ((m.nodes[0] || {})[pan.id] || { norm: 0.5 }).norm;
      const pr = el("div", "nrow npan");
      pr.append(el("span", "npan-lbl", "pan"));
      pr.append(mkPanControl(m, pan, cur));
      body.append(pr);
    }
    node.append(body);
    if (m.has_input) { const pin = el("div", "port in"); pin.dataset.id = m.id; pin.dataset.kind = "in"; node.append(pin); }
    if (m.has_output) { const po = el("div", "port out"); po.dataset.id = m.id; po.dataset.kind = "out"; node.append(po); }
    // MIDI input socket — every module has one (for future MIDI generators /
    // sequencers / MIDI fx). Visual prep: distinct top-edge socket, not yet wired.
    { const mp = el("div", "port midi in"); mp.dataset.id = m.id; mp.dataset.kind = "midi"; mp.title = "MIDI in"; node.append(mp); }
    // SEQ has a MIDI output — drag it onto a module's MIDI-in to add sequencing lanes
    if (m.type === "SEQ") { const mo = el("div", "port midi out"); mo.dataset.id = m.id; mo.dataset.kind = "midiout"; mo.title = "MIDI out — drag to a module's MIDI in"; node.append(mo); }
    head.addEventListener("mousedown", (e) => startDragNode(e, m.id));
    head.addEventListener("click", (e) => {
      if (dragMoved) { dragMoved = false; return; }   // a drag, not a select click
      if (e.ctrlKey || e.metaKey) {                     // Ctrl/⌘-click toggles bypass (dims)
        send("bypass", { module: m.id, on: !m.bypass });
        return;
      }
      sel = (sel === m.id) ? null : m.id;              // click again to de-select
      selNode = 0; renderDetail(); highlightSel();
    });
    node.title = "Ctrl/⌘-click to bypass";
    node.addEventListener("contextmenu", (e) => e.preventDefault());   // Ctrl-click on macOS
    cv.append(node);
  }
  // wires
  const svg = $("wires");
  svg.querySelectorAll("path").forEach((p) => p.remove());
  for (const c of (S.connections || [])) {
    const a = byId[c.src], b = byId[c.dst];
    if (!a || !b) continue;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", wirePath(outPt(a), inPt(b)));
    path.dataset.cid = c.id;
    path.addEventListener("click", () => { if (confirm("Remove this connection?")) send("remove_connection", { id: c.id }); });
    svg.append(path);
  }
  // MIDI / control wires (dashed amber): SEQ midi-out -> module midi-in
  for (const c of (S.midi_connections || [])) {
    const a = byId[c.src], b = byId[c.dst];
    if (!a || !b) continue;
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("class", "midi");
    path.setAttribute("d", wirePath(outPt(a), midiInPt(b)));
    path.dataset.mid = c.id;
    path.addEventListener("click", () => { if (confirm("Remove this MIDI connection?")) send("remove_midi_connection", { id: c.id }); });
    svg.append(path);
  }
  bindPorts();
}

function highlightSel() {
  document.querySelectorAll("#canvas .node").forEach((n) =>
    n.classList.toggle("sel", n.dataset.id === sel));
}

function redrawWires() {
  const byId = {}; S.modules.forEach((m) => byId[m.id] = m);
  $("wires").querySelectorAll("path[data-cid]").forEach((path) => {
    const c = (S.connections || []).find((x) => x.id === path.dataset.cid);
    if (c && byId[c.src] && byId[c.dst]) path.setAttribute("d", wirePath(outPt(byId[c.src]), inPt(byId[c.dst])));
  });
  $("wires").querySelectorAll("path[data-mid]").forEach((path) => {
    const c = (S.midi_connections || []).find((x) => x.id === path.dataset.mid);
    if (c && byId[c.src] && byId[c.dst]) path.setAttribute("d", wirePath(outPt(byId[c.src]), midiInPt(byId[c.dst])));
  });
}

let dragMoved = false;   // set during a node drag so the trailing click won't toggle selection
function startDragNode(e, id) {
  e.preventDefault();
  dragMoved = false;
  const m = S.modules.find((x) => x.id === id);
  const start = nodePos(m), sx = e.clientX, sy = e.clientY;
  const node = [...document.querySelectorAll("#canvas .node")].find((n) => n.dataset.id === id);
  const move = (ev) => {
    if (Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) > 3) dragMoved = true;
    const x = Math.max(0, start.x + (ev.clientX - sx)), y = Math.max(0, start.y + (ev.clientY - sy));
    pos[id] = { x, y }; node.style.left = x + "px"; node.style.top = y + "px"; redrawWires();
  };
  const up = () => {
    document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up);
    const p = pos[id]; if (p) send("move_module", { module: id, x: p.x, y: p.y });
  };
  document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
}

// dragging a wire from an output port to an input port
let wireDrag = null;
function bindPorts() {
  document.querySelectorAll("#canvas .port.out").forEach((po) => {
    po.addEventListener("mousedown", (e) => {
      e.preventDefault(); e.stopPropagation();
      const src = po.dataset.id;
      const tmp = document.createElementNS("http://www.w3.org/2000/svg", "path");
      tmp.setAttribute("class", "temp"); $("wires").append(tmp);
      const a = outPt(S.modules.find((m) => m.id === src));
      const rect = $("canvas").getBoundingClientRect();
      const move = (ev) => tmp.setAttribute("d", wirePath(a, { x: ev.clientX - rect.left, y: ev.clientY - rect.top }));
      const up = (ev) => {
        document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up);
        tmp.remove();
        const tgt = document.elementFromPoint(ev.clientX, ev.clientY);
        if (tgt && tgt.classList.contains("port") && tgt.dataset.kind === "in")
          send("add_connection", { src, dst: tgt.dataset.id });
      };
      document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
    });
  });
  // drag a MIDI wire from a SEQ midi-out onto a module's midi-in
  document.querySelectorAll("#canvas .port.midi.out").forEach((po) => {
    po.addEventListener("mousedown", (e) => {
      e.preventDefault(); e.stopPropagation();
      const src = po.dataset.id;
      const tmp = document.createElementNS(SVGNS, "path");
      tmp.setAttribute("class", "temp midi"); $("wires").append(tmp);
      const a = outPt(S.modules.find((m) => m.id === src));
      const rect = $("canvas").getBoundingClientRect();
      const move = (ev) => tmp.setAttribute("d", wirePath(a, { x: ev.clientX - rect.left, y: ev.clientY - rect.top }));
      const up = (ev) => {
        document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up);
        tmp.remove();
        const tgt = document.elementFromPoint(ev.clientX, ev.clientY);
        if (tgt && tgt.classList.contains("port") && tgt.dataset.kind === "midi" && tgt.dataset.id !== src)
          send("add_midi_connection", { src, dst: tgt.dataset.id });
      };
      document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
    });
  });
}

// + add module: pick a type. Colour-coded boxes (generator / processor / midi),
// grouped by category, max 4 per row.
const CAT_ORDER = ["gen", "proc", "midi"];
const CAT_LABEL = { gen: "generators", proc: "processors", midi: "midi" };
$("btn-add").onclick = () => {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, "Add module"));
  const shortDesc = (role) => role.split(/[.:—(]/)[0].trim().slice(0, 40);
  const byCat = { gen: [], proc: [], midi: [] };
  Object.keys(S.catalog).forEach((t) => (byCat[moduleCat(t)] || byCat.proc).push(t));
  const scroll = el("div", "add-scroll");
  for (const cat of CAT_ORDER) {
    if (!byCat[cat].length) continue;
    scroll.append(el("div", "add-cat-label cat-" + cat, CAT_LABEL[cat]));
    const grid = el("div", "add-grid");
    byCat[cat].forEach((t) => {
      const b = el("button", "add-box cat-" + cat);
      b.title = S.catalog[t].role;
      b.append(el("span", "add-type", t));
      b.append(el("span", "add-role", shortDesc(S.catalog[t].role)));
      b.onclick = () => {
        const wrap = $("canvas-wrap");
        send("add_module", { type: t, x: wrap.scrollLeft + 120, y: wrap.scrollTop + 100 });
        closeModal();
      };
      grid.append(b);
    });
    scroll.append(grid);
  }
  card.append(scroll);
  const row = el("div", "row"); const cancel = el("button", null, "cancel"); cancel.onclick = closeModal;
  row.append(cancel); card.append(row);
  $("modal").hidden = false;
};

// ---------------------------------------------------------------- SEQ panel
// Small Turing-style randomizer button (regenerates patterns — distinct from the
// param/aesthetic randomizers; no Free/Guided modal).
function seqDice(title, fn) {
  const b = el("button", "sec-dice", "🎲"); b.title = title; b.onclick = fn; return b;
}

function renderSeqDetail(root, m) {
  const sq = (S.seq || {})[m.id] || { tempo: 120, division: 4, running: true, targets: {} };
  const gp = m.global || {};

  const head = el("div", "mod-head");
  head.append(el("h2", null, `SEQ · ${m.id}`));
  head.append(seqDice("Randomize ALL lanes (rhythm/velocity/length + CC patterns)",
    () => send("seq_randomize", { module: m.id })));
  const del = el("button", "danger", "delete");
  del.onclick = () => { if (confirm(`Delete ${m.id}?`)) { send("remove_module", { module: m.id }); sel = null; renderDetail(); } };
  head.append(del);
  root.append(head);
  root.append(el("div", "role", "Turing-machine MIDI sequencer & clock — drag the MIDI-out (right of the node) onto a module's MIDI-in (top)."));

  // clock
  root.append(el("div", "group-title", "clock"));
  const row = el("div", "row");
  row.append(el("span", "dx7-lbl", "tempo"));
  const t = el("input"); t.type = "number"; t.min = 20; t.max = 300;
  t.value = Math.round((gp["seq.tempo"] || { base: sq.tempo }).base); t.style.width = "62px";
  t.onchange = () => send("set_param", { module: m.id, param: "seq.tempo", node: null, value: parseFloat(t.value) });
  row.append(t, el("span", "dx7-lbl", "bpm"));
  const d = el("select"); [1, 2, 3, 4, 6, 8, 12, 16].forEach((v) => { const o = el("option", null, v + "/beat"); o.value = v; d.append(o); });
  d.value = Math.round(sq.division);
  d.onchange = () => send("seq_clock", { module: m.id, division: parseFloat(d.value) });
  row.append(d);
  const run = el("button", sq.running ? "active" : "", sq.running ? "▶ run" : "■ stop");
  run.onclick = () => { sq.running = !sq.running; send("seq_clock", { module: m.id, running: sq.running }); renderDetail(); };
  row.append(run);
  root.append(row);

  // global modulatable controls (density / mutation) — tempo lives in the clock row
  const gmetas = (S.catalog.SEQ.global_params || []).filter((p) => p.id === "seq.density" || p.id === "seq.mutation");
  renderSection(root, "global", m, gmetas, null);

  // lanes per connected target
  const targets = sq.targets || {};
  if (!Object.keys(targets).length) {
    root.append(el("div", "rand-hint", "no targets connected — wire the MIDI-out to a module."));
  } else {
    for (const [tmid, lanes] of Object.entries(targets)) {
      const tm = S.modules.find((x) => x.id === tmid);
      const th = el("div", "group-title sec");
      th.append(el("span", null, `→ ${tm ? tm.type : tmid} · ${tmid}`));
      th.append(seqDice("Randomize this target's lanes", () => send("seq_randomize", { module: m.id, target: tmid })));
      root.append(th);
      lanes.forEach((lane, idx) => renderSeqLane(root, m.id, tmid, idx, lane, tm));
    }
  }

  // module LFOs — mandatory section; here it modulates the clock (tempo/density/mutation)
  renderModuleLFOs(root, m, S.catalog.SEQ);
}

function renderSeqLane(root, seqId, tmid, idx, lane, targetModule) {
  const SET = (k, v) => send("seq_lane", { module: seqId, target: tmid, index: idx, [k]: v });
  const wrap = el("div", "seq-lane" + (lane.enabled ? "" : " off"));

  // header: lane kind toggle + per-lane randomizer
  const h = el("div", "row seq-lane-head");
  const en = el("button", lane.enabled ? "active" : "", lane.kind === "note" ? "NOTE" : "CC");
  en.title = lane.enabled ? "enabled — click to mute" : "muted — click to enable";
  en.onclick = () => { lane.enabled = !lane.enabled; SET("enabled", lane.enabled); renderDetail(); };
  h.append(en);
  h.append(seqDice("Randomize this lane's pattern", () => send("seq_randomize", { module: seqId, target: tmid, index: idx })));
  wrap.append(h);

  // steps (pattern length) + mutation
  const lr = el("div", "row");
  lr.append(el("span", "dx7-lbl", "steps"));
  const len = el("input"); len.type = "number"; len.min = 1; len.max = 32; len.value = lane.length; len.style.width = "50px";
  len.title = "pattern length (steps) — polymeter"; len.onchange = () => SET("length", parseInt(len.value, 10));
  lr.append(len, el("span", "dx7-lbl", "rnd"));
  lr.append(mkRange(lane.mutation, (v) => SET("mutation", v)));
  wrap.append(lr);

  if (lane.kind === "cc") {
    const r = el("div", "row"); r.append(el("span", "dx7-lbl", "target"));
    const sel = el("select");
    const cat = targetModule ? S.catalog[targetModule.type] : null;
    const params = cat ? cat.node_params.concat(cat.global_params).filter((p) => p.modulatable) : [];
    const none = el("option", null, "— none —"); none.value = ""; sel.append(none);
    params.forEach((p) => { const o = el("option", null, p.label); o.value = p.id; sel.append(o); });
    sel.value = lane.param || "";
    sel.onchange = () => SET("param", sel.value);
    r.append(sel); wrap.append(r);
  } else {
    // NOTE lane: fixed pitch (root); rhythm + velocity + note-length come from the
    // shift register. Note + density each get their own row (tidy).
    const nr = el("div", "row"); nr.append(el("span", "dx7-lbl", "note"));
    const rt = el("input"); rt.type = "number"; rt.min = 0; rt.max = 127; rt.value = lane.root; rt.style.width = "50px";
    rt.title = "fixed pitch (root) for this target"; rt.onchange = () => SET("root", parseInt(rt.value, 10));
    nr.append(rt); wrap.append(nr);
    const dr = el("div", "row"); dr.append(el("span", "dx7-lbl", "density"));
    dr.append(mkRange(lane.density, (v) => SET("density", v)));
    wrap.append(dr);
    wrap.append(el("div", "rand-hint", "velocity + note-length from the shift register"));
  }
  root.append(wrap);
}

// ---------------------------------------------------------------- detail panel
function renderDetail() {
  const root = $("detail"); root.innerHTML = "";
  // Collapse the left pane when no module is selected — frees the canvas.
  document.querySelector("main").classList.toggle("detail-collapsed", !sel);
  if (!sel) return;
  const m = S.modules.find((x) => x.id === sel);
  if (!m) { sel = null; return; }
  const spec = S.catalog[m.type];

  if (m.type === "SEQ") { renderSeqDetail(root, m); return; }

  const head = el("div", "mod-head");
  head.append(el("h2", null, `${m.type} · ${m.id}`));
  head.append(el("span", "role", spec.role));
  const byp = el("button", m.bypass ? "active" : "", m.bypass ? "bypassed" : "bypass");
  byp.onclick = () => send("bypass", { module: m.id, on: !m.bypass });
  head.append(byp);

  const wetWrap = el("label", null, "wet ");
  const wet = mkRange(m.wet_dry, (v) => send("wet", { module: m.id, value: v }));
  wetWrap.append(wet); head.append(wetWrap);

  // node count (hot edit)
  const nWrap = el("label", null, " nodes ");
  const nIn = el("input"); nIn.type = "number"; nIn.min = 1; nIn.max = spec.max_nodes;
  nIn.value = m.node_count; nIn.style.width = "52px";
  nIn.onchange = () => send("set_nodes", { module: m.id, count: parseInt(nIn.value, 10) });
  nWrap.append(nIn); head.append(nWrap);

  const rnd = el("button", null, "🎲 randomize");
  rnd.title = "Randomize this module (uses the Randomizer amount)";
  rnd.onclick = () => pickStyle((style) => send("randomize", { scope: "module", module: m.id, amount: randAmount(), expert: randExpert(), style }), `Randomize ${m.type}`);
  head.append(rnd);

  const del = el("button", "danger", "delete");
  del.onclick = () => { if (confirm(`Delete ${m.id}?`)) { send("remove_module", { module: m.id }); sel = null; } };
  head.append(del);
  root.append(head);

  if (spec.gestures && spec.gestures.length)
    root.append(el("div", "gestures", "gestures: " + spec.gestures.join(" · ")));

  // DX7 preset browser / bank loader (preset + bankSel are handled here, not as
  // generic global sliders).
  if (m.type === "DX7") renderDX7Panel(root, m);

  // global params (DX7 splits into its own sections; every section has a 🎲)
  if (spec.global_params.length) {
    if (m.type === "DX7") renderDX7Globals(root, m, spec);
    else renderSection(root, "global", m, spec.global_params, null);
  }

  // per-node params with node tabs
  if (spec.node_params.length) {
    root.append(sectionHeader(`node ${selNode}`, m,
      spec.node_params.map((meta) => ({ param: meta.id, node: selNode }))));
    const tabs = el("div", "node-tabs");
    for (let i = 0; i < m.node_count; i++) {
      const b = el("button", i === selNode ? "active" : "", String(i));
      b.onclick = () => { selNode = i; renderDetail(); };
      tabs.append(b);
    }
    root.append(tabs);
    for (const meta of spec.node_params) root.append(mkParam(m, meta, selNode));
  }

  // per-module LFO bank
  renderModuleLFOs(root, m, spec);
}

function slotOf(m, pid, node) {
  return node == null ? m.global[pid] : (m.nodes[node] || {})[pid];
}

// DX7 global-param sections. Each renders as its own titled section with a
// section-level 🎲 (see sectionHeader). mode/preset live in the panel. DX7 is a
// pure drone source — no internal clock/envelope (use ENV/GATE modules downstream).
const DX7_MANUAL_FM = ["dx7.algorithm", "dx7.feedback"];
const DX7_MANUAL_LFO = ["dx7.lfoRate", "dx7.lfoWave", "dx7.lfoPMD", "dx7.lfoAMD", "dx7.lfoPMS"];

// A section header with an optional section-level randomizer. The 🎲 randomizes
// every (param,node) in `specs` at the Randomizer amount, skipping locked params
// (the engine drops locked slots), mirroring the per-module modulation section.
// Section (block) and per-param randomizers are always FREE — Free vs Guided is a
// global / module-wide decision only (an aesthetic can't be expressed by one block).
function sectionHeader(label, m, specs) {
  const t = el("div", "group-title" + (specs && specs.length ? " sec" : ""));
  t.append(el("span", null, label));
  if (specs && specs.length) {
    const dice = el("button", "sec-dice", "🎲");
    dice.title = "Randomize this section (free; locked params kept)";
    dice.onclick = () => specs.forEach((s) =>
      send("randomize", { scope: "param", module: m.id, param: s.param, node: s.node, amount: randAmount(), expert: randExpert() }));
    t.append(dice);
  }
  return t;
}

// Render a titled param section (header + rows). `node` null = global params.
function renderSection(root, label, m, metas, node) {
  if (!metas || !metas.length) return;
  root.append(sectionHeader(label, m, metas.map((meta) => ({ param: meta.id, node: node }))));
  for (const meta of metas) root.append(mkParam(m, meta, node));
}

// DX7 globals split into sections (voice / manual subsections), each with its own
// section randomizer. Manual sections show only in manual mode.
function renderDX7Globals(root, m, spec) {
  const byId = {}; spec.global_params.forEach((p) => (byId[p.id] = p));
  const pick = (ids) => ids.map((id) => byId[id]).filter(Boolean);
  const manual = Math.round((m.global["dx7.mode"] || { base: 0 }).base) === 1;
  renderSection(root, "voice", m, pick(["dx7.transpose"]), null);
  if (manual) {
    renderSection(root, "manual · fm", m, pick(DX7_MANUAL_FM), null);
    renderSection(root, "manual · lfo", m, pick(DX7_MANUAL_LFO), null);
    renderSection(root, "manual · operators", m, spec.global_params.filter((p) => /^dx7\.op\d/.test(p.id)), null);
  }
}

// DX7 control panel: mode (factory/manual), trigger (drone/internal), and — in
// factory mode — a preset stepper over the 16,384 bundled presets.
function renderDX7Panel(root, m) {
  const mode = Math.round((m.global["dx7.mode"] || { base: 0 }).base);          // 0 factory / 1 manual
  const presetMeta = (S.catalog.DX7.global_params || []).find((p) => p.id === "dx7.preset");
  const count = presetMeta ? Math.round(presetMeta.rmax) + 1 : 16384;
  const preset = Math.round((m.global["dx7.preset"] || { base: 0 }).base);
  // Send + optimistically update local state + re-render: a "param" event alone
  // only refreshes slider readouts, so the panel's toggles/layout would otherwise
  // never reflect the change (the bug where clicking did "nothing").
  const setP = (pid, v) => {
    send("set_param", { module: m.id, param: pid, node: null, value: v });
    if (m.global[pid]) m.global[pid].base = v;
    renderDetail();
  };

  root.append(el("div", "group-title", "DX7"));
  const wrap = el("div", "dx7-panel");

  // mode toggle
  const modeRow = el("div", "row");
  modeRow.append(el("span", "dx7-lbl", "mode"));
  ["factory", "manual"].forEach((label, val) => {
    const b = el("button", mode === val ? "active" : "", label);
    b.onclick = () => setP("dx7.mode", val);
    modeRow.append(b);
  });
  wrap.append(modeRow);

  // factory: preset stepper
  if (mode === 0) {
    const setPreset = (v) => setP("dx7.preset", ((v % count) + count) % count);
    const row = el("div", "row dx7-preset");
    const prev = el("button", null, "◀"); prev.onclick = () => setPreset(preset - 1);
    const num = el("input"); num.type = "number"; num.min = 0; num.max = count - 1; num.value = preset; num.style.width = "78px";
    num.onchange = () => setPreset(parseInt(num.value, 10) || 0);
    const next = el("button", null, "▶"); next.onclick = () => setPreset(preset + 1);
    row.append(el("span", "dx7-lbl", "preset"), prev, num, next);
    row.append(el("span", "dx7-name", `${preset} / ${count}`));
    wrap.append(row);
  } else {
    wrap.append(el("div", "rand-hint", "manual mode: shape the voice with the algorithm, operator and LFO controls below."));
  }

  root.append(wrap);
}

function mkParam(m, meta, node) {
  const row = el("div", "param");
  row.dataset.key = `${m.id}|${meta.id}|${node}`;
  row.append(el("label", null, meta.label));
  const slot = slotOf(m, meta.id, node) || { norm: 0, display: "", base: meta.default, locked: false };

  if (meta.curve === "enum" && meta.enum_values) {
    const selEl = el("select");
    meta.enum_values.forEach((v, i) => {
      const o = el("option", null, v); o.value = i; selEl.append(o);
    });
    selEl.value = Math.round(slot.base);
    selEl.onchange = () => send("set_param", { module: m.id, param: meta.id, node, value: parseFloat(selEl.value) });
    row.append(selEl);
    row.append(el("span", "readout", slot.display));
  } else {
    const r = mkRange(slot.norm, (v) => send("set_param_norm", { module: m.id, param: meta.id, node, value: v }));
    row.append(r);
    row.append(el("span", "readout", slot.display));
  }

  const lk = el("button", "lk" + (slot.locked ? " on" : ""));
  lk.innerHTML = slot.locked ? SVG_LOCK_CLOSED : SVG_LOCK_OPEN;
  lk.title = slot.locked ? "Locked — click to unlock" : "Unlocked — click to lock";
  lk.onclick = () => send("set_lock", { module: m.id, param: meta.id, node, locked: !slot.locked });
  row.append(lk);

  const dice = el("button", "dice", "🎲");
  dice.title = "Randomize this parameter (free; uses the randomizer amount)";
  dice.onclick = () => send("randomize", { scope: "param", module: m.id, param: meta.id, node, amount: randAmount(), expert: randExpert() });
  row.append(dice);
  return row;
}

function updateParam(ev) {
  // update the readout + slider in place without a full re-render
  const key = `${ev.id}|${ev.param}|${ev.node}`;
  const row = document.querySelector(`.param[data-key="${CSS.escape(key)}"]`);
  if (!row) return;
  const ro = row.querySelector(".readout");
  if (ro) ro.textContent = ev.display;
}

function renderModuleLFOs(root, m, spec) {
  const allParams = [...spec.global_params, ...spec.node_params].filter((p) => p.modulatable);
  if (!allParams.length) return;
  const bank = m.per_module_lfos || {};
  const enabled = !!m.per_module_lfos_enabled;

  root.append(el("div", "group-title", "module LFOs"));
  const head = el("div", "row");
  const en = el("button", enabled ? "active" : "", enabled ? "LFOs on" : "LFOs off");
  en.onclick = () => send("set_per_module_lfos_enabled", { module: m.id, enabled: !enabled });
  head.append(en);
  const rnd = el("button", null, "🎲 randomize");
  rnd.onclick = () => send("randomize_per_module_lfos", { module: m.id });
  head.append(rnd);
  root.append(head);

  const grid = el("div", "module-lfo-grid");
  for (const meta of allParams) {
    const cfg = bank[meta.id] || { enabled: false, shape: "sine", rate: 0.5, depth: 0.3 };
    const row = el("div", "module-lfo-row" + (cfg.enabled ? " on" : ""));
    row.dataset.key = `${m.id}|${meta.id}`;

    const cb = el("input"); cb.type = "checkbox"; cb.checked = !!cfg.enabled;
    cb.onchange = () => send("set_per_module_lfo", { module: m.id, param: meta.id, enabled: cb.checked });

    const label = el("span", "lfo-label", meta.label);

    const sh = el("select");
    LFO_SHAPES.forEach((s) => { const o = el("option", null, LFO_SHAPE_LABEL[s]); o.value = s; sh.append(o); });
    sh.value = cfg.shape || "sine";
    sh.onchange = () => send("set_per_module_lfo", { module: m.id, param: meta.id, shape: sh.value });

    const rate = el("input"); rate.type = "range"; rate.min = 0; rate.max = 1; rate.step = 0.001;
    rate.value = rateToNorm(cfg.rate || 0.5); rate.title = "rate";
    rate.oninput = () => { const hz = normToRate(parseFloat(rate.value)); send("set_per_module_lfo", { module: m.id, param: meta.id, rate: hz }); };

    const depth = el("input"); depth.type = "range"; depth.min = 0; depth.max = 1; depth.step = 0.001;
    depth.value = Math.min(1, (cfg.depth || 0.3) / 1.5); depth.title = "depth";
    depth.oninput = () => send("set_per_module_lfo", { module: m.id, param: meta.id, depth: parseFloat(depth.value) * 1.5 });

    row.append(cb, label, sh, rate, depth);
    grid.append(row);
  }
  root.append(grid);
}

function mkRange(val, oninput) {
  const r = el("input"); r.type = "range"; r.min = 0; r.max = 1; r.step = 0.001; r.value = val;
  r.oninput = () => oninput(parseFloat(r.value));
  return r;
}

// ---------------------------------------------------------------- meters / macros
function renderMeters(meters) {
  const root = $("meters"); if (!meters.length && root.childElementCount) { /* keep */ }
  if (root.childElementCount !== meters.length) {
    root.innerHTML = "";
    meters.forEach(() => { const b = el("div", "bar"); b.append(el("span")); root.append(b); });
  }
  meters.forEach((v, i) => {
    const span = root.children[i] && root.children[i].firstChild;
    if (span) span.style.width = Math.min(100, Math.max(0, v * 100)) + "%";
  });
}

// ---------------------------------------------------------------- scenes
// A scene is a full performance snapshot (modules + params + LFOs). Click a scene
// to load it, morphing from the current state over the morph-time (seconds). Add
// as many as you like; they are labelled S1, S2, … by order and wrap to the panel.
function morphTime() { const e = $("morph-time"); return e ? parseFloat(e.value) : 0; }

function renderScenes() {
  const root = $("scenes"); root.innerHTML = "";
  (S.scenes || []).forEach((sc, i) => {
    const active = sc.id === S.active_scene;
    const empty = !(sc.snapshot && sc.snapshot.modules && sc.snapshot.modules.length);
    const morphing = morph && morph.id === sc.id;
    const chip = el("div", "scene-chip" + (active ? " active" : "") + (empty ? " empty" : "")
      + (morphing ? " morphing" : ""));
    const tag = el("button", "scene-tag", `S${i + 1}`);
    tag.title = empty
      ? `S${i + 1}: empty slot — select, then 💾 to store the current patch`
      : `Load S${i + 1}${active ? " (running)" : ""} — morph ${morphTime().toFixed(1)} s`;
    tag.onclick = () => send("load_scene", { id: sc.id, morph: morphTime() });
    const del = el("button", "scene-del", "×");
    del.title = `Delete S${i + 1}`;
    del.onclick = (e) => { e.stopPropagation(); send("remove_scene", { id: sc.id }); };
    chip.append(tag, del);
    if (morphing) {                       // live morph progress fill
      const prog = el("div", "scene-prog");
      prog.style.width = Math.round((morph.t || 0) * 100) + "%";
      chip.append(prog);
    }
    root.append(chip);
  });
  if (morph) {                            // global morph status line
    const pct = Math.round((morph.t || 0) * 100);
    root.append(el("div", "morph-status", `morphing → ${pct}%${morph.structural ? " · re-patching" : ""}`));
  }
  if (!(S.scenes || []).length) root.append(el("span", "rand-hint", "no scenes — ＋ to add a slot"));
}

$("btn-add-scene").onclick = () => send("add_scene", {});
$("btn-capture").onclick = () => send("capture_scene", {});
$("morph-time").oninput = () => {
  $("morph-time-val").textContent = morphTime().toFixed(1) + " s";
};

// ---------------------------------------------------------------- modulation
// ---------------------------------------------------------------- LFO bank
const LFO_SHAPES = ["sine", "triangle", "sh"];
const LFO_SHAPE_LABEL = { sine: "sine", triangle: "triangle", sh: "s&h" };
const rateToNorm = (hz) => Math.max(0, Math.min(1, Math.log(hz / 0.02) / Math.log(8 / 0.02)));
const normToRate = (v) => 0.02 * Math.pow(8 / 0.02, v);

function renderLFOs() {
  const root = $("lfos"); root.innerHTML = "";
  const lfos = S.lfos || [];
  if (document.activeElement !== $("lfo-count")) $("lfo-count").value = lfos.length || 8;
  const targets = S.mod_targets || [];
  for (const lfo of lfos) {
    const strip = el("div", "lfo-strip");
    strip.append(el("span", "lfo-name", lfo.id.toUpperCase()));

    const sh = el("select", "lfo-shape");
    LFO_SHAPES.forEach((s) => { const o = el("option", null, LFO_SHAPE_LABEL[s]); o.value = s; sh.append(o); });
    sh.value = lfo.shape;
    sh.onchange = () => send("lfo_set", { id: lfo.id, shape: sh.value });

    const rate = el("input", "lfo-rate"); rate.type = "range"; rate.min = 0; rate.max = 1; rate.step = 0.001;
    rate.value = rateToNorm(lfo.rate); rate.title = "rate";
    const ratev = el("span", "lfo-val", lfo.rate.toFixed(2) + " Hz");
    rate.oninput = () => { const hz = normToRate(parseFloat(rate.value)); ratev.textContent = hz.toFixed(2) + " Hz"; send("lfo_set", { id: lfo.id, rate: hz }); };

    const depth = el("input", "lfo-depth"); depth.type = "range"; depth.min = 0; depth.max = 1; depth.step = 0.001;
    depth.value = Math.min(1, lfo.depth / 1.5); depth.title = "depth";
    depth.oninput = () => send("lfo_set", { id: lfo.id, depth: parseFloat(depth.value) * 1.5 });

    const tgt = el("select", "lfo-target");
    const none = el("option", null, "— off —"); none.value = ""; tgt.append(none);
    targets.forEach((t) => { const o = el("option", null, t.label); o.value = t.value; tgt.append(o); });
    tgt.value = lfo.target || "";
    tgt.onchange = () => send("lfo_set", { id: lfo.id, target: tgt.value });

    strip.append(sh, rate, ratev, depth, tgt);
    root.append(strip);
  }
}

$("lfo-count").onchange = () => send("lfo_count", { count: parseInt($("lfo-count").value, 10) });
$("btn-lfo-rand").onclick = () => send("lfo_randomize");

function renderModSources() {
  const root = $("mod-sources"); root.innerHTML = "";
  for (const s of (S.mod_sources || []).filter((x) => !x.is_lfo)) {
    const row = el("div", "mod-source");
    row.append(el("span", null, `${s.label || s.id} · ${s.type}`));
    const rate = mkRange(Math.min(1, s.rate / 10), () => {});
    rate.disabled = true;
    row.append(el("span", null, `rate ${s.rate}`));
    root.append(row);
  }
}

function renderModRoutes() {
  const root = $("mod-routes"); root.innerHTML = "";
  for (const r of (S.mod_routes || []).filter((x) => !(x.source_id || "").startsWith("lfo"))) {
    const row = el("div", "mod-route");
    row.append(el("span", null, `${r.source_id} → ${r.dest_module_id}.${r.dest_param_id.split(".").pop()} (${r.node_scope}, d=${r.depth})`));
    const x = el("button", "danger", "×");
    x.onclick = () => send("remove_mod_route", { id: r.id });
    row.append(x);
    root.append(row);
  }
}

$("btn-add-src").onclick = () => {
  const type = $("src-type").value;
  const id = "src_" + ((S.mod_sources || []).length + 1);
  send("add_mod_source", { id, type, label: type.split("_")[0] });
};

function rowLabel(label, control) { const r = el("div", "row"); r.append(el("label", null, label), control); return r; }
function closeModal() { $("modal").hidden = true; }

// ---------------------------------------------------------------- randomizer
// The randomizer is the centrepiece: randomize a single parameter, the selected
// module, or the whole patch, with a shared "random amount" (GRM-style: 0 keeps
// results near current, 1 is fully random within each param's policy range).
function randAmount() { return parseFloat($("rand-amt").value); }
function randExpert() { return $("rand-expert").checked; }

$("rand-amt").oninput = () => { $("rand-amt-val").textContent = Math.round(randAmount() * 100) + "%"; };

// Every randomizer first asks Free vs Guided; Guided then picks an artist whose
// aesthetic shapes the generated values (module-aware, backend ./aesthetics.py).
const ARTISTS = [
  { id: "vidna_obmana", name: "Vidna Obmana", desc: "immersive ambient · dense slow drones, spacious & warm" },
  { id: "lustmord", name: "Lustmord", desc: "dark ambient · cavernous, static, sub-bass, vast reverb" },
  { id: "bernard_parmegiani", name: "Bernard Parmegiani", desc: "musique concrète · sculpted, spatial metamorphosis" },
  { id: "ben_frost", name: "Ben Frost", desc: "abrasive · distortion, noise, dramatic contrast" },
  { id: "autechre", name: "Autechre", desc: "algorithmic · fragmented, complex, DSP artefacts" },
];
// onPick(style) where style === "free" or an artist id. Two choices only: Free
// (full randomization) and Guided — Guided picks one of the artist aesthetics at
// random behind the scenes and generates immediately (no artist selection).
function pickStyle(onPick, title) {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, title || "Randomize"));
  const box = el("div", "rand-choice");
  const free = el("button"); free.innerHTML = `Free<span class="sub">full randomization</span>`;
  free.onclick = () => { closeModal(); onPick("free"); };
  const guided = el("button"); guided.innerHTML = `Guided<span class="sub">a random established aesthetic, applied instantly</span>`;
  guided.onclick = () => { closeModal(); onPick(ARTISTS[Math.floor(Math.random() * ARTISTS.length)].id); };
  box.append(free, guided);
  card.append(box);
  const row = el("div", "row"); const cancel = el("button", null, "cancel"); cancel.onclick = closeModal;
  row.append(cancel); card.append(row);
  $("modal").hidden = false;
}

// "Randomize" is a 3-scope chooser: Patch (rewire + params), Params (params only),
// Chain (pick modules -> new wiring). Each then runs through the Free/Guided flow.
$("btn-rand-all").onclick = () => openRandomizeMenu();

function openRandomizeMenu() {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, "Randomize"));
  const box = el("div", "rand-choice");
  const opt = (label, sub, fn) => {
    const b = el("button"); b.innerHTML = `${label}<span class="sub">${sub}</span>`;
    b.onclick = fn; box.append(b);
  };
  opt("Patch", "rewire a new graph AND generate all params",
      () => pickStyle((style) => send("random_patch", { amount: randAmount(), style }), "Patch"));
  opt("Params", "generate only the params of the current modules",
      () => pickStyle((style) => send("randomize", { scope: "global", amount: randAmount(), expert: randExpert(), style }), "Params"));
  opt("Chain", "pick modules and generate a new chain",
      () => openChainBuilder());
  card.append(box);
  const row = el("div", "row"); const cancel = el("button", null, "cancel"); cancel.onclick = closeModal;
  row.append(cancel); card.append(row);
  $("modal").hidden = false;
}

function openChainBuilder() {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, "Generate Chain — choose modules"));
  const counts = {};
  const total = el("div", "rand-hint");
  const sum = () => Object.values(counts).reduce((a, b) => a + b, 0);
  const upd = () => { total.textContent = `${sum()} / 24 modules selected (max 5 of each)`; };
  Object.keys(S.catalog).filter((t) => S.catalog[t].is_audio).forEach((t) => {
    counts[t] = 0;
    const r = el("div", "chain-row");
    r.append(el("span", "chain-name", `${t} — ${S.catalog[t].role}`.slice(0, 42)));
    const minus = el("button", null, "–"), val = el("span", "chain-val", "0"), plus = el("button", null, "+");
    minus.onclick = () => { if (counts[t] > 0) { counts[t]--; val.textContent = counts[t]; upd(); } };
    plus.onclick = () => { if (counts[t] < 5 && sum() < 24) { counts[t]++; val.textContent = counts[t]; upd(); } };
    r.append(minus, val, plus); card.append(r);
  });
  card.append(total); upd();
  const row = el("div", "row");
  const cancel = el("button", null, "cancel"); cancel.onclick = closeModal;
  const gen = el("button", "active", "Generate Chain");
  gen.onclick = () => {
    const list = [];
    Object.entries(counts).forEach(([t, n]) => { for (let i = 0; i < n; i++) list.push(t); });
    if (!list.length) { closeModal(); return; }
    // Chain: guided choices are handled per-module (not as a global structure decision).
    pickStyle((style) => send("random_chain", { types: list, style }), "Chain");
  };
  row.append(cancel, gen); card.append(row);
  $("modal").hidden = false;
}
$("btn-rand-module").onclick = () => {
  if (!sel) { alert("Select a module first."); return; }
  pickStyle((style) => send("randomize", { scope: "module", module: sel, amount: randAmount(), expert: randExpert(), style }), "Randomize module");
};

// ---------------------------------------------------------------- patch save / load (modal-style)
async function openSaveModal() {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, "Save performance"));
  const current = S ? S.global.name : "untitled";
  const input = el("input"); input.type = "text"; input.value = current; input.placeholder = "patch name";
  card.append(rowLabel("name", input));
  const list = await (await fetch("/api/patches")).json();
  if (list.length) {
    card.append(el("div", "rand-hint", `Existing patches: ${list.join(", ")}`));
  }
  const actions = el("div", "row");
  const cancel = el("button", null, "cancel"); cancel.onclick = closeModal;
  const save = el("button", "active", "save");
  save.onclick = async () => {
    const name = input.value.trim();
    if (!name) return;
    await fetch("/api/patch/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    closeModal();
  };
  input.onkeydown = (e) => { if (e.key === "Enter") save.onclick(); };
  actions.append(cancel, save); card.append(actions);
  $("modal").hidden = false;
  input.focus(); input.select();
}

async function openLoadModal() {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, "Load performance"));
  const list = await (await fetch("/api/patches")).json();
  if (!list.length) {
    card.append(el("p", "rand-hint", "No saved patches yet."));
  } else {
    const box = el("div", "rand-choice");
    box.style.maxHeight = "60vh";
    box.style.overflowY = "auto";
    list.forEach((name) => {
      const b = el("button");
      b.innerHTML = `${name}<span class="sub">click to load</span>`;
      b.onclick = async () => {
        await fetch("/api/patch/load", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
        closeModal();
      };
      box.append(b);
    });
    card.append(box);
  }
  const actions = el("div", "row");
  const cancel = el("button", null, "cancel"); cancel.onclick = closeModal;
  actions.append(cancel); card.append(actions);
  $("modal").hidden = false;
}

// ---------------------------------------------------------------- transport
// the status pill is a one-click reconnect
$("conn").style.cursor = "pointer";
$("conn").title = "Click to reconnect / rebuild the engine";
$("conn").onclick = () => send("engine_reconnect");
$("btn-save").onclick = openSaveModal;
$("btn-load").onclick = openLoadModal;
$("btn-contrast").onclick = () => {
  const b = document.body;
  b.dataset.contrast = b.dataset.contrast === "high" ? "normal" : "high";
  send("controller", { transport: "ui", selector: "/accessibility/contrast", value: b.dataset.contrast === "high" ? 1 : 0 });
};
// Live audio via HLS (continuous, client-decoupled stream from the SC container).
const HLS_URL = "/hls/atelier.m3u8";
function startAudio() {
  const p = $("player"), btn = $("btn-audio");
  if (window.Hls && Hls.isSupported()) {
    const h = new Hls({ liveSyncDurationCount: 2, lowLatencyMode: true });
    h.loadSource(HLS_URL); h.attachMedia(p); p._hls = h;
  } else {
    p.src = HLS_URL;   // Safari plays HLS natively
  }
  p.play().catch(() => {});
  btn.classList.add("active"); btn.textContent = "❚❚ audio";
}
function stopAudio() {
  const p = $("player"), btn = $("btn-audio");
  p.pause();
  if (p._hls) { p._hls.destroy(); p._hls = null; }
  p.removeAttribute("src"); p.load();
  btn.classList.remove("active"); btn.textContent = "▶ audio";
}
$("btn-audio").onclick = () => {
  $("btn-audio").classList.contains("active") ? stopAudio() : startAudio();
};

// ---------------------------------------------------------------- recording
// Capture the live stream to a CD-quality (44.1 kHz / 16-bit) WAV in the SC
// container; manage/download takes from the recordings modal.
let recTimer = null, recStart = 0;
function paintRecording(st) {
  const btn = $("btn-record"), lbl = btn.querySelector(".rec-label");
  if (st && st.active) {
    btn.classList.add("active");
    recStart = (st.started ? st.started * 1000 : Date.now()) - (st.elapsed || 0) * 1000;
    if (!recTimer) recTimer = setInterval(() => {
      const s = Math.max(0, Math.floor((Date.now() - recStart) / 1000));
      lbl.textContent = `${String((s / 60) | 0).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    }, 500);
  } else {
    btn.classList.remove("active");
    if (recTimer) { clearInterval(recTimer); recTimer = null; }
    lbl.textContent = "record";
  }
}
$("btn-record").onclick = () => {
  send($("btn-record").classList.contains("active") ? "record_stop" : "record_start");
};

async function openRecordingsModal() {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, "Recordings"));
  const data = await (await fetch("/api/recordings")).json();
  const list = data.recordings || [];
  if (data.recording && data.recording.active) {
    card.append(el("div", "rand-hint", "● recording in progress — stop it to finalize and list the take."));
  }
  if (!list.length) {
    card.append(el("p", "rand-hint", "No recordings yet. Hit ● record on the toolbar."));
  } else {
    const box = el("div", "rec-list");
    list.forEach((r) => {
      const row = el("div", "rec-item");
      const meta = el("div", "rec-meta");
      meta.append(el("div", "rec-name", prettyRecName(r.name)));
      meta.append(el("div", "rec-sub", `${fmtSize(r.size)} · ${new Date(r.mtime * 1000).toLocaleString()}`));
      const dl = el("a", "rec-dl", "⤓ download");
      dl.href = r.url; dl.setAttribute("download", r.name);
      row.append(meta, dl);
      box.append(row);
    });
    card.append(box);
  }
  const actions = el("div", "row");
  const cancel = el("button", null, "close"); cancel.onclick = closeModal;
  actions.append(cancel); card.append(actions);
  $("modal").hidden = false;
}
function prettyRecName(n) {
  const m = n.match(/^rec_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.wav$/);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : n;
}
function fmtSize(b) {
  if (b >= 1 << 20) return (b / (1 << 20)).toFixed(1) + " MB";
  if (b >= 1 << 10) return (b / (1 << 10)).toFixed(0) + " KB";
  return b + " B";
}
$("btn-recordings").onclick = openRecordingsModal;

// rewire the patch — regenerate connections only; params are left untouched.
$("btn-rewire").onclick = () => {
  if (!S || !S.modules || !S.modules.length) return;
  send("rewire_patch");
};

// clear patch
$("btn-clear").onclick = () => {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, "Clear patch?"));
  card.append(el("p", null, "This removes every module and connection from the canvas."));
  const row = el("div", "row");
  const cancel = el("button", null, "cancel"); cancel.onclick = closeModal;
  const ok = el("button", "danger", "clear");
  ok.onclick = () => { send("clear_patch"); closeModal(); sel = null; renderDetail(); };
  row.append(cancel, ok); card.append(row);
  $("modal").hidden = false;
};

// keyboard shortcuts
document.addEventListener("keydown", (e) => {
  if ($("modal").hidden === false) return;
  const tag = document.activeElement && document.activeElement.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (e.key === "Backspace" && sel) {
    e.preventDefault();
    send("remove_module", { module: sel });
    sel = null;
    renderDetail();
  }
});

connect();

// signed-in user (login wall) — show the username in the topbar.
fetch("/api/me").then((r) => r.ok ? r.json() : null).then((me) => {
  if (me && me.username) $("user").textContent = me.username + (me.is_admin ? " ★" : "");
}).catch(() => {});
