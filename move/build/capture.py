#!/usr/bin/env python3
"""Capture the live scsynth master output (bus 0) to a WAV via DiskOut, for
click analysis. Usage: capture.py <scsynth_port> <wrcapture.scsyndef> <out.wav> <seconds>"""
import socket, struct, sys, time

PORT = int(sys.argv[1]); DEF = sys.argv[2]; OUT = sys.argv[3]
SECS = float(sys.argv[4]) if len(sys.argv) > 4 else 4.0
BUF = 99; NODE = 9001

def pad(b): return b + b"\x00" * ((4 - len(b) % 4) % 4)
def s_(s): return pad(s.encode() + b"\x00")
def blob(b): return struct.pack(">i", len(b)) + pad(b)
def msg(path, *args):
    tags = ","; pay = b""
    for a in args:
        if isinstance(a, str): tags += "s"; pay += s_(a)
        elif isinstance(a, int): tags += "i"; pay += struct.pack(">i", a)
        elif isinstance(a, float): tags += "f"; pay += struct.pack(">f", a)
        elif isinstance(a, (bytes, bytearray)): tags += "b"; pay += blob(bytes(a))
    return s_(path) + s_(tags) + pay

sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); addr = ("127.0.0.1", PORT)
defbytes = open(DEF, "rb").read()
sk.sendto(msg("/d_recv", defbytes), addr); time.sleep(0.3)
sk.sendto(msg("/b_alloc", BUF, 65536, 2), addr); time.sleep(0.3)
# open the wav for streaming (leaveOpen=1)
sk.sendto(msg("/b_write", BUF, OUT, "wav", "float", 0, 0, 1), addr); time.sleep(0.3)
# capture synth at the TAIL of the root group (after master -> reads final bus 0)
sk.sendto(msg("/s_new", "wrcapture", NODE, 1, 0, "buf", BUF), addr)
print(f"capturing {SECS}s -> {OUT}")
time.sleep(SECS)
sk.sendto(msg("/n_free", NODE), addr); time.sleep(0.2)
sk.sendto(msg("/b_close", BUF), addr); time.sleep(0.4)   # finalize WAV header
sk.sendto(msg("/b_free", BUF), addr)
print("done")
