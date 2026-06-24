#!/bin/sh
# Tear the Wildrider stack DOWN completely. Run by the overtake ui.js when the
# user exits via the Back button, so nothing survives into the next session: the
# launcher (run-stack.sh) reuses a still-running controller, which is what made a
# fresh launch come back with the previous session's patch.
pkill -9 -f atelier.headless 2>/dev/null  # the Python controller (holds the patch)
killall -9 sclang scsynth jackd 2>/dev/null
# scsynth's shm segment + the shadow-JACK flag must go, or the next boot can fail
# / mis-detect a live shadow JACK.
rm -f /dev/shm/SuperColliderServer_* 2>/dev/null
rm -f /data/UserData/schwung/jack_running 2>/dev/null
# Drop the control/status hand-off files so a stale grid can't flash on relaunch.
rm -f /data/UserData/wildrider/share/control.json 2>/dev/null
rm -f /data/UserData/wildrider/share/status.json 2>/dev/null
