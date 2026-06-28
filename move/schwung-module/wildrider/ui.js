// Wildrider — Schwung overtake runner (P3 default canvas).
//
// The 32 pads are the patch canvas: guided artist-random patches lay their
// modules onto the grid, colour-coded by function. The 8 top encoders are the
// randomized macro knobs. Track buttons regenerate / rewire / delete.
//
//   Pads (short press)   toggle module on/off (off = dim), show its name
//   Track 1              new guided random patch
//   Track 2              rewire current patch
//   Track 3              toggle SCENES view (32 pads = 32 scene slots)
//   (in SCENES) pad      load a stored scene with a 10s morph
//   (in SCENES) shift+pad  save the current performance into that slot
//   Track 4              toggle SAMPLER view (32 pads = 32 sample slots)
//   (in SAMPLER) pad     tap empty=rec the master mix, tap again=stop; tap take=play/stop
//   (in SAMPLER) X+pad   delete that take
//   X (Delete) + pad     delete the module at that pad
//   Encoders E1..E8      macros M1..M8 (slider shown while turning)
//   Back                 exit the runner
//
// ui.js cannot open sockets, so commands/macros are written to control.json and
// the controller polls them; the screen is drawn from status.json.

import {
    Black, BrightGreen, ForestGreen, AzureBlue, RoyalBlue,
    ElectricViolet, Violet, VividYellow, Mustard, White, Red, Purple,
    MoveShift, MoveBack, MoveKnob1, MoveKnob8, MoveKnob1Touch, MoveKnob8Touch, MoveMasterTouch, MoveDelete,
    MovePlay, MoveRec, MoveMainKnob, MoveMainButton, MoveRow1, MoveRow2, MoveRow3, MoveRow4
} from '/data/UserData/move-anything/shared/constants.mjs';
import { setLED, decodeDelta } from '/data/UserData/move-anything/shared/input_filter.mjs';

const WR = '/data/UserData/wildrider';
const MODULE_DIR = '/data/UserData/schwung/modules/overtake/wildrider';
const HOOKS_DIR = '/data/UserData/schwung/hooks';
const STATUS_FILE = WR + '/share/status.json';
const CONTROL_FILE = WR + '/share/control.json';

/* Pad note layout: cell 0 = top-left, row by row. Matches the controller's
 * abstract pad-cell numbering (0-31). */
const PAD_NOTES = [
    92, 93, 94, 95, 96, 97, 98, 99,   /* row 0 (top)    cells 0-7  */
    84, 85, 86, 87, 88, 89, 90, 91,   /* row 1          cells 8-15 */
    76, 77, 78, 79, 80, 81, 82, 83,   /* row 2          cells 16-23 */
    68, 69, 70, 71, 72, 73, 74, 75    /* row 3 (bottom) cells 24-31 */
];
const NOTE_TO_CELL = {};
for (let i = 0; i < 32; i++) NOTE_TO_CELL[PAD_NOTES[i]] = i;

/* Category colours when ON (function colour). When a module is OFF it is shown
 * in WHITE (a clear, category-independent "muted" state). */
const CAT_ON = {
    gen:  BrightGreen,
    fx:   AzureBlue,
    both: ElectricViolet,             /* RINGS / FBANK — generator AND processor */
    midi: VividYellow
};
const OFF_COLOR = White;

let phase = 0;
let launched = false;
let lastStatusAt = -100;
let grid = [];                 /* [{pad,type,cat,on}] from status.json */
let cellMap = {};              /* cell -> module info */
let ready = false;
let cpu = 0, nodes = 0;
let macroVal = new Array(8).fill(0);
let macrosSynced = false;
let seq = 0, lastCmd = '', lastArg = -1;
let deleteHeld = false;        /* the X / Delete key, held = delete-a-pad modifier */
let playHeld = false, recHeld = false;   /* Play/Rec = force a generator on empty-pad add */
let shiftHeld = false;
let masterTouched = false;     /* volume-knob capacitive touch held */
let row2Down = 0;              /* Track 2 press time, for short/long detect */
let lfoStates = new Array(16).fill(false);   /* 16 step-button global LFOs on/off */
let lfoPending = new Array(16).fill(0);      /* hold the optimistic toggle until ~here (ms) so a
                                              * mid-round-trip status read can't snap the LED back */
/* ---- CHAINS manual patch builder (Shift + Track 1) ---- */
let chainsMode = false;
let chainsModules = [];        /* selectable module types (from modules.json) */
let chainsIdx = 0;             /* scroll position in the list */
let chainsSel = {};            /* picked: { type: instanceCount } */
let chainsActive = null;       /* a just-selected type awaiting an instance count */
const STEP_BASE = 16;          /* step buttons = MIDI notes 16..31 */
const LFO_ON_COLOR = VividYellow;
/* ---- SCENES view (Track 3 toggles it) — the 32 pads are 32 scene slots ----
 * Pad press recalls a stored scene with a 10s morph; Shift+pad stores the
 * current performance (patch + LFOs + macros) into that slot. */
let scenesMode = false;
let sceneFilled = new Array(32).fill(false);   /* which slots hold a scene */
let sceneActive = -1;          /* currently loaded scene pad (or -1) */
let sceneMorphTo = -1;         /* morph destination pad while morphing (or -1) */
let lastSceneActive = -1, lastMorphTo = -1;    /* edge-detect for macro re-sync */
const SCENE_FILLED_COLOR = RoyalBlue;
const SCENE_ACTIVE_COLOR = White;
const SCENE_MORPH_COLOR = ElectricViolet;
/* Scene morph-time editor (Shift + Track 3): jog scans 1..99s, jog-click confirms. */
let morphEdit = false;
let morphTime = 10;            /* seconds, 1..99, default 10 */
/* ---- SAMPLER view (Track 4 toggles it) — 32 pads = 32 sample slots ----
 * Short-press empty = record the master mix; press again = stop -> playable.
 * Short-press a filled slot = loop playback; press again = stop. X+pad deletes.
 * LEDs: empty=unlit, recording=red(flashing), filled idle=white, playing=purple. */
