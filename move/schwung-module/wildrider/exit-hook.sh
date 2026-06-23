#!/bin/sh
# Wildrider overtake exit cleanup — called by the Schwung shim on clean exit.
# Tear down the whole on-device stack and release the shadow-JACK flag.
pkill -f atelier.headless 2>/dev/null
killall -9 sclang   2>/dev/null
killall -9 scsynth  2>/dev/null
killall -9 jackd    2>/dev/null
rm -f /data/UserData/schwung/jack_running
