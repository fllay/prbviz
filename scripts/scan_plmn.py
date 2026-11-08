#!/usr/bin/env python3
"""Cell scan that also reports PLMN / TAC / cell identity.

usage: scripts/scan_plmn.py [config.yaml]        (default config/testbed_n78.yaml; band_list in the yaml selects the band)

1. stops the viewer (the B210 is exclusive) and runs nrscan  -> GSCN, SSB frequency, PCI of every cell found
2. merges repeat hits of one cell (same PCI, neighbouring GSCN) and keeps the lowest GSCN, which is the real SSB
3. runs nrscope on each cell until it has decoded SIB1 (or SIB1_TIMEOUT s) and reads PLMN list, TAC, cell identity from its log
Writes run/scan/scan_plmn.csv and prints a table with the time every phase took. The viewer is not restarted.
"""
import csv, os, re, signal, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(ROOT, 'config', 'testbed_n78.yaml')
NRSCAN = os.path.join(ROOT, 'build/nrscope/src/nrscan'); NRSCOPE = os.path.join(ROOT, 'build/nrscope/src/nrscope')
SCAN_DIR = os.path.join(ROOT, 'run', 'scan'); SIB1_TIMEOUT = 60; MERGE_GSCN = 15
T = {}
def tick(name, t0): T[name] = time.time() - t0; print('[%6.1f s] %s' % (T[name], name), flush=True)

t_all = time.time()
subprocess.run([os.path.join(ROOT, 'scripts', 'stop.sh')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)                                          # let the B210 be released
os.makedirs(SCAN_DIR, exist_ok=True)
open(os.path.join(SCAN_DIR, 'config.yaml'), 'w').write(open(CFG).read())
csvp = os.path.join(SCAN_DIR, 'scan.csv')
if os.path.exists(csvp): os.remove(csvp)

t0 = time.time()
print('nrscan on %s ...' % CFG, flush=True)
with open(os.path.join(SCAN_DIR, 'nrscan.log'), 'w') as lg:
    subprocess.run([NRSCAN], cwd=SCAN_DIR, stdout=lg, stderr=subprocess.STDOUT, timeout=900)   # it aborts when it exits: ignore the return code
tick('nrscan sweep', t0)

cells = []
for r in csv.reader(open(csvp)) if os.path.exists(csvp) else []:
    if r and r[0].strip().isdigit(): cells.append((int(r[0]), float(r[1]), int(r[2])))
cells.sort(); merged = []
for g, f, p in cells:
    if merged and merged[-1]['pci'] == p and g - merged[-1]['last'] <= MERGE_GSCN: merged[-1]['last'] = g; merged[-1]['hits'] += 1
    else: merged.append(dict(gscn=g, last=g, freq=f, pci=p, hits=1))
print('nrscan found %d hit(s) = %d cell(s): %s' % (len(cells), len(merged), ', '.join('GSCN %d PCI %d' % (m['gscn'], m['pci']) for m in merged)), flush=True)

def digits(s): return ''.join(s.replace(' ', '').split(','))
def parse_sib1(text):
    t = re.sub(r'\s+', '', text)
    plmns = ['%s-%s' % (digits(a), digits(b)) for a, b in re.findall(r'"mcc":\[([\d,]+)\],"mnc":\[([\d,]+)\]', t)]
    plmns = list(dict.fromkeys(plmns))                    # the log can contain SIB1 several times
    tac = re.search(r'"trackingAreaCode":"([01]+)"', t); ci = re.search(r'"cellIdentity":"([01]+)"', t)
    return plmns, (int(tac.group(1), 2) if tac else None), (int(ci.group(1), 2) if ci else None)

rows = []
for m in merged:
    cd = os.path.join(SCAN_DIR, 'cell_gscn%d' % m['gscn']); os.makedirs(cd, exist_ok=True)
    y = open(CFG).read()
    y = re.sub(r'(ssb_freq:\s*)\d+', r'\g<1>%d' % round(m['freq']), y); y = re.sub(r'(\n\s*pci:\s*)\d+', r'\g<1>%d' % m['pci'], y)
    open(os.path.join(cd, 'config.yaml'), 'w').write(y)
    logp = os.path.join(cd, 'nrscope.log'); t0 = time.time(); ok = False
    print('nrscope on GSCN %d (%.2f MHz, PCI %d) until SIB1 ...' % (m['gscn'], m['freq'] / 1e6, m['pci']), flush=True)
    with open(logp, 'w') as lg:
        pr = subprocess.Popen(['stdbuf', '-oL', '-eL', NRSCOPE], cwd=cd, stdout=lg, stderr=subprocess.STDOUT, start_new_session=True)
    while time.time() - t0 < SIB1_TIMEOUT and pr.poll() is None:
        time.sleep(0.5)
        txt = open(logp, errors='replace').read()
        if 'cellIdentity' in txt and 'cellReservedForOperatorUse' in txt: ok = True; time.sleep(1.5); break
    dt = time.time() - t0
    try: os.killpg(pr.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    try: pr.wait(timeout=10)
    except subprocess.TimeoutExpired: os.killpg(pr.pid, signal.SIGKILL)
    plmns, tac, ci = parse_sib1(open(logp, errors='replace').read()) if ok else ([], None, None)
    mib = re.search(r'MIB: sfn=\d+ ssb_idx=(\d+).*?coreset0=(\d+) ss0=(\d+)', open(logp, errors='replace').read())
    rows.append(dict(gscn=m['gscn'], freq_mhz=m['freq'] / 1e6, pci=m['pci'], hits=m['hits'], plmn=' '.join(plmns), tac=tac, nci=ci, sib1_s=round(dt, 1) if ok else None, ok=ok))
    print('   -> %s in %.1f s' % ('SIB1 decoded' if ok else 'SIB1 NOT decoded within %d s' % SIB1_TIMEOUT, dt), flush=True)
    time.sleep(3)
tick('total', t_all)

print('\nGSCN   SSB MHz    PCI  PLMN(s)               TAC        NCI (36 bit)   SIB1 after')
for r in rows:
    print('%-6d %-10.2f %-4d %-21s %-10s %-14s %s' % (r['gscn'], r['freq_mhz'], r['pci'], r['plmn'] or '-', r['tac'] if r['tac'] is not None else '-',
          ('0x%X' % r['nci']) if r['nci'] is not None else '-', ('%.1f s' % r['sib1_s']) if r['ok'] else 'not decoded'))
with open(os.path.join(SCAN_DIR, 'scan_plmn.csv'), 'w', newline='') as f:
    w = csv.writer(f); w.writerow(['GSCN', 'ssb_freq_hz', 'PCI', 'PLMN', 'TAC', 'NCI', 'sib1_seconds'])
    for r in rows: w.writerow([r['gscn'], int(round(r['freq_mhz'] * 1e6)), r['pci'], r['plmn'], r['tac'], r['nci'], r['sib1_s']])
print('\ntime: nrscan %.0f s, SIB1 decode %.0f s (%d cell(s)), total %.0f s' % (T.get('nrscan sweep', 0), T['total'] - T.get('nrscan sweep', 0), len(rows), T['total']))
