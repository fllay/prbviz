"""Overlay description (meta.json) for a cell found by the viewer's scan, derived from its MIB and SIB1.

The lab cells have a hand-written meta file (TDD pattern, SSB position, CORESET0 occasions, slot alignment). For a scanned cell the same
information is in what the scan already decodes, so build it from there (FR1, SSB and common SCS equal: 15 or 30 kHz):

  SIB1 (nrscope log)   carrierBandwidth / offsetToCarrier, offsetToPointA, ssb-PositionsInBurst, ssb-PeriodicityServingCell,
                       tdd-UL-DL-ConfigurationCommon (pattern1 + pattern2)
  MIB                  k_SSB, controlResourceSetZero, searchSpaceZero, subCarrierSpacingCommon

  SSB position   lowest SSB subcarrier = Point A + (offsetToPointA*12 + k_SSB) subcarriers of 15 kHz (38.211 7.4.3.1), 20 PRB wide
  SSB symbols    Case A / C: {2,8}+14n; Case B (30 kHz FDD): {4,8,16,20}+28n; beam i is the i-th candidate (blocks as listed in the bitmap)
  CORESET0       PRBs / symbols from TS 38.213 tables 13-1..13-4 (same tables as index.html), SIB1 PDCCH occasions from table 13-11:
                 value = O*2^mu + floor(i*M) for every SSB beam i in the bitmap; slot = value mod slots-per-frame, odd frame when value >= slots-per-frame
  alignment      server.py locks nrscope's slot counter to the SSB burst (see Hub._track): period = SSB period, width = slots the burst spans (max 3)
"""
import re

_PERIOD_MS = {'ms0p5': 0.5, 'ms0p625': 0.625, 'ms1': 1, 'ms1p25': 1.25, 'ms2': 2, 'ms2p5': 2.5, 'ms3': 3, 'ms4': 4, 'ms5': 5, 'ms10': 10}

# [nprb, nsym, offset_rb] per controlResourceSetZero, key 'SSB scs/PDCCH scs' (same as CS0_TAB in index.html)
CS0_TAB = {
    '15/15': [[24, 2, 0], [24, 2, 2], [24, 2, 4], [24, 3, 0], [24, 3, 2], [24, 3, 4], [48, 1, 12], [48, 1, 16], [48, 2, 12], [48, 2, 16], [48, 3, 12], [48, 3, 16], [96, 1, 38], [96, 2, 38], [96, 3, 38], None],
    '15/30': [[24, 2, 5], [24, 2, 6], [24, 2, 7], [24, 2, 8], [24, 3, 5], [24, 3, 6], [24, 3, 7], [24, 3, 8], [48, 1, 18], [48, 1, 20], [48, 2, 18], [48, 2, 20], [48, 3, 18], [48, 3, 20], None, None],
    '30/15': [[48, 1, 2], [48, 1, 6], [48, 2, 2], [48, 2, 6], [48, 3, 2], [48, 3, 6], [96, 1, 28], [96, 2, 28], [96, 3, 28]] + [None] * 7,
    '30/30': [[24, 2, 0], [24, 2, 1], [24, 2, 2], [24, 2, 3], [24, 2, 4], [24, 3, 0], [24, 3, 1], [24, 3, 2], [24, 3, 3], [24, 3, 4], [48, 1, 12], [48, 1, 14], [48, 1, 16], [48, 2, 12], [48, 2, 14], [48, 2, 16]],
}
# searchSpaceZero -> [O, sets per slot, M, first symbol]  (table 13-11, FR1, multiplexing pattern 1)
CS0_SS0 = [[0, 1, 1, 0], [0, 2, .5, 0], [2, 1, 1, 0], [2, 2, .5, 0], [5, 1, 1, 0], [5, 2, .5, 0], [7, 1, 1, 0], [7, 2, .5, 0], [0, 1, 2, 0], [5, 1, 2, 0], [0, 1, 1, 1], [0, 1, 1, 2], [2, 1, 1, 1], [2, 1, 1, 2], [5, 1, 1, 1], [5, 1, 1, 2]]