let samplerMode = false;
let sampStates = new Array(32).fill('empty');   /* per slot from status.json */
let sampFlashOn = false;       /* red-flash phase for recording slots */
/* CTRL-ALL loop region (knob 1 = start, knob 2 = end), 0..1 of each take. */
let loopStart = 0.0, loopEnd = 1.0;
/* Slot-selection mode (Shift+pad in the sampler view): knob 8 = volume, knob 7 =
 * pan, knobs 1/2 = loop start/end for THIS slot only. Bars show on knob TOUCH. */
let selSlots = [];                            /* selected slots (multi-select), [] = none */
let selPrimary = -1;                          /* slot whose values the bars show */
let selVol = 1.0, selPan = 0.0, selLs = 0.0, selLe = 1.0;   /* working values (applied to all selected) */
let selCut = 1.0, selRes = 0.0, selPit = 0.0;  /* filter cut/res (0..1), pitch (semis) */
let filtCut = 1.0, filtRes = 0.0, filtPit = 0.0;  /* CTRL-ALL working values */
let sampKnobShow = null;                       /* which param bar to draw */
let lastSamParam = null, lastSamValue = 0;     /* last per-slot edit (sent as `samedit`) */
let sampVol = new Array(32).fill(1.0), sampPan = new Array(32).fill(0.0);
let sampLs = new Array(32).fill(0.0), sampLe = new Array(32).fill(1.0);
let sampCut = new Array(32).fill(1.0), sampRes = new Array(32).fill(0.0), sampPit = new Array(32).fill(0.0);
/* Per-slot step-button FX (step 1 = GATE, step 2 = DISTORT). */
let sampGateOn = new Array(32).fill(0), sampDistOn = new Array(32).fill(0);
let pendingSampFx = null, fxN = 0;            /* sampfx event sent in writeControl */
const FX_ON_COLOR = Mustard;
let heldCell = -1, heldStart = 0, heldNameShown = false, heldAdjusted = false;
const LONG_PRESS_MS = 400;     /* >= this = long press (show name, no toggle) */
let levels = {};               /* cell -> module audio level (amp), default 1.0 */
/* Per-module level is set with hold-pad + jog wheel. We latch the target cell so
 * a pad released mid-turn keeps adjusting that module; reset when the jog idles. */
let levelCell = -1, levelAt = 0;
let lastLevel = null;          /* {pad,val} last module-level change, for control.json */
let overlay = null;            /* {kind:'name'|'macro', ...} */
let overlayUntil = -1;
let ledDirty = true;
let lastSig = '';              /* signature of the visible state (gates redraws) */
let screenDirty = true;        /* redraw the screen only when this is set —
                                  drawing every tick (133Hz) floods the SPI
                                  display and XRuns the audio thread. */

function sys(cmd) { if (typeof host_system_cmd === 'function') host_system_cmd(cmd); }
function clamp01(x) { return x < 0 ? 0 : x > 1 ? 1 : x; }

function writeControl() {
    if (typeof host_write_file !== 'function') return;
    const doc = { seq: seq, cmd: lastCmd, arg: lastArg, macros: macroVal };
    if (lastLevel) doc.level = lastLevel;
    doc.looprange = [loopStart, loopEnd];   /* CTRL-ALL loop region (applied on change) */
    doc.filterall = [filtCut, filtRes];     /* CTRL-ALL filter cutoff/res */
    doc.pitchall = filtPit;                 /* CTRL-ALL pitch (semitones) */
    if (selSlots.length > 0 && lastSamParam) doc.samedit = { sels: selSlots, p: lastSamParam, v: lastSamValue };
    if (pendingSampFx) doc.sampfx = pendingSampFx;
    host_write_file(CONTROL_FILE, JSON.stringify(doc));
}
function sendCmd(cmd, arg) { seq++; lastCmd = cmd; lastArg = arg; writeControl(); }
/* addmod carries an optional forced module type (Play/Rec gestures). */
function sendAddmod(cell, mtype) {
    seq++; lastCmd = 'addmod'; lastArg = cell;
    if (typeof host_write_file === 'function') {
        host_write_file(CONTROL_FILE, JSON.stringify(
            { seq: seq, cmd: 'addmod', arg: cell, macros: macroVal, mtype: mtype || '' }));
    }
}

/* ================= CHAINS manual patch builder ================= */
function enterChains() {
    chainsModules = [];
    if (typeof host_read_file === 'function') {
        var raw = host_read_file(WR + '/share/modules.json');
        if (raw) { try { chainsModules = JSON.parse(raw); } catch (e) {} }
    }
    chainsMode = chainsModules.length > 0;
    chainsIdx = 0; chainsSel = {}; chainsActive = null;
    ledDirty = true; screenDirty = true;
}
function exitChains() { chainsMode = false; chainsActive = null; ledDirty = true; screenDirty = true; }
function chainsGenerate() {
    var sel = [];
    for (var t in chainsSel) sel.push([t, chainsSel[t]]);
    seq++; lastCmd = 'chainsgen'; lastArg = -1;
    if (typeof host_write_file === 'function') {
        host_write_file(CONTROL_FILE, JSON.stringify(
            { seq: seq, cmd: 'chainsgen', arg: -1, macros: macroVal, chains: sel }));
    }
    exitChains();
}
/* CHAINS input: jog scroll/select, top pads = instance count, shift+jog = build. */
function handleChains(status, d1, d2) {
    if (status === 0xB0 && d1 === MoveShift) { shiftHeld = d2 > 0; return; }
    if (status === 0xB0 && d1 === MoveBack && d2 > 0) { exitChains(); return; }   /* cancel */
    if (status === 0xB0 && d1 === MoveMainKnob) {            /* jog rotate = scroll */
        var dn = decodeDelta(d2);
        if (dn !== 0) {
            chainsIdx = Math.max(0, Math.min(chainsModules.length - 1, chainsIdx + dn));
            screenDirty = true;
        }
        return;
    }
    if (status === 0xB0 && d1 === MoveMainButton && d2 > 0) {  /* jog press */
        if (shiftHeld) { chainsGenerate(); return; }          /* shift+jog = build patch */
        var cur = chainsModules[chainsIdx];
        if (chainsSel[cur] !== undefined) { delete chainsSel[cur]; chainsActive = null; }  /* deselect */
        else { chainsActive = cur; }                          /* select -> await count */
        screenDirty = true; ledDirty = true;
        return;
    }
    /* top row pad (notes 92..99) = instance count 1..8 for the active module */
    if (status === 0x90 && d2 > 0 && d1 >= 92 && d1 <= 99 && chainsActive) {
        chainsSel[chainsActive] = (d1 - 92) + 1;
        chainsActive = null; screenDirty = true; ledDirty = true;
        return;
    }
}
function renderChainsLEDs() {
    for (var c = 0; c < 32; c++) {
        var color = Black;
        /* while choosing an instance count, light the top row 1..8 as a ruler */
        if (chainsActive && c < 8) color = White;
        setLED(PAD_NOTES[c], color);
    }
    for (var i = 0; i < 16; i++) setLED(STEP_BASE + i, Black);
    ledDirty = false;
}
function drawChains() {
    if (typeof clear_screen !== 'function') return;
    clear_screen();
    var name = chainsModules[chainsIdx] || '';
    var selected = chainsSel[name] !== undefined;
    print(0, 0, (chainsIdx + 1) + '/' + chainsModules.length, 1);
    if (selected) {                       /* negative: white box + black large text */
        fill_rect(0, 12, 128, 26, 1);
        print(4, 16, name, 0);            /* black-on-white (mode 0) */
        print(96, 16, 'x' + chainsSel[name], 0);
    } else {
        print(0, 14, name, 2);            /* large white-on-black */
    }
    if (chainsActive === name) print(0, 44, 'PICK 1-8 (top pads)', 1);
    else if (chainsActive) print(0, 44, 'set ' + chainsActive + ' count', 1);
    else print(0, 44, 'jog=pick  shift+jog=build', 1);
    var n = 0; for (var k in chainsSel) n++;
    print(0, 54, n + ' modules picked', 1);
}

