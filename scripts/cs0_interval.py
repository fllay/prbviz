import json, base64, time, urllib.request, sys
import numpy as np
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 10
req = urllib.request.urlopen('http://127.0.0.1:8080/stream?sym=1', timeout=15)
t0 = time.time(); slots = {}; NP = S = None; align = 0
buf = b''
while time.time() - t0 < DUR:
    line = req.readline()
    if not line.startswith(b'data: '): continue
    m = json.loads(line[6:])
    if 'sym' not in m: continue
    NP = m['np']; S = m.get('nsym', 14); align = m.get('align', 0); lo = m['lo']
    u = np.frombuffer(base64.b64decode(m['sym']), dtype=np.uint8).reshape(m['n'], S, NP)
    for i in range(m['n']): slots[m['slot0'] + i] = u[i]
req.close()
print('captured slots', len(slots), 'NP', NP, 'S', S, 'align', align, 'span', min(slots), max(slots))
def db(q): return q.astype(np.float32) / 1.8 - 100
def pw(q):  # mean linear power -> dB, ignoring missing (q==0)
    v = np.where(q > 0, 10 ** (db(q) / 10), np.nan); return 10 * np.log10(np.nanmean(v))
A = slice(0, 24); B = slice(24, NP)
# per (slot-in-frame, frame parity): excess dB of PRBs 0-23 over PRBs 24..NP-1 in PDCCH (sym 0-1) and PDSCH (sym 2-13) regions
res = {}
frames = {}
for a, q in slots.items():
    sfn = (a + align) // 20; si = (a + align) % 20
    frames.setdefault(sfn, {})[si] = q
rows = []
for sfn in sorted(frames):
    fr = frames[sfn]
    if len(fr) < 20: continue
    for si in range(20):
        q = fr[si]
        if si not in (0, 1, 2): continue
        e_pdcch = pw(q[0:2, A]) - pw(q[0:2, B])
        e_pdsch = pw(q[2:14, A]) - pw(q[2:14, B])
        rows.append((sfn, si, e_pdcch, e_pdsch))
rows = np.array(rows)
print('complete frames', len(set(rows[:, 0])))
for si in (0, 1, 2):
    r = rows[rows[:, 1] == si]
    for par in (0, 1):
        rr = r[(r[:, 0] % 2) == par]
        print('slot %d  SFN parity %d: n=%d  PDCCH-region excess dB median %.1f p90 %.1f | PDSCH-region excess median %.1f p90 %.1f max %.1f' % (si, par, len(rr), np.median(rr[:, 2]), np.percentile(rr[:, 2], 90), np.median(rr[:, 3]), np.percentile(rr[:, 3], 90), rr[:, 3].max()))
np.save('/tmp/cs0_rows.npy', rows)
# detect "SIB1 present" frames: slot 1 PDSCH excess > thr
sel = rows[rows[:, 1] == 1]
thr = (np.median(sel[:, 3]) + sel[:, 3].max()) / 2
hit = sel[sel[:, 3] > max(thr, 6)]
print('threshold %.1f dB -> hits %d of %d frames' % (max(thr, 6), len(hit), len(sel)))
hs = hit[:, 0].astype(int)
print('hit SFNs (first 40):', list(hs[:40]))
d = np.diff(hs)
if len(d): print('interval in frames: min %d max %d, histogram %s' % (d.min(), d.max(), dict(zip(*np.unique(d, return_counts=True)))))
