#!/usr/bin/env python3
"""Size-rotating log writer.  usage: <command> 2>&1 | rotlog.py <file> [max_mb=20] [keep=5]

Reads stdin and appends it to <file>; when the file reaches max_mb it becomes <file>.1 (older ones shift to .2 ... .<keep>, the oldest is dropped).
A non-empty <file> left by the previous run is rotated first, so every run starts with a fresh file."""
import os, signal, sys

path = sys.argv[1]
max_b = int(float(sys.argv[2]) * 1048576) if len(sys.argv) > 2 else 20 * 1048576
keep = max(1, int(sys.argv[3])) if len(sys.argv) > 3 else 5
signal.signal(signal.SIGHUP, signal.SIG_IGN)


def rotate():
    for i in range(keep - 1, 0, -1):
        if os.path.exists('%s.%d' % (path, i)):
            os.replace('%s.%d' % (path, i), '%s.%d' % (path, i + 1))
    if os.path.exists(path):
        os.replace(path, path + '.1')


if os.path.exists(path) and os.path.getsize(path) > 0:
    rotate()
f = open(path, 'ab')
size = 0
inp = sys.stdin.buffer
while True:
    line = inp.readline()
    if not line:
        break
    f.write(line)
    f.flush()
    size += len(line)
    if size >= max_b:
        f.close()
        rotate()
        f = open(path, 'ab')
        size = 0
f.close()