def _first_sib1(text):
    t = re.sub(r'\s+', '', text)
    i = t.find('DecodedSIB1:')
    if i < 0:
        return t
    j = t.find('DecodedSIB1:', i + 12)
    return t[i:j] if j > 0 else t[i:]


def _pattern(body):
    g = lambda k: (re.search(r'"%s":(\d+)' % k, body) or [0, 0])[1]
    per = re.search(r'"dl-UL-TransmissionPeriodicity-v1530":"(ms[0-9p]+)"', body) or re.search(r'"dl-UL-TransmissionPeriodicity":"(ms[0-9p]+)"', body)
    return dict(ms=_PERIOD_MS.get(per.group(1)) if per else None, dl=int(g('nrofDownlinkSlots')), dls=int(g('nrofDownlinkSymbols')),
                ul=int(g('nrofUplinkSlots')), uls=int(g('nrofUplinkSymbols')))


def parse_sib1_params(text):
    """Layout fields of the first SIB1 in an nrscope log (the log JSON is not valid JSON, so regexes on the whitespace-free text). {} if none."""
    t = _first_sib1(text)
    out = {}
    m = re.search(r'"freqBandIndicatorNR":(\d+)', t)
    if m: out['band'] = int(m.group(1))
    m = re.search(r'"offsetToPointA":(\d+)', t)
    if m: out['point_a'] = int(m.group(1))
    m = re.search(r'"frequencyInfoDL":.*?"scs-SpecificCarrierList":\[\{"offsetToCarrier":(\d+),"subcarrierSpacing":"kHz(\d+)","carrierBandwidth":(\d+)', t)
    if m: out['carrier'] = dict(offset=int(m.group(1)), scs=int(m.group(2)), prbs=int(m.group(3)))
    m = re.search(r'"ssb-PositionsInBurst":\{"inOneGroup":"([01]+)"', t)
    if m: out['ssb_bitmap'] = m.group(1)
    m = re.search(r'"ssb-PeriodicityServingCell":"ms(\d+)"', t)
    if m: out['ssb_period_ms'] = int(m.group(1))
    m = re.search(r'"tdd-UL-DL-ConfigurationCommon":\{"referenceSubcarrierSpacing":"kHz(\d+)","pattern1":\{([^{}]*)\}(?:,"pattern2":\{([^{}]*)\})?', t)
    if m:
        out['tdd'] = dict(scs=int(m.group(1)), p1=_pattern(m.group(2)), p2=_pattern(m.group(3)) if m.group(3) else None)
    return out


def _tdd_meta(tdd, scs):
    """{'period_slots','pattern','special_dl_symbols','special_ul_symbols'} or None. Slot count of a pattern = periodicity * scs / 15."""
    if not tdd or tdd['scs'] != scs:
        return None
    pat, sd, su = '', None, None
    for p in (tdd['p1'], tdd['p2']):
        if not p:
            continue
        s = 'D' * p['dl'] + ('S' if p['dls'] or p['uls'] else '') + 'U' * p['ul']
        if p['ms'] is not None:
            n = p['ms'] * scs / 15
            if n != int(n) or int(n) < len(s):
                return None
            s += 'U' * (int(n) - len(s))
        if 'S' in s and sd is None:
            sd, su = p['dls'], p['uls']
        pat += s
    if not pat:
        return None
    return dict(period_slots=len(pat), pattern=pat, special_dl_symbols=sd if sd is not None else 6, special_ul_symbols=su if su is not None else 0)


