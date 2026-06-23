#!/usr/bin/env python3
"""Tiny stdlib OSC sender for poking the Wildrider control channel on the Move.
Usage: wr_send.py <port> <path> [s:str | i:int | f:float] ...
Example: wr_send.py 57150 /wr/param s:dx71 s:amp i:-1 f:1.4"""
import socket, struct, sys

def osc_str(s):
    b = s.encode() + b"\x00"
    return b + b"\x00" * ((4 - len(b) % 4) % 4)

def build(path, args):
    tags = ","; payload = b""
    for a in args:
        t, _, v = a.partition(":")
        if t == "s": tags += "s"; payload += osc_str(v)
        elif t == "i": tags += "i"; payload += struct.pack(">i", int(v))
        elif t == "f": tags += "f"; payload += struct.pack(">f", float(v))
    return osc_str(path) + osc_str(tags) + payload

port = int(sys.argv[1]); path = sys.argv[2]; args = sys.argv[3:]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(build(path, args), ("127.0.0.1", port))
print(f"sent {path} {args} -> :{port}")