function readStatus() {
    if (typeof host_read_file !== 'function') return;
    const raw = host_read_file(STATUS_FILE);
    if (!raw) return;
    let s;
    try { s = JSON.parse(raw); } catch (e) { return; }
    ready = !!(s.ready && s.engine);
    cpu = s.cpu != null ? s.cpu : 0;
    nodes = s.nodes != null ? s.nodes : 0;
    grid = Array.isArray(s.grid) ? s.grid : [];
    cellMap = {};
    for (const g of grid) cellMap[g.pad] = g;
    /* Sync knob positions to the controller's macro values once per patch. */
    if (!macrosSynced && Array.isArray(s.macros) && s.macros.length) {
        for (let i = 0; i < 8 && i < s.macros.length; i++) macroVal[i] = s.macros[i].val || 0;
        macrosSynced = true;
        screenDirty = true;
    }
    if (Array.isArray(s.lfos)) {
        var nowMs = Date.now();
        for (var li = 0; li < 16; li++) {
            if (nowMs >= lfoPending[li]) lfoStates[li] = !!s.lfos[li];  /* skip while a toggle is in flight */
        }
    }
    /* Scene bank: filled slots + active + morph destination. */
    if (s.scenes) {
        if (Array.isArray(s.scenes.filled)) {
            for (var pi = 0; pi < 32; pi++) sceneFilled[pi] = !!s.scenes.filled[pi];
        }
        sceneActive = (s.scenes.active != null) ? s.scenes.active : -1;
        sceneMorphTo = (s.scenes.morphTo != null) ? s.scenes.morphTo : -1;
        /* Re-sync the 8 knob accumulators to the recalled scene's macros once a
         * morph FINISHES (or an instant load lands) — the controller restores the
         * scene's macro values at that point, so re-read them from status. */
        if ((lastMorphTo >= 0 && sceneMorphTo < 0) ||
            (sceneMorphTo < 0 && sceneActive !== lastSceneActive)) {
            macrosSynced = false;
        }
        lastMorphTo = sceneMorphTo; lastSceneActive = sceneActive;
    }
    /* Sampler slot states + per-slot params. */
    if (s.sampler && Array.isArray(s.sampler.states)) {
        for (var qi = 0; qi < 32; qi++) {
            sampStates[qi] = s.sampler.states[qi] || 'empty';
            if (s.sampler.vol) sampVol[qi] = s.sampler.vol[qi];
            if (s.sampler.pan) sampPan[qi] = s.sampler.pan[qi];
            if (s.sampler.ls) sampLs[qi] = s.sampler.ls[qi];
            if (s.sampler.le) sampLe[qi] = s.sampler.le[qi];
            if (s.sampler.cut) sampCut[qi] = s.sampler.cut[qi];
            if (s.sampler.res) sampRes[qi] = s.sampler.res[qi];
            if (s.sampler.pit) sampPit[qi] = s.sampler.pit[qi];
            if (s.sampler.gate) sampGateOn[qi] = s.sampler.gate[qi];
            if (s.sampler.dist) sampDistOn[qi] = s.sampler.dist[qi];
        }
    }
    /* Only mark dirty when the VISIBLE state changed (not cpu/meter jitter), so
     * an idle patch causes ZERO LED/SPI traffic — that traffic XRuns audio. */
    var sceneSig = scenesMode ? ('S' + sceneActive + '/' + sceneMorphTo + '/' +
        sceneFilled.map(function (v) { return v ? '1' : '0'; }).join('')) : '';
    var sampSig = samplerMode ? ('Z' + selSlots.join('.') + ':' + sampStates.join(',') + '|' + sampGateOn.join('') + sampDistOn.join('')) : '';
    var sig = (ready ? '1' : '0') + '|' + grid.map(function (g) {
        return g.pad + (g.on ? '+' : '-') + g.cat; }).join(',') + '|' + lfoStates.map(function (v) { return v ? '1' : '0'; }).join('') + '|' + sceneSig + '|' + sampSig;
    if (sig !== lastSig) { lastSig = sig; ledDirty = true; screenDirty = true; }
}

/* ---- LEDs ---- */
function renderLEDs() {
    for (let cell = 0; cell < 32; cell++) {
        const g = cellMap[cell];
        let color = Black;                          /* empty pad = UNLIT */
        if (g) color = g.on ? (CAT_ON[g.cat] || CAT_ON.fx) : OFF_COLOR;
        setLED(PAD_NOTES[cell], color);
    }
    /* 16 step buttons (notes 16..31) = global LFO toggles: lit when enabled. */
    for (let i = 0; i < 16; i++) {
        setLED(STEP_BASE + i, lfoStates[i] ? LFO_ON_COLOR : Black);
    }
    ledDirty = false;
}

