#!/usr/bin/env python3
"""Cell scan that also reads PLMN / TAC / cell identity (used by the viewer's Scan panel, also fine by hand).

usage: scripts/scan_plmn.py [config.yaml] [--band N] [--gain dB] [--json progress.json] [--keep-server]

1. stops nrscope (and the web server too, unless --keep-server): the B210 is exclusive
2. runs nrscan on the band (band_list of the yaml, or --band) with a fixed RX gain (--gain, default: rx_gain of the yaml)
3. merges repeat hits of one cell (same PCI and SCS, neighbouring GSCN), keeps the lowest GSCN, which is the real SSB
4. runs nrscope on each cell until it has decoded SIB1 (or 60 s) and reads PLMN list, TAC, cell identity and the MIB from its log
Progress and results are written to --json while it runs (the viewer polls it). The viewer is not restarted.
"""
import argparse, csv, json, os, re, signal, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument('config', nargs='?', default=os.path.join(ROOT, 'config', 'testbed_n78.yaml'))
ap.add_argument('--band', type=int); ap.add_argument('--gain', type=float)
ap.add_argument('--json'); ap.add_argument('--keep-server', action='store_true')
A = ap.parse_args()
CFG = os.path.abspath(A.config)
NRSCAN = os.path.join(ROOT, 'build/nrscope/src/nrscan'); NRSCOPE = os.path.join(ROOT, 'build/nrscope/src/nrscope')
SCAN_DIR = os.path.join(ROOT, 'run', 'scan'); SIB1_TIMEOUT = 60; MERGE_GSCN = 15; SWEEP_TIMEOUT = 900

cfg_text = open(CFG).read()
if A.band: cfg_text = re.sub(r'(?m)^(\s*band_list:\s*)\[[^\]]*\]', r'\g<1>[%d]' % A.band, cfg_text)
if A.gain is not None: cfg_text = re.sub(r'(?m)^(\s*rx_gain:\s*)[0-9.]+', r'\g<1>%g' % A.gain, cfg_text)
band = A.band or int((re.search(r'(?m)^\s*band_list:\s*\[(\d+)', cfg_text) or [0, 0])[1])
gain = A.gain if A.gain is not None else float((re.search(r'(?m)^\s*rx_gain:\s*([0-9.]+)', cfg_text) or [0, 0])[1])

t_start = time.time()
ST = dict(state='running', pid=os.getpid(), started=t_start, band=band, gain=gain, phase='stopping', scan=None, cell=None, results=[], timing={})
def save():
    if not A.json: return
    ST['updated'] = time.time(); ST['elapsed'] = ST['updated'] - t_start
    tmp = A.json + '.tmp'; json.dump(ST, open(tmp, 'w')); os.replace(tmp, A.json)
def say(*a): print('[%6.1f s]' % (time.time() - t_start), *a, flush=True)

def digits(s): return ''.join(s.replace(' ', '').split(','))
def parse_sib1(text):
    t = re.sub(r'\s+', '', text)
    plmns = list(dict.fromkeys('%s-%s' % (digits(a), digits(b)) for a, b in re.findall(r'"mcc":\[([\d,]+)\],"mnc":\[([\d,]+)\]', t)))   # the log can contain SIB1 several times
    tac = re.search(r'"trackingAreaCode":"([01]+)"', t); ci = re.search(r'"cellIdentity":"([01]+)"', t)
    return plmns, (int(tac.group(1), 2) if tac else None), (int(ci.group(1), 2) if ci else None)
def parse_mib(text):
    m = re.search(r'MIB: sfn=(\d+) ssb_idx=(\d+) hrf=\w+ scs=(\d+) ssb_offset=(\d+) dmrs_typeA_pos=pos(\d) coreset0=(\d+) ss0=(\d+) barred=(\w)', text)
    return dict(ssb_idx=int(m.group(2)), k_ssb=int(m.group(4)), coreset0=int(m.group(6)), ss0=int(m.group(7)), barred=m.group(8) == 'y') if m else None

def sweep_progress(path):
    try: txt = open(path, errors='replace').read()
    except OSError: return
    passes = re.findall(r'Start scaning band (\d+) with scs idx (\d+)\ngscn (\d+) to gscn (\d+)', txt)
    gs = re.findall(r'Start scaning GSCN number (\d+)', txt)
    if passes and gs:
        _, sidx, lo, hi = passes[-1]
        ST['scan'] = dict(pass_no=len(passes), scs_khz=15 << int(sidx), lo=int(lo), hi=int(hi), gscn=int(gs[-1]), hits=txt.count('Cell Found! (maybe'))

