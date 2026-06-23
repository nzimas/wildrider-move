// Wildrider — Schwung overtake runner.
//
// Launches the on-device Wildrider stack (Qt-less SuperCollider engine +
// headless Python controller) and renders a status screen from the controller's
// status.json. Pad/encoder mapping + the full framebuffer renderer land in P3;
// P2 proves the runner boots the stack from the Move's menu and owns the screen.

var WR = '/data/UserData/wildrider';
var MODULE_DIR = '/data/UserData/schwung/modules/overtake/wildrider';
var HOOKS_DIR = '/data/UserData/schwung/hooks';
var STATUS_FILE = WR + '/share/status.json';

var phase = 0;
var launched = false;
var lastStatus = null;
var lastStatusAt = -100;

function sys(cmd) {
    if (typeof host_system_cmd === 'function') host_system_cmd(cmd);
}

function readStatus() {
    if (typeof host_read_file !== 'function') return null;
    var raw = host_read_file(STATUS_FILE);
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
}

globalThis.init = function () {
    phase = 0;
    launched = false;
    lastStatus = null;
    lastStatusAt = -100;
};

globalThis.tick = function () {
    phase++;

    // Frame 2: install the exit cleanup hook (per-module + global fallback).
    if (phase === 2) {
        sys('mkdir -p ' + HOOKS_DIR);
        sys('cp ' + MODULE_DIR + '/exit-hook.sh ' + HOOKS_DIR + '/overtake-exit-wildrider.sh');
        sys('chmod +x ' + HOOKS_DIR + '/overtake-exit-wildrider.sh');
        sys('cp ' + MODULE_DIR + '/exit-hook.sh ' + HOOKS_DIR + '/overtake-exit.sh');
        sys('chmod +x ' + HOOKS_DIR + '/overtake-exit.sh');
    }

    // Frame 3: launch the engine + controller (non-blocking).
    if (phase === 3) {
        if (typeof clear_screen === 'function') clear_screen();
        if (typeof print === 'function') {
            print(0, 12, 'WILDRIDER', 2);
            print(0, 38, 'starting engine...', 1);
        }
        sys('sh -c "sh ' + WR + '/run-stack.sh &"');
        launched = true;
    }

    if (!launched) return;

    // Refresh status at ~3 Hz (status.json is written ~5 Hz by the controller).
    if (phase - lastStatusAt >= 20) {
        var s = readStatus();
        if (s) lastStatus = s;
        lastStatusAt = phase;
    }

    // Draw the status screen every frame so the overtake display stays ours.
    if (typeof clear_screen === 'function') clear_screen();
    if (typeof print !== 'function') return;

    print(0, 12, 'WILDRIDER', 2);

    if (!lastStatus) {
        print(0, 38, 'booting engine...', 1);
        return;
    }

    var s = lastStatus;
    var ready = s.ready && s.engine;
    print(0, 34, ready ? 'READY' : 'booting...', 1);
    print(0, 46, 'cpu ' + (s.cpu != null ? s.cpu : '?') + '%  nodes ' + (s.nodes != null ? s.nodes : '?'), 1);
    var mtr = (s.meters && s.meters.length) ? s.meters[0] : 0;
    var bars = Math.max(0, Math.min(12, Math.round(mtr * 80)));
    var vu = '';
    for (var i = 0; i < 12; i++) vu += (i < bars) ? '|' : '.';
    print(0, 58, 'out ' + vu, 1);
};

// Pad / encoder handling — P3. Forward to the controller control channel
// (127.0.0.1:57150) once the Move control map is specified.
globalThis.onMidiMessageInternal = function (data) {};
globalThis.onMidiMessageExternal = function (data) {};