/* ---- SCENES view LEDs: filled=blue, loaded=white, morph target=violet ---- */
function renderScenesLEDs() {
    for (var c = 0; c < 32; c++) {
        var color = Black;                              /* empty slot = UNLIT */
        if (c === sceneMorphTo) color = SCENE_MORPH_COLOR;       /* morphing toward */
        else if (c === sceneActive) color = SCENE_ACTIVE_COLOR;  /* currently loaded */
        else if (sceneFilled[c]) color = SCENE_FILLED_COLOR;     /* stored scene */
        setLED(PAD_NOTES[c], color);
    }
    /* Keep the LFO row lit in the scenes view too: scenes capture the LFO on/off
     * states, so showing them here makes that visible — they update live as a
     * recalled scene's pattern lands. */
    for (var i = 0; i < 16; i++) setLED(STEP_BASE + i, lfoStates[i] ? LFO_ON_COLOR : Black);
    ledDirty = false;
}
function drawScenes() {
    if (typeof clear_screen !== 'function' || typeof print !== 'function') return;
    clear_screen();
    print(0, 6, 'SCENES', 2);
    var n = 0; for (var i = 0; i < 32; i++) if (sceneFilled[i]) n++;
    if (sceneMorphTo >= 0) {
        print(0, 34, 'morphing -> ' + (sceneMorphTo + 1), 1);
        print(0, 48, n + ' stored  shift+pad=save', 1);
    } else {
        print(0, 34, n + ' stored' + (sceneActive >= 0 ? '  on ' + (sceneActive + 1) : ''), 1);
        print(0, 48, 'pad=load  shift+pad=save', 1);
    }
}

/* ---- Scene morph-time editor (Shift + Track 3): jog scans, jog-click confirms ---- */
function handleMorphEdit(status, d1, d2) {
    if (status === 0xB0 && d1 === MoveShift) { shiftHeld = d2 > 0; return; }  /* keep Shift tracked so its release isn't swallowed */
    if (status === 0xB0 && d1 === MoveMainKnob) {              /* jog rotate = scan 1..99 */
        var dn = decodeDelta(d2);
        if (dn !== 0) { morphTime = Math.max(1, Math.min(99, morphTime + dn)); screenDirty = true; }
        return;
    }
    if (status === 0xB0 && d1 === MoveMainButton && d2 > 0) {  /* jog click = confirm */
        sendCmd('morphtime', morphTime);
        morphEdit = false; screenDirty = true; ledDirty = true; showAction('MORPH ' + morphTime + 's');
        return;
    }
    if (status === 0xB0 && d1 === MoveBack && d2 > 0) {        /* Back = cancel */
        morphEdit = false; screenDirty = true; ledDirty = true; return;
    }
    /* swallow everything else while editing */
}
function drawMorphEdit() {
    if (typeof clear_screen !== 'function' || typeof print !== 'function') return;
    clear_screen();
    print(0, 6, 'MORPH TIME', 2);
    print(0, 34, morphTime + ' sec', 2);
    print(0, 56, 'jog scan   click = ok', 1);
}

/* ---- SAMPLER view LEDs: empty=off, recording=red(flash), filled=white, playing=purple ---- */
function renderSamplerLEDs(flashOn) {
    for (var c = 0; c < 32; c++) {
        var st = sampStates[c], color = Black;
        if (st === 'recording') color = flashOn ? Red : Black;   /* recording wins */
        else if (selSlots.indexOf(c) >= 0) color = VividYellow;  /* selected slot(s) */
        else if (st === 'playing') color = Purple;
        else if (st === 'filled') color = White;
        setLED(PAD_NOTES[c], color);
    }
    /* step buttons = per-slot FX (step1 GATE, step2 DISTORT). Reflect the selected
     * slot's state, or slot 0 as the representative when nothing is selected (ALL). */
    var fxRep = (selPrimary >= 0) ? selPrimary : 0;
    for (var i = 0; i < 16; i++) {
        var on = (i === 0) ? !!sampGateOn[fxRep] : (i === 1) ? !!sampDistOn[fxRep] : false;
        setLED(STEP_BASE + i, on ? FX_ON_COLOR : Black);
    }
    ledDirty = false;
}
function cutHz(n) { return Math.round(20 * Math.pow(900, n)); }   /* normalised -> Hz */
function panLbl(p) { return p === 0 ? 'C' : (p > 0 ? 'R' + Math.round(p * 100) : 'L' + Math.round(-p * 100)); }
function drawSampler() {
    if (typeof clear_screen !== 'function' || typeof print !== 'function') return;
    clear_screen();
    /* A knob is touched/turning -> show that param's bar (slot value, or CTRL-ALL). */
    if (sampKnobShow) {
        var sel = selSlots.length > 0;
        print(0, 6, sel ? (selSlots.length > 1 ? (selSlots.length + ' SLOTS') : ('SLOT ' + (selPrimary + 1))) : 'ALL SLOTS', 2);
        var vol = sel ? selVol : 1, pan = sel ? selPan : 0;
        var ls = sel ? selLs : loopStart, le = sel ? selLe : loopEnd;
        var cut = sel ? selCut : filtCut, res = sel ? selRes : filtRes, pit = sel ? selPit : filtPit;
        if (sampKnobShow === 'vol') { print(0, 24, 'VOLUME', 1); bar(vol); }
        else if (sampKnobShow === 'pan') { print(0, 24, 'PAN ' + panLbl(pan), 1); bbar(pan); }
        else if (sampKnobShow === 'pit') { print(0, 24, 'PITCH ' + (pit > 0 ? '+' : '') + (Math.round(pit * 10) / 10) + ' st', 1); bbar(pit / 24); }
        else if (sampKnobShow === 'cut') { print(0, 24, 'CUTOFF ' + cutHz(cut) + ' Hz', 1); bar(cut); }
        else if (sampKnobShow === 'res') { print(0, 24, 'RESONANCE ' + Math.round(res * 100) + '%', 1); bar(res); }
        else if (sampKnobShow === 'ls') { print(0, 24, 'LOOP START ' + Math.round(ls * 100) + '%', 1); bar(ls); }
        else if (sampKnobShow === 'le') { print(0, 24, 'LOOP END ' + Math.round(le * 100) + '%', 1); bar(le); }
        return;
    }
    print(0, 6, 'SAMPLER', 2);
    var rec = 0, fill = 0, play = 0;
    for (var i = 0; i < 32; i++) {
        var s = sampStates[i];
        if (s === 'recording') rec++; else if (s === 'playing') play++; else if (s === 'filled') fill++;
    }
    if (rec > 0) print(0, 30, 'RECORDING...', 1);
    else print(0, 30, (fill + play) + ' takes   ' + play + ' playing', 1);
    if (selSlots.length > 0) print(0, 44, selSlots.length + ' sel: k8vol k7pan k5pit k3/4filt k1/2loop', 1);
    else print(0, 44, 'CTRL-ALL k1/2loop k3/4filt k5pit  shift+pad', 1);
    print(0, 56, 'tap=rec/play  X+pad=del', 1);
}

