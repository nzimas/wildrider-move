// Atelier control surface client.
// Renders the whole instrument from the server's machine-describable snapshot
// (catalog metadata + live patch/modulation/scene/control state) and drives it
// over a websocket. No module knowledge is hard-coded here.
"use strict";

let S = null;            // latest full snapshot
let sel = null;          // selected module id
let selNode = 0;         // selected node index in the detail panel
let ws = null;

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

async function refresh() { S = await (await fetch("/api/state")).json(); clearPos(); renderAll(); }
function clearPos() { for (const k in pos) delete pos[k]; }

function onEvent(ev) {
  switch (ev.type) {
    case "snapshot": case "reload":
      S = ev.snapshot; clearPos(); renderAll(); break;
    case "engine":
      updateEngine(ev); break;
    case "param":
      updateParam(ev); break;
    case "macro": break;
    default:
      refresh();   // structural change: pull a fresh authoritative snapshot
  }
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
  renderMacros();
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
function wirePath(a, b) {
  const dx = Math.max(40, Math.abs(b.x - a.x) * 0.5);
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
}

function renderCanvas() {
  const cv = $("canvas");
  // remove old nodes (keep the <svg id=wires>)
  cv.querySelectorAll(".node").forEach((n) => n.remove());
  const byId = {};
  for (const m of S.modules) {
    byId[m.id] = m;
    const p = nodePos(m);
    const node = el("div", "node" + (m.id === sel ? " sel" : "") + (m.bypass ? " bypassed" : ""));
    node.style.left = p.x + "px"; node.style.top = p.y + "px"; node.dataset.id = m.id;
    const head = el("div", "nhead");
    head.append(el("span", "ntype", m.type));
    head.append(el("span", "nid", m.id));
    node.append(head);
    const body = el("div", "nbody");
    const r1 = el("div", "nrow"); r1.append(el("span", null, `${m.node_count} node${m.node_count > 1 ? "s" : ""}`));
    r1.append(el("span", null, m.bypass ? "byp" : `${Math.round(m.wet_dry * 100)}% wet`));
    body.append(r1);
    node.append(body);
    if (m.has_input) { const pin = el("div", "port in"); pin.dataset.id = m.id; pin.dataset.kind = "in"; node.append(pin); }
    if (m.has_output) { const po = el("div", "port out"); po.dataset.id = m.id; po.dataset.kind = "out"; node.append(po); }
    head.addEventListener("mousedown", (e) => startDragNode(e, m.id));
    head.addEventListener("click", () => { sel = m.id; selNode = 0; renderDetail(); highlightSel(); });
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
}

function startDragNode(e, id) {
  e.preventDefault();
  const m = S.modules.find((x) => x.id === id);
  const start = nodePos(m), sx = e.clientX, sy = e.clientY;
  const node = [...document.querySelectorAll("#canvas .node")].find((n) => n.dataset.id === id);
  const move = (ev) => {
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
}

// + add module: pick a type
$("btn-add").onclick = () => {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, "Add module"));
  const grid = el("div", "row");
  Object.keys(S.catalog).forEach((t) => {
    const b = el("button", null, t);
    b.title = S.catalog[t].role;
    b.onclick = () => {
      const wrap = $("canvas-wrap");
      send("add_module", { type: t, x: wrap.scrollLeft + 120, y: wrap.scrollTop + 100 });
      closeModal();
    };
    grid.append(b);
  });
  card.append(grid);
  const row = el("div", "row"); const cancel = el("button", null, "cancel"); cancel.onclick = closeModal;
  row.append(cancel); card.append(row);
  $("modal").hidden = false;
};

// ---------------------------------------------------------------- detail panel
function renderDetail() {
  const root = $("detail"); root.innerHTML = "";
  if (!sel) { root.append(el("p", "role", "Select a module on the canvas, or ＋ add one.")); return; }
  const m = S.modules.find((x) => x.id === sel);
  if (!m) { sel = null; return; }
  const spec = S.catalog[m.type];

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
  rnd.onclick = () => send("randomize", { scope: "module", module: m.id, amount: randAmount(), expert: randExpert() });
  head.append(rnd);

  const del = el("button", "danger", "delete");
  del.onclick = () => { if (confirm(`Delete ${m.id}?`)) { send("remove_module", { module: m.id }); sel = null; } };
  head.append(del);
  root.append(head);

  if (spec.gestures && spec.gestures.length)
    root.append(el("div", "gestures", "gestures: " + spec.gestures.join(" · ")));

  // global params
  if (spec.global_params.length) {
    root.append(el("div", "group-title", "global"));
    for (const meta of spec.global_params) root.append(mkParam(m, meta, null));
  }

  // per-node params with node tabs
  if (spec.node_params.length) {
    root.append(el("div", "group-title", `node ${selNode}`));
    const tabs = el("div", "node-tabs");
    for (let i = 0; i < m.node_count; i++) {
      const b = el("button", i === selNode ? "active" : "", String(i));
      b.onclick = () => { selNode = i; renderDetail(); };
      tabs.append(b);
    }
    root.append(tabs);
    for (const meta of spec.node_params) root.append(mkParam(m, meta, selNode));
  }
}

function slotOf(m, pid, node) {
  return node == null ? m.global[pid] : (m.nodes[node] || {})[pid];
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

  const lk = el("button", "lk" + (slot.locked ? " on" : ""), "L");
  lk.title = "Lock: exclude from randomize / scene recall";
  lk.onclick = () => send("set_lock", { module: m.id, param: meta.id, node, locked: !slot.locked });
  row.append(lk);

  const md = el("button", "md", "~");
  md.title = meta.modulatable ? "Modulate this parameter (polyadic)" : "Not modulatable";
  md.disabled = !meta.modulatable;
  md.onclick = () => openRouteModal(m.id, meta.id);
  row.append(md);

  const dice = el("button", "dice", "🎲");
  dice.title = "Randomize this parameter (uses the randomizer amount)";
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

function renderMacros() {
  const root = $("macros"); root.innerHTML = "";
  for (const mac of (S.control.macros || [])) {
    const wrap = el("div", "macro");
    wrap.append(el("label", null, `${mac.name} [${mac.page}]`));
    const r = mkRange(mac.value, (v) => send("macro", { id: mac.id, value: v }));
    wrap.append(r);
    root.append(wrap);
  }
}

// ---------------------------------------------------------------- scenes
function renderScenes() {
  const root = $("scenes"); root.innerHTML = "";
  const a = $("morph-a"), b = $("morph-b");
  a.innerHTML = ""; b.innerHTML = "";
  for (const sc of (S.scenes || [])) {
    const chip = el("div", "scene-chip");
    chip.append(el("span", null, sc.name || sc.id));
    const rc = el("button", null, "recall");
    rc.onclick = () => send("recall_scene", { id: sc.id });
    chip.append(rc);
    root.append(chip);
    for (const selEl of [a, b]) {
      const o = el("option", null, sc.name || sc.id); o.value = sc.id; selEl.append(o);
    }
  }
}

$("btn-capture").onclick = () => {
  const id = "scene_" + ((S.scenes || []).length + 1);
  const name = prompt("Scene name", id);
  if (name) send("capture_scene", { id, name });
};
$("morph-t").oninput = () => {
  const a = $("morph-a").value, b = $("morph-b").value;
  if (a && b) send("morph_scenes", { a, b, t: parseFloat($("morph-t").value) });
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

function openRouteModal(moduleId, paramId) {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, `Modulate ${moduleId}.${paramId.split(".").pop()}`));
  const srcSel = el("select");
  for (const s of (S.mod_sources || [])) { const o = el("option", null, s.label || s.id); o.value = s.id; srcSel.append(o); }
  if (!(S.mod_sources || []).length) { const o = el("option", null, "(add a source first)"); o.value = ""; srcSel.append(o); }
  const scopeSel = el("select");
  ["allDecorrelated", "allSame", "alternating", "selected", "spatiallyWeighted", "randomSubset", "onePerNode"]
    .forEach((v) => { const o = el("option", null, v); o.value = v; scopeSel.append(o); });
  const depth = el("input"); depth.type = "number"; depth.value = 0.5; depth.step = 0.05; depth.min = -2; depth.max = 2;

  card.append(rowLabel("source", srcSel));
  card.append(rowLabel("node scope", scopeSel));
  card.append(rowLabel("depth (-2..2)", depth));
  const actions = el("div", "row");
  const ok = el("button", "active", "connect");
  ok.onclick = () => {
    if (!srcSel.value) { closeModal(); return; }
    send("add_mod_route", {
      id: "route_" + Date.now(), source: srcSel.value, module: moduleId,
      param: paramId, scope: scopeSel.value, depth: parseFloat(depth.value),
    });
    closeModal();
  };
  const cancel = el("button", null, "cancel"); cancel.onclick = closeModal;
  actions.append(cancel, ok); card.append(actions);
  $("modal").hidden = false;
}
function rowLabel(label, control) { const r = el("div", "row"); r.append(el("label", null, label), control); return r; }
function closeModal() { $("modal").hidden = true; }

// ---------------------------------------------------------------- randomizer
// The randomizer is the centrepiece: randomize a single parameter, the selected
// module, or the whole patch, with a shared "random amount" (GRM-style: 0 keeps
// results near current, 1 is fully random within each param's policy range).
function randAmount() { return parseFloat($("rand-amt").value); }
function randExpert() { return $("rand-expert").checked; }

$("rand-amt").oninput = () => { $("rand-amt-val").textContent = Math.round(randAmount() * 100) + "%"; };
// "Randomize" is a 3-scope chooser: Patch (rewire + params), Params (params only),
// Chain (pick modules -> new wiring, params unchanged).
$("btn-rand-all").onclick = () => openRandomizeMenu();

function openRandomizeMenu() {
  const card = $("modal-card"); card.innerHTML = "";
  card.append(el("h3", null, "Randomize"));
  const box = el("div", "rand-choice");
  const opt = (label, sub, fn) => {
    const b = el("button"); b.innerHTML = `${label}<span class="sub">${sub}</span>`;
    b.onclick = fn; box.append(b);
  };
  opt("Patch", "rewire a new graph AND randomize all params",
      () => { send("random_patch", { amount: randAmount() }); closeModal(); });
  opt("Params", "randomize only the params of the current modules",
      () => { send("randomize", { scope: "global", amount: randAmount(), expert: randExpert() }); closeModal(); });
  opt("Chain", "pick modules and generate a new chain (params kept)",
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
  const upd = () => { total.textContent = `${sum()} / 8 modules selected (max 2 of each)`; };
  Object.keys(S.catalog).filter((t) => S.catalog[t].is_audio).forEach((t) => {
    counts[t] = 0;
    const r = el("div", "chain-row");
    r.append(el("span", "chain-name", `${t} — ${S.catalog[t].role}`.slice(0, 42)));
    const minus = el("button", null, "–"), val = el("span", "chain-val", "0"), plus = el("button", null, "+");
    minus.onclick = () => { if (counts[t] > 0) { counts[t]--; val.textContent = counts[t]; upd(); } };
    plus.onclick = () => { if (counts[t] < 2 && sum() < 8) { counts[t]++; val.textContent = counts[t]; upd(); } };
    r.append(minus, val, plus); card.append(r);
  });
  card.append(total); upd();
  const row = el("div", "row");
  const cancel = el("button", null, "cancel"); cancel.onclick = closeModal;
  const gen = el("button", "active", "Generate Chain");
  gen.onclick = () => {
    const list = [];
    Object.entries(counts).forEach(([t, n]) => { for (let i = 0; i < n; i++) list.push(t); });
    if (list.length) send("random_chain", { types: list });
    closeModal();
  };
  row.append(cancel, gen); card.append(row);
  $("modal").hidden = false;
}
$("btn-rand-module").onclick = () => {
  if (!sel) { alert("Select a module first."); return; }
  send("randomize", { scope: "module", module: sel, amount: randAmount(), expert: randExpert() });
};

// ---------------------------------------------------------------- transport
$("btn-panic").onclick = () => send("panic");
$("btn-reset").onclick = () => send("reset_transport");
$("btn-engine").onclick = () => send("engine_reconnect");
// the status pill is also a one-click reconnect
$("conn").style.cursor = "pointer";
$("conn").title = "Click to reconnect / rebuild the engine";
$("conn").onclick = () => send("engine_reconnect");
$("btn-save").onclick = async () => {
  const name = prompt("Patch name", S ? S.global.name : "untitled");
  if (name) await fetch("/api/patch/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
};
$("btn-load").onclick = async () => {
  const list = await (await fetch("/api/patches")).json();
  const name = prompt("Load which patch?\n" + list.join("\n"), list[0] || "");
  if (name) await fetch("/api/patch/load", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
};
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

connect();
