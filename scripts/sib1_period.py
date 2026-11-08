"""Exact SIB1 timing from the decoder: how often does the gNB send SIB1 (SI-RNTI DCI + PDSCH), per slot position / beam?

Needs nrscope started with NRSCOPE_SIB1C=1 (it then decodes SIB1 at every occasion and publishes the topic `sib1c` with the absolute slot):
    NRSCOPE_SIB1C=1 scripts/switch.sh <cell id>      # or NRSCOPE_SIB1C=1 scripts/start.sh <config>
    python3 scripts/sib1_period.py 60                # listen for 60 s
Restart without NRSCOPE_SIB1C afterwards (it adds decoder load). Events with CRC failed are false alarms of the blind search.
"""
import struct, sys, time, json
import zmq, numpy as np
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 100
ctx = zmq.Context(); s = ctx.socket(zmq.SUB); s.connect('tcp://127.0.0.1:5556'); s.setsockopt(zmq.SUBSCRIBE, b'sib1c')
import os
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    cid = open(os.path.join(root, 'run', 'cell.id')).read().strip()
    mp = [c['meta'] for c in json.load(open(os.path.join(root, 'config', 'cells.json'))) if c['id'] == cid][0]
    beams = json.load(open(os.path.join(root, mp))).get('coreset0', {}).get('beams', {})
except Exception:
    beams = {}
poller = zmq.Poller(); poller.register(s, zmq.POLLIN); t0 = time.time(); ev = []
while time.time() - t0 < dur:
    if not dict(poller.poll(500)): continue
    parts = s.recv_multipart(); p = parts[-1]
    magic, ver, flags, abs_slot, sfn, slot_idx = struct.unpack_from('<HBBQHB', p, 0)
    if magic != 0x5052 or ver != 5: continue
    ev.append((time.time() - t0, abs_slot, sfn, slot_idx, flags & 1, (flags >> 1) & 0x3f))
print('listened %.0f s, %d SIB1 PDSCH decode attempts (SI-RNTI DCI found)' % (dur, len(ev)))
if not ev: sys.exit()
a_all = np.array(ev); a = a_all[a_all[:, 4] == 1]
print('CRC ok: %d of %d, modulation bits/symbol seen: %s' % (len(a), len(a_all), sorted(set(int(x) for x in a[:, 5]))))
frame = (a[:, 1] // 20).astype(int); pos = (a[:, 1] % 40).astype(int)
print('\nslot position in 2-frame cycle (abs_slot mod 40) -> count, beam per meta, frame intervals')
for p in sorted(set(pos)):
    f = frame[pos == p]; d = np.diff(f)
    print('  pos %2d (beam %s): %3d events, first SFNs %s, intervals %s' % (p, beams.get(str(p)), len(f), [int(x) % 1024 for x in f[:6]], dict(zip(*[v.tolist() for v in np.unique(d, return_counts=True)])) if len(d) else ''))
f = np.unique(frame); d = np.diff(f)
print('\nall beams together: %d distinct frames, interval histogram (frames): %s' % (len(f), dict(zip(*[v.tolist() for v in np.unique(d, return_counts=True)])) if len(d) else ''))
tt = a[:, 0]; print('wall-clock gaps between decodes (s), first 15:', np.round(np.diff(tt), 2)[:15])