/* ---- screen ----
 * Name overlay is HELD: shown from pad-down until pad-up (overlayUntil = +inf,
 * cleared on release). Macro overlay is timed. */
function showName(g) { overlay = { kind: 'name', type: g.type, cat: g.cat, on: g.on }; overlayUntil = 1e12; screenDirty = true; }
function showLevel(g, v) { overlay = { kind: 'level', type: g.type, val: v }; overlayUntil = 1e12; screenDirty = true; }   /* held while pad down */
function clearHeld() { if (overlay && (overlay.kind === 'name' || overlay.kind === 'level')) { overlay = null; screenDirty = true; } }
function showMacro(i) { overlay = { kind: 'macro', idx: i }; overlayUntil = phase + 30; screenDirty = true; }
function showLfo(i, label) { overlay = { kind: 'lfo', idx: i, label: label }; overlayUntil = phase + 24; screenDirty = true; }
function showAction(label) { overlay = { kind: 'action', label: label }; overlayUntil = phase + 24; screenDirty = true; }

function bar(frac) {   /* draw a 0..1 unipolar bar */
    if (typeof draw_rect === 'function') draw_rect(6, 34, 116, 14, 1);
    if (typeof fill_rect === 'function') fill_rect(8, 36, Math.max(0, Math.round(frac * 112)), 10, 1);
}
function bbar(val) {   /* draw a -1..1 bipolar bar, filled from the centre */
    if (typeof draw_rect !== 'function' || typeof fill_rect !== 'function') return;
    draw_rect(6, 34, 116, 14, 1);
    var cx = 64, w = Math.round(Math.abs(val) * 56);
    if (val >= 0) fill_rect(cx, 36, w, 10, 1); else fill_rect(cx - w, 36, w, 10, 1);
    fill_rect(cx, 34, 1, 14, 1);   /* centre tick */
}

function drawScreen() {
    if (typeof clear_screen !== 'function' || typeof print !== 'function') return;
    clear_screen();
    if (overlay && phase < overlayUntil) {
        if (overlay.kind === 'name') {
            print(0, 8, overlay.type, 2);
            print(0, 40, overlay.cat.toUpperCase() + (overlay.on ? '  ON' : '  OFF'), 1);
        } else if (overlay.kind === 'level') {
            print(0, 6, overlay.type, 2);
            print(0, 22, 'LEVEL', 1);
            bar(overlay.val / 2);
        } else if (overlay.kind === 'lfo') {
            print(0, 8, 'LFO ' + (overlay.idx + 1), 2);
            print(0, 40, overlay.label, 1);
        } else if (overlay.kind === 'action') {
            print(0, 24, overlay.label, 2);
        } else {
            const i = overlay.idx;
            print(0, 6, 'M' + (i + 1), 2);
            bar(macroVal[i]);
        }
        return;
    }
    overlay = null;
    print(0, 6, 'WILDRIDER', 2);
    if (!ready) { print(0, 38, 'booting engine...', 1); return; }
    print(0, 34, grid.length + ' modules', 1);
    print(0, 48, 'cpu ' + cpu + '%  nodes ' + nodes, 1);
}

/* ================= host entry points ================= */
globalThis.init = function () {
    // The host ticks ui.js (and flushes the SPI display) at 133Hz by default;
    // that per-tick work on the audio cores preempts scsynth -> JACK XRuns
    // (clicks/pops). 30Hz is plenty for the screen/LED feedback and gives the
    // audio thread far more uninterrupted time.
    if (typeof host_set_refresh_rate === 'function') host_set_refresh_rate(30);
    phase = 0; launched = false; lastStatusAt = -100;
    grid = []; cellMap = {}; ready = false; macrosSynced = false;
    macroVal = new Array(8).fill(0); seq = 0; deleteHeld = false; shiftHeld = false;
    playHeld = false; recHeld = false;
    masterTouched = false; row2Down = 0;
    scenesMode = false; sceneFilled = new Array(32).fill(false);
    sceneActive = -1; sceneMorphTo = -1; lastSceneActive = -1; lastMorphTo = -1;
    morphEdit = false; morphTime = 10;
    samplerMode = false; sampStates = new Array(32).fill('empty'); sampFlashOn = false;
    loopStart = 0.0; loopEnd = 1.0;
    selSlots = []; selPrimary = -1; selVol = 1.0; selPan = 0.0; selLs = 0.0; selLe = 1.0;
    selCut = 1.0; selRes = 0.0; selPit = 0.0; filtCut = 1.0; filtRes = 0.0; filtPit = 0.0;
    sampKnobShow = null; lastSamParam = null; lastSamValue = 0;
    sampVol = new Array(32).fill(1.0); sampPan = new Array(32).fill(0.0);
    sampLs = new Array(32).fill(0.0); sampLe = new Array(32).fill(1.0);
    sampCut = new Array(32).fill(1.0); sampRes = new Array(32).fill(0.0); sampPit = new Array(32).fill(0.0);
    sampGateOn = new Array(32).fill(0); sampDistOn = new Array(32).fill(0); pendingSampFx = null; fxN = 0;
    chainsMode = false; chainsModules = []; chainsIdx = 0; chainsSel = {}; chainsActive = null;
    lfoStates = new Array(16).fill(false); lfoPending = new Array(16).fill(0);
    heldCell = -1; heldStart = 0; heldNameShown = false; heldAdjusted = false;
    levels = {}; lastLevel = null; levelCell = -1; levelAt = 0;
    overlay = null; overlayUntil = -1; ledDirty = true; screenDirty = true;
};

