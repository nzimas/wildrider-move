#!/usr/bin/env python3
"""Minimal OSC client: load wrtone.scsyndef into a live scsynth and play it.
Runs on the Move (python3 present). No deps — hand-rolls OSC packets."""
import socket, struct, sys, time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 57110
DEF = sys.argv[2] if len(sys.argv) > 2 else "/data/UserData/wildrider/share/wrtone.scsyndef"
SECONDS = float(sys.argv[3]) if len(sys.argv) > 3 else 2.5

def pad(b):
    return b + b"\x00" * ((4 - len(b) % 4) % 4 or 0)

def osc_str(s):
    return pad(s.encode() + b"\x00")

def osc_blob(b):
    return struct.pack(">i", len(b)) + pad(b)

def msg(path, *args):
    tags = ","
    payload = b""
    for a in args:
        if isinstance(a, str):
            tags += "s"; payload += osc_str(a)
        elif isinstance(a, int):
            tags += "i"; payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"; payload += struct.pack(">f", a)
        elif isinstance(a, (bytes, bytearray)):
            tags += "b"; payload += osc_blob(bytes(a))
    return osc_str(path) + osc_str(tags) + payload

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
addr = ("127.0.0.1", PORT)

# Load the synthdef from a file the server can read.
sock.sendto(msg("/d_load", DEF), addr)
time.sleep(0.4)
# Start it on the default group; outputs bus 0/1 -> JACK system playback.
sock.sendto(msg("/s_new", "wrtone", 1001, 0, 0, "freq", 440.0, "amp", 0.2), addr)
print(f"playing wrtone on :{PORT} for {SECONDS}s ...")
time.sleep(SECONDS)
sock.sendto(msg("/n_free", 1001), addr)
print("done")