def stop_radio():
    subprocess.run([os.path.join(ROOT, 'scripts', 'stop_nrscope.sh' if A.keep_server else 'stop.sh')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)                                          # let the B210 be released

def main():
    save(); say('stopping nrscope'); stop_radio()
    os.makedirs(SCAN_DIR, exist_ok=True)
    open(os.path.join(SCAN_DIR, 'config.yaml'), 'w').write(cfg_text)
    csvp = os.path.join(SCAN_DIR, 'scan.csv'); logp = os.path.join(SCAN_DIR, 'nrscan.log')
    if os.path.exists(csvp): os.remove(csvp)
    ST['phase'] = 'scan'; save(); t0 = time.time(); say('nrscan band n%d, rx_gain %g dB' % (band, gain))
    with open(logp, 'w') as lg:
        pr = subprocess.Popen([NRSCAN], cwd=SCAN_DIR, stdout=lg, stderr=subprocess.STDOUT, start_new_session=True)
        while pr.poll() is None:                           # nrscan aborts when it exits: the return code means nothing
            time.sleep(1); sweep_progress(logp); save()
            if time.time() - t0 > SWEEP_TIMEOUT: os.killpg(pr.pid, signal.SIGKILL); break
    sweep_progress(logp); ST['timing']['sweep_s'] = round(time.time() - t0, 1); say('nrscan sweep done in %.0f s' % ST['timing']['sweep_s'])

    log = open(logp, errors='replace').read(); hit_scs = []; cur = None
    for ln in log.splitlines():
        m = re.match(r'Start scaning band \d+ with scs idx (\d+)', ln)
        if m: cur = int(m.group(1))
        elif ln.startswith('Cell Found!'): hit_scs.append(cur)
    hits = []
    rows = [r for r in (csv.reader(open(csvp)) if os.path.exists(csvp) else []) if r and r[0].strip().isdigit()]
    for k, r in enumerate(rows): hits.append((int(r[0]), float(r[1]), int(r[2]), 15 << (hit_scs[k] if k < len(hit_scs) and hit_scs[k] is not None else 1)))
    hits.sort(); merged = []
    for g, f, p, s in hits:
        same = next((m for m in reversed(merged) if m['pci'] == p and m['scs_khz'] == s and g - m['last'] <= MERGE_GSCN), None)   # a neighbouring hit with another PCI may sit in between
        if same: same['last'] = g; same['hits'] += 1
        else: merged.append(dict(gscn=g, last=g, freq_hz=int(round(f)), pci=p, scs_khz=s, hits=1))
    say('nrscan found %d hit(s) = %d cell(s)' % (len(hits), len(merged)))
    ST['phase'] = 'sib1'; ST['scan'] = None; ST['n_cells'] = len(merged); save()

    t1 = time.time()
    for ci, m in enumerate(merged):
        ST['cell'] = dict(no=ci + 1, of=len(merged), gscn=m['gscn'], pci=m['pci']); save()
        cd = os.path.join(SCAN_DIR, 'cell_gscn%d' % m['gscn']); os.makedirs(cd, exist_ok=True)
        y = re.sub(r'(?m)^(\s*ssb_freq:\s*)\d+', r'\g<1>%d' % m['freq_hz'], cfg_text); y = re.sub(r'(?m)^(\s*pci:\s*)\d+', r'\g<1>%d' % m['pci'], y)
        y = re.sub(r'(?m)^(\s*scs_index:\s*)\d+', r'\g<1>%d' % {15: 0, 30: 1, 60: 2}.get(m['scs_khz'], 1), y)
        open(os.path.join(cd, 'config.yaml'), 'w').write(y)
        lp = os.path.join(cd, 'nrscope.log'); t2 = time.time(); ok = False
        say('nrscope on GSCN %d (%.2f MHz, PCI %d, %d kHz) until SIB1' % (m['gscn'], m['freq_hz'] / 1e6, m['pci'], m['scs_khz']))
        with open(lp, 'w') as lg:
            pr = subprocess.Popen(['stdbuf', '-oL', '-eL', NRSCOPE], cwd=cd, stdout=lg, stderr=subprocess.STDOUT, start_new_session=True)
        while time.time() - t2 < SIB1_TIMEOUT and pr.poll() is None:
            time.sleep(0.5); txt = open(lp, errors='replace').read()
            if 'cellIdentity' in txt and 'cellReservedForOperatorUse' in txt: ok = True; time.sleep(1.5); break
        dt = time.time() - t2
        try: os.killpg(pr.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: pr.wait(timeout=10)
        except subprocess.TimeoutExpired: os.killpg(pr.pid, signal.SIGKILL)
        txt = open(lp, errors='replace').read(); plmns, tac, nci = parse_sib1(txt) if ok else ([], None, None)
        ST['results'].append(dict(gscn=m['gscn'], freq_hz=m['freq_hz'], pci=m['pci'], scs_khz=m['scs_khz'], hits=m['hits'], ok=ok, plmn=plmns, tac=tac, nci=nci,
                                  sib1_s=round(dt, 1) if ok else None, mib=parse_mib(txt)))
        save(); say('   -> %s in %.1f s' % ('SIB1 decoded' if ok else 'SIB1 NOT decoded', dt)); time.sleep(3)
    ST['timing']['sib1_s'] = round(time.time() - t1, 1); ST['timing']['total_s'] = round(time.time() - t_start, 1)
    ST['phase'] = 'done'; ST['state'] = 'done'; ST['cell'] = None; save()

    print('\nGSCN   SSB MHz    SCS  PCI  PLMN(s)        TAC        NCI (36 bit)   SIB1 after')
    for r in ST['results']:
        print('%-6d %-10.2f %-4d %-4d %-14s %-10s %-14s %s' % (r['gscn'], r['freq_hz'] / 1e6, r['scs_khz'], r['pci'], ' '.join(r['plmn']) or '-', r['tac'] if r['tac'] is not None else '-',
              ('0x%X' % r['nci']) if r['nci'] is not None else '-', ('%.1f s' % r['sib1_s']) if r['ok'] else 'not decoded'))
    with open(os.path.join(SCAN_DIR, 'scan_plmn.csv'), 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['GSCN', 'ssb_freq_hz', 'scs_khz', 'PCI', 'PLMN', 'TAC', 'NCI', 'sib1_seconds'])
        for r in ST['results']: w.writerow([r['gscn'], r['freq_hz'], r['scs_khz'], r['pci'], ' '.join(r['plmn']), r['tac'], r['nci'], r['sib1_s']])
    print('\ntime: nrscan %.0f s, SIB1 decode %.0f s (%d cell(s)), total %.0f s' % (ST['timing']['sweep_s'], ST['timing']['sib1_s'], len(ST['results']), ST['timing']['total_s']))

try:
    main()
except BaseException as e:
    ST['state'] = 'error'; ST['error'] = '%s: %s' % (type(e).__name__, e); save(); raise
