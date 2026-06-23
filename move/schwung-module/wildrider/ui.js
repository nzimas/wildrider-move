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
    MoveShift, MoveBack, MoveKnob1, MoveKnob8,
    MoveRow1, MoveRow2, MoveRow3
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
    host_write_file(CONTROL_FILE, JSON.stringify(doc));
}
function sendCmd(cmd, arg) { seq++; lastCmd = cmd; lastArg = arg; writeControl(); }

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
    /* Only mark dirty when the VISIBLE state changed (not cpu/meter jitter), so
     * an idle patch causes ZERO LED/SPI traffic — that traffic XRuns audio. */
    var sig = (ready ? '1' : '0') + '|' + grid.map(function (g) {
        return g.pad + (g.on ? '+' : '-') + g.cat; }).join(',');
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
    ledDirty = false;
}

/* ---- screen ----
 * Name overlay is HELD: shown from pad-down until pad-up (overlayUntil = +inf,
 * cleared on release). Macro overlay is timed. */
function showName(g) { overlay = { kind: 'name', type: g.type, cat: g.cat, on: g.on }; overlayUntil = 1e12; screenDirty = true; }
function clearName() { if (overlay && overlay.kind === 'name') { overlay = null; screenDirty = true; } }
function showMacro(i) { overlay = { kind: 'macro', idx: i }; overlayUntil = phase + 30; screenDirty = true; }

function drawScreen() {
    if (typeof clear_screen !== 'function' || typeof print !== 'function') return;
    clear_screen();
    if (overlay && phase < overlayUntil) {
        if (overlay.kind === 'name') {
            print(0, 8, overlay.type, 2);
            print(0, 40, overlay.cat.toUpperCase() + (overlay.on ? '  ON' : '  OFF'), 1);
        } else {
            const i = overlay.idx;
            print(0, 6, 'M' + (i + 1), 2);
            const w = Math.round(macroVal[i] * 112);
            if (typeof draw_rect === 'function') draw_rect(6, 34, 116, 14, 1);
            if (typeof fill_rect === 'function') fill_rect(8, 36, Math.max(0, w), 10, 1);
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
    macroVal = new Array(8).fill(0); seq = 0; track3Held = false;
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

    if (phase - lastStatusAt >= 6) { readStatus(); lastStatusAt = phase; }   /* ~5Hz at 30Hz refresh */
    if (ledDirty) renderLEDs();
    /* Expire a timed (macro) overlay -> revert to the idle screen once. */
    if (overlay && overlay.kind === 'macro' && phase >= overlayUntil) { overlay = null; screenDirty = true; }
    /* Redraw ONLY when something changed (or a macro slider is live), so the
     * SPI display isn't flushed 133x/s — that contention was XRunning audio. */
    if (screenDirty) { drawScreen(); screenDirty = false; }
};

globalThis.onMidiMessageInternal = function (data) {
    const status = data[0] & 0xF0;
    const d1 = data[1];
    const d2 = data[2];

    /* Pad press (note-on, velocity>0) */
    if (status === 0x90 && d2 > 0 && d1 >= 68 && d1 <= 99) {
        const cell = NOTE_TO_CELL[d1];
        const g = cellMap[cell];
        if (g === undefined || g === null) return;   /* empty pad */
        if (track3Held) {
            sendCmd('delete', cell);
        } else {
            g.on = !g.on;                 /* optimistic local feedback */
            ledDirty = true;
            sendCmd('toggle', cell);
            showName(g);                  /* held until pad release */
        }
        return;
    }
    /* Pad release: clear the held module-name overlay. */
    if (status === 0x80 || (status === 0x90 && d2 === 0)) {
        if (d1 >= 68 && d1 <= 99) clearName();
        return;
    }

    if (status === 0xB0) {
        if (d1 === MoveBack && d2 > 0) { if (typeof host_exit_module === 'function') host_exit_module(); return; }
        if (d1 === MoveShift) { return; }
        if (d1 === MoveRow1 && d2 > 0) { macrosSynced = false; sendCmd('newpatch', -1); return; }
        if (d1 === MoveRow2 && d2 > 0) { sendCmd('rewire', -1); return; }
        if (d1 === MoveRow3) { track3Held = d2 > 0; return; }
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
