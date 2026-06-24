// Wildrider — Schwung overtake runner (P3 default canvas).
//
// The 32 pads are the patch canvas: guided artist-random patches lay their
// modules onto the grid, colour-coded by function. The 8 top encoders are the
// randomized macro knobs. Track buttons regenerate / rewire / delete.
//
//   Pads (short press)   toggle module on/off (off = dim), show its name
//   Track 1              new guided random patch
//   Track 2              rewire current patch
//   Track 3 (hold)+pad   delete the module at that pad
//   Encoders E1..E8      macros M1..M8 (slider shown while turning)
//   Back                 exit the runner
//
// ui.js cannot open sockets, so commands/macros are written to control.json and
// the controller polls them; the screen is drawn from status.json.

import {
    Black, BrightGreen, ForestGreen, AzureBlue, RoyalBlue,
    ElectricViolet, Violet, VividYellow, Mustard, White,
    MoveShift, MoveBack, MoveKnob1, MoveKnob8, MoveMasterTouch,
    MoveMainKnob, MoveMainButton, MoveRow1, MoveRow2, MoveRow3
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
let track3Held = false;
let shiftHeld = false;
let masterTouched = false;     /* volume-knob capacitive touch held */
let row2Down = 0;              /* Track 2 press time, for short/long detect */
let lfoStates = new Array(16).fill(false);   /* 16 step-button global LFOs on/off */
/* ---- CHAINS manual patch builder (Shift + Track 1) ---- */
let chainsMode = false;
let chainsModules = [];        /* selectable module types (from modules.json) */
let chainsIdx = 0;             /* scroll position in the list */
let chainsSel = {};            /* picked: { type: instanceCount } */
let chainsActive = null;       /* a just-selected type awaiting an instance count */
const STEP_BASE = 16;          /* step buttons = MIDI notes 16..31 */
const LFO_ON_COLOR = VividYellow;
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
    host_write_file(CONTROL_FILE, JSON.stringify(doc));
}
function sendCmd(cmd, arg) { seq++; lastCmd = cmd; lastArg = arg; writeControl(); }

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
    if (Array.isArray(s.lfos)) { for (var li = 0; li < 16; li++) lfoStates[li] = !!s.lfos[li]; }
    /* Only mark dirty when the VISIBLE state changed (not cpu/meter jitter), so
     * an idle patch causes ZERO LED/SPI traffic — that traffic XRuns audio. */
    var sig = (ready ? '1' : '0') + '|' + grid.map(function (g) {
        return g.pad + (g.on ? '+' : '-') + g.cat; }).join(',') + '|' + lfoStates.map(function (v) { return v ? '1' : '0'; }).join('');
    if (sig !== lastSig) { lastSig = sig; ledDirty = true; screenDirty = true; }
}

/* ---- LEDs ---- */
function renderLEDs() {
    for (let cell = 0; cell < 32; cell++) {
        const g = cellMap[cell];
        let color = Black;
        if (g) {
            color = g.on ? (CAT_ON[g.cat] || CAT_ON.fx) : OFF_COLOR;
        }
        setLED(PAD_NOTES[cell], color);
    }
    /* 16 step buttons (notes 16..31) = global LFO toggles: lit when enabled. */
    for (let i = 0; i < 16; i++) {
        setLED(STEP_BASE + i, lfoStates[i] ? LFO_ON_COLOR : Black);
    }
    ledDirty = false;
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

function bar(frac) {   /* draw a 0..1 bar */
    if (typeof draw_rect === 'function') draw_rect(6, 34, 116, 14, 1);
    if (typeof fill_rect === 'function') fill_rect(8, 36, Math.max(0, Math.round(frac * 112)), 10, 1);
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
    macroVal = new Array(8).fill(0); seq = 0; track3Held = false; shiftHeld = false;
    masterTouched = false; row2Down = 0;
    chainsMode = false; chainsModules = []; chainsIdx = 0; chainsSel = {}; chainsActive = null;
    lfoStates = new Array(16).fill(false);
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

    /* Volume-knob capacitive touch (note 8, on=127 / off<=63) -> modifier. */
    if (d1 === MoveMasterTouch && (status === 0x90 || status === 0x80)) {
        masterTouched = (status === 0x90 && d2 >= 64);   /* used only by the LFO-randomize combo */
        return;
    }

    /* Step buttons (notes 16..31) = the 16 global LFOs. Press toggles on/off;
     * Shift+press re-randomizes that one LFO (keeping its on/off state). */
    if (status === 0x90 && d2 > 0 && d1 >= STEP_BASE && d1 <= STEP_BASE + 15) {
        const i = d1 - STEP_BASE;
        if (shiftHeld && masterTouched && i === 0) {   /* shift + vol-touch + step1 = randomize ALL */
            sendCmd('lforandall', -1);
            showAction('RND ALL LFOS');
        } else if (shiftHeld) {                        /* shift + step N = re-randomize that LFO */
            sendCmd('lforand', i);
            showLfo(i, 'RND');
        } else {
            lfoStates[i] = !lfoStates[i];        /* optimistic */
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
        const g = cellMap[cell];
        if (g === undefined || g === null) return;   /* empty pad */
        if (track3Held) { sendCmd('delete', cell); return; }   /* delete: immediate */
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
        if (d1 === MoveBack && d2 > 0) { if (typeof host_exit_module === 'function') host_exit_module(); return; }
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
        if (d1 === MoveRow3) { track3Held = d2 > 0; return; }
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
            if (delta !== 0) {
                macroVal[i] = clamp01(macroVal[i] + delta * 0.015);
                writeControl();           /* macro change: same seq, new values */
                showMacro(i);
            }
            return;
        }
    }
};

globalThis.onMidiMessageExternal = function (data) {};
