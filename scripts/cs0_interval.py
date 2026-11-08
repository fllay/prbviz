#!/usr/bin/env python3
"""Measure how often CORESET0 (SIB1 / paging / RAR) is actually used, from the live prbviz stream.

usage: cs0_interval.py [seconds=12] [url=http://127.0.0.1:8080]

Nothing about the period is assumed. The CORESET0 PRB range and the nominal occasion slots come from /meta
(coreset0.prb_lo/prb_hi, slot_idx or cycle_slots). For every nominal occasion the script compares the mean power of the
CORESET0 PRBs with the PRBs next to them in the same slot; an occasion is ACTIVE above 8 dB (same rule as the viewer).
It then prints the active frames and the interval histogram, so a 20 ms or a 160 ms SIB1 period shows up as it is.
"""
import base64, json, sys, time, urllib.request
import numpy as np

dur = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
base = sys.argv[2] if len(sys.argv) > 2 else 'http://127.0.0.1:8080'
THR = 8.0
meta = json.load(urllib.request.urlopen(base + '/meta', timeout=10))
c0 = meta['coreset0']; spf = meta.get('slots_per_frame', 20)
# nominal occasions: slot positions inside a 2-frame cycle (keeps the SFN parity of n41-style cycle_slots) or slots of every frame
cyc = sorted(set(c % (2 * spf) for c in c0['cycle_slots'])) if c0.get('cycle_slots') else None
cand = cyc if cyc else sorted(c0['slot_idx'])
print('CORESET0 PRB %d..%d, nominal slot(s) %s %s, %d slots/frame' % (c0['prb_lo'], c0['prb_hi'] - 1, cand, 'in a 2-frame cycle' if cyc else 'in frame', spf))

slots = {}; align = 0; lo = 0; NP = 0
req = urllib.request.urlopen(base + '/stream', timeout=15); t0 = time.time()
while time.time() - t0 < dur:
    line = req.readline()
    if not line.startswith(b'data: '): continue
    m = json.loads(line[6:]); NP = m['np']; lo = m['lo']; align = m.get('align', 0)
    u = np.frombuffer(base64.b64decode(m['data']), dtype=np.uint8).reshape(m['n'], NP)
    for i in range(m['n']): slots[m['slot0'] + i] = u[i]
req.close()
i0, i1 = max(0, c0['prb_lo'] - lo), min(NP, c0['prb_hi'] - lo)
j0, j1 = i1, NP
if j1 - j0 < 4: j0, j1 = 0, i0
def mean_db(q):
    q = q[q > 0].astype(np.float64)
    return 10 * np.log10(np.mean(10 ** ((q / 1.8 - 100) / 10))) if len(q) else np.nan
rows = []
for a, q in slots.items():
    f, si = divmod(a + align, spf)
    if (((a + align) % (2 * spf)) if cyc else si) in cand: rows.append((f, si, mean_db(q[i0:i1]) - mean_db(q[j0:j1])))
rows = np.array(sorted(rows))
fr_all = len(set(int(r[0]) for r in rows))
print('captured %.0f ms (%d slots); %d nominal occasions' % (len(slots) * 0.5, len(slots), len(rows)))
act = rows[rows[:, 2] > THR]
act_f = sorted(set(int(f) for f in act[:, 0]))
print('active occasions: %d (excess > %.0f dB: min %.1f, median %.1f, max %.1f);  idle occasions: max excess %.1f dB' % (
    len(act), THR, act[:, 2].min() if len(act) else 0, np.median(act[:, 2]) if len(act) else 0, act[:, 2].max() if len(act) else 0,
    rows[rows[:, 2] <= THR][:, 2].max() if (rows[:, 2] <= THR).any() else 0))
if len(act_f) < 3: print('too few active occasions (%d) to estimate a period; median excess of all occasions %.1f dB' % (len(act_f), np.median(rows[:, 2]))); sys.exit(0)
d = np.diff(act_f); h = dict(zip(*[x.tolist() for x in np.unique(d, return_counts=True)]))
med = int(np.median(d))
print('slots in frame with activity:', sorted(set(int(s) for s in act[:, 1])))
print('interval between active frames (frames -> count):', h)
print('=> period about %d frame(s) = %d ms; first active SFNs (mod 1024): %s' % (med, med * 10, [f % 1024 for f in act_f[:8]]))
print('   SFN mod %d values seen: %s' % (med, sorted(set(f % med for f in act_f))) if med > 1 else '   active in every frame')