globalThis.tick = function () {
    phase++;

    if (phase === 2) {
        sys('mkdir -p ' + HOOKS_DIR);
        sys('cp ' + MODULE_DIR + '/exit-hook.sh ' + HOOKS_DIR + '/overtake-exit-wildrider.sh');
        sys('chmod +x ' + HOOKS_DIR + '/overtake-exit-wildrider.sh');
        sys('cp ' + MODULE_DIR + '/exit-hook.sh ' + HOOKS_DIR + '/overtake-exit.sh');
        sys('chmod +x ' + HOOKS_DIR + '/overtake-exit.sh');
    }
    if (phase === 3) {
        if (typeof clear_screen === 'function') { clear_screen(); print(0, 12, 'WILDRIDER', 2); print(0, 38, 'starting engine...', 1); }
        sys('sh -c "sh ' + WR + '/run-stack.sh &"');
        launched = true;
    }
    if (!launched) return;

    if (chainsMode) {                       /* CHAINS builder owns the screen + LEDs */
        if (ledDirty) renderChainsLEDs();
        if (screenDirty) { drawChains(); screenDirty = false; }
        return;
    }
    if (phase - lastStatusAt >= 6) { readStatus(); lastStatusAt = phase; }   /* ~5Hz at 30Hz refresh */
    if (morphEdit) {                        /* morph-time editor owns the screen */
        if (screenDirty) { drawMorphEdit(); screenDirty = false; }
        return;
    }
    if (samplerMode) {                      /* SAMPLER view owns the grid + screen */
        var fOn = (Math.floor(phase / 5) % 2) === 0;   /* blink ~3Hz for recording slots */
        if (sampStates.indexOf('recording') >= 0 && fOn !== sampFlashOn) { sampFlashOn = fOn; ledDirty = true; }
        if (ledDirty) renderSamplerLEDs(sampFlashOn);
        if (screenDirty) { drawSampler(); screenDirty = false; }
        return;
    }
    if (scenesMode) {                       /* SCENES view owns the grid + screen */
        if (ledDirty) renderScenesLEDs();
        if (screenDirty) { drawScenes(); screenDirty = false; }
        return;
    }
    /* Long-press crossed the threshold: reveal the module name (no toggle).
     * Skip if the level was already being shown (master knob turned while held). */
    if (heldCell >= 0 && !heldNameShown && !heldAdjusted && (Date.now() - heldStart) >= LONG_PRESS_MS) {
        const g = cellMap[heldCell];
        if (g) { showName(g); heldNameShown = true; }
    }
    /* Release the level-latch once the jog goes idle, so the next hold re-targets. */
    if (levelCell >= 0 && (Date.now() - levelAt) > 350) levelCell = -1;
    if (ledDirty) renderLEDs();
    /* Expire a timed overlay (macro / volume) -> revert to the idle screen once. */
    if (overlay && (overlay.kind === 'macro' || overlay.kind === 'level' || overlay.kind === 'lfo' || overlay.kind === 'action') && phase >= overlayUntil) { overlay = null; screenDirty = true; }
    /* Redraw ONLY when something changed (or a macro slider is live), so the
     * SPI display isn't flushed 133x/s — that contention was XRunning audio. */
    if (screenDirty) { drawScreen(); screenDirty = false; }
};