def _ssb_symbols(i, scs, tdd):
    """(slot in the burst, first symbol in that slot) of SSB candidate i. Case A/C: {2,8}+14n, Case B (30 kHz, not TDD): {4,8,16,20}+28n."""
    if scs == 30 and not tdd:
        g = (4, 8, 16, 20)[i % 4] + 28 * (i // 4)
    else:
        g = (2, 8)[i % 2] + 14 * (i // 2)
    return g // 14, g % 14


def build_meta(r, p, band=None):
    """Extra meta keys for scan result r (needs r['mib']) from the SIB1 params p. Returns {} when something needed is missing or unsupported."""
    mib = r.get('mib')
    scs = r.get('scs_khz')
    if not (mib and p and scs in (15, 30) and p.get('point_a') is not None and p.get('carrier') and p.get('ssb_bitmap')):
        return {}
    car = p['carrier']
    if car['scs'] != scs or mib['k_ssb'] >= 24:
        return {}
    spf = 10 * scs // 15
    mu = 0 if scs == 15 else 1
    out = {}
    out['carrier_prbs'] = car['prbs']
    out['carrier_center_prb'] = car['offset'] + car['prbs'] / 2.0
    tdd = _tdd_meta(p.get('tdd'), scs)
    if tdd:
        out['tdd'] = tdd
    # SSB: PRB range and the burst
    x = (p['point_a'] * 12 + mib['k_ssb']) * 15.0 / (12 * scs)
    lo = int(x + 0.5); hi = lo + 20
    beams = [i for i, c in enumerate(p['ssb_bitmap']) if c == '1']
    if not beams:
        return out
    blocks, bm = {}, {}
    for i in beams:
        sl, sy = _ssb_symbols(i, scs, tdd)
        blocks.setdefault(sl, []).append(dict(s=sy - 1, b=i))        # viewer convention: s = first SSB symbol - 1 (nrscope's FFT window sits one symbol early)
        bm.setdefault(sl, []).append(str(i))
    slots = sorted(blocks)
    pf = max(1, p.get('ssb_period_ms', 20) // 10)
    out['ssb'] = dict(slot_idx=slots, prb_lo=lo, prb_hi=hi, period_frames=pf, beams={str(k): ','.join(v) for k, v in bm.items()}, nsym=4,
                      blocks={str(k): v for k, v in blocks.items()})
    # CORESET0 / SIB1 PDCCH occasions
    e = (CS0_TAB.get('%d/%d' % (scs, mib.get('scs_common') or scs)) or [None] * 16)[mib['coreset0']]
    ss = CS0_SS0[mib['ss0']] if 0 <= mib['ss0'] < len(CS0_SS0) else None
    if e and ss:
        vals = {}
        for i in beams:
            vals[ss[0] * 2 ** mu + int(i * ss[2])] = i
        cyc = sorted(vals)
        out['coreset0'] = dict(cycle_slots=cyc, slot_idx=[v % spf for v in cyc], prb_lo=lo - e[2], prb_hi=lo - e[2] + e[0], beams={str(v): str(vals[v]) for v in cyc},
                               sym_start=ss[3], nsym=e[1],
                               note='derived from the MIB (CORESET0 idx %d, SS0 idx %d) and the SIB1 SSB bitmap %s' % (mib['coreset0'], mib['ss0'], p['ssb_bitmap']))
    # slot alignment: lock to the SSB burst. width = slots the burst spans (max 3), expected = the best W-slot run under "contrast ~ number of SSB blocks"
    W = max(1, min(3, slots[-1] - slots[0] + 1))
    cnt = lambda s: len(blocks.get(s, []))
    start = max(range(slots[0] - W + 1, slots[-1] + 1), key=lambda k: (sum(cnt(k + j) for j in range(W)), -k))
    ref = [lo, hi]
    cl = car['prbs']
    bg = [b for b in ([max(0, lo - 14), lo - 2], [hi + 2, min(cl, hi + 14)]) if b[1] - b[0] >= 4]
    if bg:
        out['align'] = dict(mode='ssb', period=max(1, min(pf, 8)) * spf, width=W, expected=start % spf, ref=ref, bg=bg)
    return out
