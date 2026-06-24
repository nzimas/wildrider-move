#!/bin/sh
# Wildrider overtake exit cleanup — called by the Schwung shim on clean exit.
# Tear down the whole on-device stack and release the shadow-JACK flag.
pkill -9 -f atelier.headless 2>/dev/null
killall -9 sclang   2>/dev/null
killall -9 scsynth  2>/dev/null
killall -9 jackd    2>/dev/null
# scsynth's shared-memory server segment must go too, or the next launch (as the
# same user) can fail in World_New if a stale one is present.
rm -f /dev/shm/SuperColliderServer_* 2>/dev/null
rm -f /data/UserData/schwung/jack_running
# Drop the hand-off files so a stale grid can't flash on relaunch.
rm -f /data/UserData/wildrider/share/control.json /data/UserData/wildrider/share/status.json 2>/dev/null