globalThis.onMidiMessageInternal = function (data) {
    const status = data[0] & 0xF0;
    const d1 = data[1];
    const d2 = data[2];

    if (chainsMode) { handleChains(status, d1, d2); return; }   /* modal: CHAINS builder */
    if (morphEdit) { handleMorphEdit(status, d1, d2); return; }  /* modal: morph-time editor */

    /* Volume-knob capacitive touch (note 8, on=127 / off<=63) -> modifier. */
    if (d1 === MoveMasterTouch && (status === 0x90 || status === 0x80)) {
        masterTouched = (status === 0x90 && d2 >= 64);   /* used only by the LFO-randomize combo */
        return;
    }
    /* Encoder capacitive touch (notes 0..7 = knob 1..8). In the sampler slot view,
     * TOUCHING a control knob shows its bar (not only on rotation). */
    if (d1 >= MoveKnob1Touch && d1 <= MoveKnob8Touch && (status === 0x90 || status === 0x80)) {
        if (samplerMode) {
            var touched = (status === 0x90 && d2 >= 64);
            var ki = d1 - MoveKnob1Touch;    /* 0..7 = knob 1..8 */
            var sel = selSlots.length > 0;
            var which = (ki === 0) ? 'ls' : (ki === 1) ? 'le' : (ki === 2) ? 'cut' : (ki === 3) ? 'res'
                      : (ki === 4) ? 'pit' : (ki === 6 && sel) ? 'pan' : (ki === 7 && sel) ? 'vol' : null;
            if (touched) { if (which) { sampKnobShow = which; screenDirty = true; } }
            else if (which && sampKnobShow === which) { sampKnobShow = null; screenDirty = true; }
        }
        return;
    }

    /* Step buttons (notes 16..31) = the 16 global LFOs. Press toggles on/off;
     * Shift+press re-randomizes that one LFO (keeping its on/off state). */
    if (status === 0x90 && d2 > 0 && d1 >= STEP_BASE && d1 <= STEP_BASE + 15) {
        const i = d1 - STEP_BASE;
        if (samplerMode) {                       /* SAMPLER: step buttons toggle per-slot FX (selected slots, or ALL) */
            var fxName = (i === 0) ? 'gate' : (i === 1) ? 'dist' : null;
            if (fxName) {
                var targets = (selSlots.length > 0) ? selSlots.slice() : [];
                if (targets.length === 0) { for (var z = 0; z < 32; z++) targets.push(z); }   /* no selection -> ALL */
                var rep = (selPrimary >= 0) ? selPrimary : 0;
                var curOn = (fxName === 'gate') ? sampGateOn[rep] : sampDistOn[rep];
                var newOn, doRand;
                if (curOn) { if (shiftHeld) { newOn = 1; doRand = 1; } else { newOn = 0; doRand = 0; } }
                else { newOn = 1; doRand = 1; }   /* toggling ON randomizes the params */
                for (var k = 0; k < targets.length; k++) {     /* optimistic local state */
                    if (fxName === 'gate') sampGateOn[targets[k]] = newOn; else sampDistOn[targets[k]] = newOn;
                }
                fxN++;
                pendingSampFx = { fx: fxName, sels: targets, on: newOn, rand: doRand, n: fxN };
                writeControl();
                showAction(fxName.toUpperCase() + (newOn ? (doRand && curOn ? ' RND' : ' ON') : ' OFF') + (selSlots.length ? '' : ' ALL'));
                ledDirty = true; screenDirty = true;
            }
            return;                              /* step row is FX in the sampler view (no LFO) */
        }
        if (shiftHeld && masterTouched && i === 0) {   /* shift + vol-touch + step1 = randomize ALL */
            sendCmd('lforandall', -1);
            showAction('RND ALL LFOS');
        } else if (shiftHeld) {                        /* shift + step N = re-randomize that LFO */
            sendCmd('lforand', i);
            showLfo(i, 'RND');
        } else {
            lfoStates[i] = !lfoStates[i];        /* optimistic */
            lfoPending[i] = Date.now() + 600;    /* hold it through the round-trip */
            ledDirty = true;
            sendCmd('lfotoggle', i);
            showLfo(i, lfoStates[i] ? 'ON' : 'OFF');
        }
        return;
    }
    if (status === 0x80 && d1 >= STEP_BASE && d1 <= STEP_BASE + 15) return;  /* step release */

    /* Pad DOWN (note-on, velocity>0): start tracking the press. We decide on
     * RELEASE — a short tap toggles on/off; a long hold shows the module name
     * (revealed in tick() once it crosses the threshold) and does NOT toggle. */
    if (status === 0x90 && d2 > 0 && d1 >= 68 && d1 <= 99) {
        const cell = NOTE_TO_CELL[d1];
        if (samplerMode) {                           /* SAMPLER: tap=rec/play/stop, shift=select, X=delete */
            if (shiftHeld) {                         /* Shift+pad toggles this slot in/out of the selection (multi-select) */
                var si = selSlots.indexOf(cell);
                if (si >= 0) { selSlots.splice(si, 1); }      /* deselect */
                else { selSlots.push(cell); }                 /* add to selection */
                selPrimary = (selSlots.length > 0) ? selSlots[selSlots.length - 1] : -1;
                if (selPrimary >= 0) { selVol = sampVol[selPrimary]; selPan = sampPan[selPrimary]; selLs = sampLs[selPrimary]; selLe = sampLe[selPrimary]; selCut = sampCut[selPrimary]; selRes = sampRes[selPrimary]; selPit = sampPit[selPrimary]; }
                lastSamParam = null;                          /* no stale edit applied to a new selection */
                sampKnobShow = null; ledDirty = true; screenDirty = true;
                return;
            }
            if (deleteHeld) { sampStates[cell] = 'empty'; var di = selSlots.indexOf(cell); if (di >= 0) selSlots.splice(di, 1); if (selPrimary === cell) selPrimary = selSlots.length ? selSlots[selSlots.length - 1] : -1; sendCmd('sampdel', cell); }
            else { sendCmd('samppad', cell); }       /* controller resolves rec/play/stop by state */
            ledDirty = true; screenDirty = true;
            return;
        }
        if (scenesMode) {                            /* SCENES: pad=load, shift+pad=store */
            if (shiftHeld) { sceneFilled[cell] = true; sendCmd('storescene', cell); }
            else if (sceneFilled[cell]) { sendCmd('loadscene', cell); }
            ledDirty = true; screenDirty = true;
            return;
        }
        const g = cellMap[cell];
        if (g === undefined || g === null) {         /* empty pad -> grow the patch */
            if (!shiftHeld && !deleteHeld) {
                /* Play/Rec force a specific generator; else random by row. */
                var mt = (playHeld && recHeld) ? 'WAVIARY' : playHeld ? 'RINGS' : recHeld ? 'DX7' : '';
                sendAddmod(cell, mt);
                showAction(mt ? ('ADD ' + mt) : ((Math.floor(cell / 8) % 2 === 0) ? 'ADD GEN' : 'ADD FX'));
            }
            return;
        }
        if (deleteHeld) { sendCmd('delete', cell); showAction('DEL ' + g.type); return; }   /* X key + pad = delete */
        if (shiftHeld) { sendCmd('randmod', cell); showAction('RND ' + g.type); return; }  /* shift+pad = randomize module params */
        heldCell = cell; heldStart = Date.now(); heldNameShown = false; heldAdjusted = false;
        return;
    }
    /* Pad UP: short tap (and no name/level interaction) -> toggle; otherwise the
     * hold was for showing the name / adjusting the level, so don't toggle. */
    if (status === 0x80 || (status === 0x90 && d2 === 0)) {
        if (d1 >= 68 && d1 <= 99) {
            const cell = NOTE_TO_CELL[d1];
            if (heldCell === cell) {
                const shortTap = (Date.now() - heldStart) < LONG_PRESS_MS;
                if (shortTap && !heldNameShown && !heldAdjusted) {
                    const g = cellMap[cell];
                    if (g) { g.on = !g.on; ledDirty = true; sendCmd('toggle', cell); }
                }
                clearHeld();
                heldCell = -1; heldNameShown = false; heldAdjusted = false;
            }
        }
        return;
    }

    if (status === 0xB0) {
        if (d1 === MoveBack && d2 > 0) {
            sys('sh ' + WR + '/stop-stack.sh');   /* kill engine+controller so the next launch is fresh */
            if (typeof host_exit_module === 'function') host_exit_module();
            return;
        }
        if (d1 === MoveShift) { shiftHeld = d2 > 0; return; }
        if (d1 === MoveRow1 && d2 > 0) {
            if (shiftHeld) { enterChains(); return; }          /* shift+Track1 = CHAINS builder */
            macrosSynced = false; levels = {}; lastLevel = null; sendCmd('newpatch', -1); showAction('NEW PATCH'); return;
        }
        /* Track 2: short press = rewire connections; long press = rewire AND
         * re-randomize all module parameters (decide on release). */
        if (d1 === MoveRow2) {
            if (d2 > 0) { row2Down = Date.now(); }
            else if ((Date.now() - row2Down) >= LONG_PRESS_MS) { sendCmd('rewirerand', -1); showAction('REWIRE + RND'); }
            else { sendCmd('rewire', -1); showAction('REWIRE'); }
            return;
        }
        if (d1 === MoveRow3) {                                  /* Track 3 = SCENES view; Shift+Track 3 = morph-time editor */
            if (d2 > 0) {
                if (shiftHeld) { morphEdit = true; screenDirty = true; }
                else { scenesMode = !scenesMode; if (scenesMode) samplerMode = false; ledDirty = true; screenDirty = true; showAction(scenesMode ? 'SCENES' : 'PATCH'); }
            }
            return;
        }
        if (d1 === MoveRow4) {                                  /* Track 4 = toggle SAMPLER view */
            if (d2 > 0) { samplerMode = !samplerMode; if (samplerMode) scenesMode = false; ledDirty = true; screenDirty = true; showAction(samplerMode ? 'SAMPLER' : 'PATCH'); }
            return;
        }
        if (d1 === MoveDelete) { deleteHeld = d2 > 0; return; }   /* X key held = delete modifier */
        if (d1 === MovePlay) { playHeld = d2 > 0; return; }       /* Play+empty pad = add RINGS */
        if (d1 === MoveRec) { recHeld = d2 > 0; return; }         /* Rec+empty pad = add DX7; Play+Rec = WAVIARY */
        /* The master knob (CC 79) is the Move's NATIVE host master volume — the
         * host owns it, so we never touch it (intercepting would fight the host
         * volume). Per-module level lives on the JOG wheel instead. */
        if (d1 === MoveMainKnob) {       /* hold a pad + jog wheel -> that module's level (amp) */
            const delta = decodeDelta(d2);
            if (delta === 0) return;
            if (heldCell >= 0) levelCell = heldCell;     /* (re)latch the target while the pad is held */
            if (levelCell >= 0) {
                const c = levelCell;
                levelAt = Date.now();
                if (levels[c] === undefined) levels[c] = 1.0;
                levels[c] = Math.max(0, Math.min(2, levels[c] + delta * 0.03));
                lastLevel = { pad: c, val: levels[c] };
                heldAdjusted = true;
                writeControl();
                if (cellMap[c]) showLevel(cellMap[c], levels[c]);
            }
            return;
        }
        if (d1 >= MoveKnob1 && d1 <= MoveKnob8) {
            const i = d1 - MoveKnob1;
            const delta = decodeDelta(d2);
            if (delta === 0) return;
            if (samplerMode) {
                var step = 0.0025;            /* fine loop step: ~400 detents across the take */
                if (selSlots.length > 0) {    /* selected (1 or many): per-slot vol/pan/pitch/filter/loop */
                    var pp = null, pv = 0;
                    if (i === 7) { selVol = clamp01(selVol + delta * 0.02); pp = 'vol'; pv = selVol; }
                    else if (i === 6) { selPan = Math.max(-1, Math.min(1, selPan + delta * 0.02)); pp = 'pan'; pv = selPan; }
                    else if (i === 4) { selPit = Math.max(-24, Math.min(24, selPit + delta * 0.5)); pp = 'pit'; pv = selPit; }
                    else if (i === 2) { selCut = clamp01(selCut + delta * 0.01); pp = 'cut'; pv = selCut; }
                    else if (i === 3) { selRes = clamp01(selRes + delta * 0.01); pp = 'res'; pv = selRes; }
                    else if (i === 0) { selLs = clamp01(selLs + delta * step); if (selLs > selLe - 0.01) selLs = Math.max(0, selLe - 0.01); pp = 'ls'; pv = selLs; }
                    else if (i === 1) { selLe = clamp01(selLe + delta * step); if (selLe < selLs + 0.01) selLe = Math.min(1, selLs + 0.01); pp = 'le'; pv = selLe; }
                    else { return; }
                    lastSamParam = pp; lastSamValue = pv; sampKnobShow = pp;
                    /* mirror locally so the bars stay correct for all selected on next select */
                    for (var k = 0; k < selSlots.length; k++) {
                        var sx = selSlots[k];
                        if (pp === 'vol') sampVol[sx] = pv; else if (pp === 'pan') sampPan[sx] = pv;
                        else if (pp === 'pit') sampPit[sx] = pv; else if (pp === 'cut') sampCut[sx] = pv;
                        else if (pp === 'res') sampRes[sx] = pv; else if (pp === 'ls') sampLs[sx] = pv;
                        else if (pp === 'le') sampLe[sx] = pv;
                    }
                    writeControl(); screenDirty = true;
                    return;
                }
                /* CTRL-ALL (no selection): k1/k2 = loop, k3/k4 = filter, k5 = pitch */
                if (i === 0) {
                    loopStart = clamp01(loopStart + delta * step);
                    if (loopStart > loopEnd - 0.01) loopStart = Math.max(0, loopEnd - 0.01);
                    sampKnobShow = 'ls';
                } else if (i === 1) {
                    loopEnd = clamp01(loopEnd + delta * step);
                    if (loopEnd < loopStart + 0.01) loopEnd = Math.min(1, loopStart + 0.01);
                    sampKnobShow = 'le';
                } else if (i === 2) { filtCut = clamp01(filtCut + delta * 0.01); sampKnobShow = 'cut'; }
                else if (i === 3) { filtRes = clamp01(filtRes + delta * 0.01); sampKnobShow = 'res'; }
                else if (i === 4) { filtPit = Math.max(-24, Math.min(24, filtPit + delta * 0.5)); sampKnobShow = 'pit'; }
                else { return; }              /* knobs 6-8 reserved */
                writeControl(); screenDirty = true;
                return;
            }
            macroVal[i] = clamp01(macroVal[i] + delta * 0.015);
            writeControl();                   /* macro change: same seq, new values */
            showMacro(i);
            return;
        }
    }
};

globalThis.onMidiMessageExternal = function (data) {};
