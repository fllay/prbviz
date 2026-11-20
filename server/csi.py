"""CSI-RS channel estimation from nrscope's 'csi' topic (raw resource elements of symbols 4 and 8 of every slot).

The gNB (OCUDU) sends, every 40 slots (20 ms at 30 kHz SCS), with scramblingID 1:
  slot 2 and 3 : TRS, 1 port, density 3 (pilots on subcarriers k0, k0+4, k0+8 of every PRB), symbols 4 and 8
  slot 4       : 2-port CSI-RS, density 1, FD-CDM2 (pilot pair on subcarriers 2,3 of every PRB), symbol 4
(values from the UE's RRCReconfiguration, see README; override them with server/csi.json).
nrscope's slot counter can sit at an arbitrary offset, so the module locks onto the slot that carries the 2-port CSI-RS by trying the
pilot sequence on loud symbols (a wrong slot number gives an incoherent result, the right one a smooth channel).
Per 20 ms occasion it returns: H of both ports (from the pilot pair: S = h0+h1, D = h0-h1), H of the TRS, channel impulse responses,
timing, and the phase-aligned taps (timing slope removed, phase referenced to the strongest tap of port 0)."""
import json
import os
import struct
import time

import numpy as np

HDR = struct.Struct('<HBBQHBBhHH')
DEFAULT = dict(nid=1, trs_slots=[2, 3], trs_syms=[4, 8], trs_k0=1, res_slot=4, res_sym=4, res_k=2, period=40)
NFFT = 256          # zero padding of the CIR (bin = 1/(NFFT*df): 10.85 ns for the 2-port resource)
CROP = 128          # bins kept around zero delay in the output (+-0.69 us)
TAPS = (-8, 24)     # aligned taps sent: bins -8 .. +23 around the strongest tap


def _gold_tables():
    L = 2100
    x1 = np.zeros(L + 31, np.uint8)
    x1[0] = 1
    for n in range(L):
        x1[n + 31] = x1[n + 3] ^ x1[n]
    B = np.zeros((31, L), np.uint8)
    for i in range(31):
        x2 = np.zeros(L + 31, np.uint8)
        x2[i] = 1
        for n in range(L):
            x2[n + 31] = x2[n + 3] ^ x2[n + 2] ^ x2[n + 1] ^ x2[n]
        B[i] = x2[:L]
    return x1[:L], B.astype(np.int32)


_X1, _B = _gold_tables()
_seq_cache = {}


def pilot_seq(ns, l, nid, m):
    """CSI-RS sequence r(0..m-1) of TS 38.211 7.4.1.5.2 (slot ns in frame, symbol l, scramblingID nid)."""
    key = (ns, l, nid, m)
    r = _seq_cache.get(key)
    if r is None:
        cinit = ((1 << 10) * (14 * ns + l + 1) * (2 * nid + 1) + nid) % (1 << 31)
        bits = ((cinit >> np.arange(31)) & 1).astype(np.int32)
        x2 = (bits @ _B[:, 1600:1600 + 2 * m]) & 1
        c = _X1[1600:1600 + 2 * m].astype(np.int32) ^ x2
        r = ((1 - 2 * c[0::2]) + 1j * (1 - 2 * c[1::2])) / np.sqrt(2)
        if len(_seq_cache) > 400:
            _seq_cache.clear()
        _seq_cache[key] = r
    return r


def coherence(h):
    """Adjacent-pilot phase coherence: ~1 for a smooth channel (right sequence), ~0.1 for noise (wrong sequence)."""
    return float(abs(np.sum(h[1:] * np.conj(h[:-1]))) / (np.sum(abs(h[1:]) * abs(h[:-1])) + 1e-30))


def _tau_slope(h, df):
    """Delay (s) from the phase slope over frequency; |tau| < 1/(2 df)."""
    return float(-np.angle(np.sum(h[1:] * np.conj(h[:-1]))) / (2 * np.pi * df))


def _cir(h, n=NFFT):
    c = np.fft.ifft(np.concatenate([h, np.zeros(n - len(h), complex)])) * (n / len(h))
    return np.fft.fftshift(c)      # index n/2 = zero delay


def _peak(c):
    a = 20 * np.log10(abs(c) + 1e-12)
    k = int(np.argmax(a))
    d = 0.0
    if 0 < k < len(a) - 1:
        den = a[k - 1] - 2 * a[k] + a[k + 1]
        if den < 0:
            d = 0.5 * (a[k - 1] - a[k + 1]) / den
    return k + d, float(a[k])


def _r(x, k=1):
    return np.round(np.asarray(x, float), k).tolist()


class CsiProc:
    def __init__(self, path=None):
        self.cfg = dict(DEFAULT)
        try:
            if path and os.path.exists(path):
                self.cfg.update(json.load(open(path)))
        except Exception:
            pass
        self.off = None            # raw abs_slot % 40 of the gNB's slot 0
        self.cand = None
        self.cand_n = 0
        self.bad = 0
        self.pend = {}             # occasion -> dict(trs=..., res0=..., last=abs slot)
        self.stat = dict(occ=0, locks=0, unlocks=0)

    # ---- parsing -----------------------------------------------------------------------------------------------------
    @staticmethod
    def _grid(d, nsc):
        g = np.frombuffer(d, np.float32, count=2 * nsc * 2, offset=26).reshape(2, nsc, 2)
        return g[..., 0] + 1j * g[..., 1]          # row 0 = symbol d[22], row 1 = symbol d[23]

    def _res0(self, y, ns, nprb):
        Y = y[:nprb * 12].reshape(nprb, 12)
        r = pilot_seq(ns, self.cfg['res_sym'], self.cfg['nid'], 2 * nprb)
        n = np.arange(nprb)
        k = self.cfg['res_k']
        return Y[:, k] / r[2 * n], Y[:, k + 1] / r[2 * n + 1]

    def _trs(self, y, ns, l, nprb):
        k0 = self.cfg['trs_k0']
        Y = y[:nprb * 12].reshape(nprb, 12)[:, [k0, k0 + 4, k0 + 8]].reshape(-1)
        return Y / pilot_seq(ns, l, self.cfg['nid'], 3 * nprb)

    # ---- locking -----------------------------------------------------------------------------------------------------
    def _try_lock(self, a, y, nprb):
        k = self.cfg['res_k']
        Y = y[0][:nprb * 12].reshape(nprb, 12)
        pil = np.mean(abs(Y[:, k]) ** 2 + abs(Y[:, k + 1]) ** 2)
        ref = np.mean(abs(Y[:, (k + 5) % 12]) ** 2 + abs(Y[:, (k + 6) % 12]) ** 2) + 1e-20
        if pil < 4 * ref:          # the 2-port CSI-RS is far above the other REs of its symbol
            return
        S, D = self._res0(y[0], self.cfg['res_slot'], nprb)
        if min(coherence(S), coherence(D)) < 0.6:
            return
        off = (a - self.cfg['res_slot']) % self.cfg['period']
        if off == self.cand:
            self.cand_n += 1
        else:
            self.cand, self.cand_n = off, 1
        if self.cand_n >= 3:
            self.off, self.bad = off, 0
            self.stat['locks'] += 1
            self.pend.clear()

    # ---- main entry --------------------------------------------------------------------------------------------------
    def feed(self, d):
        """One 'csi' message -> None or a finished occasion (dict)."""
        if len(d) < 26:
            return None
        magic, ver, fl, a, sfn, sidx, _r0, gain, _lo, nsc = HDR.unpack_from(d, 0)
        if magic != 0x5052 or ver != 9 or nsc < 24 or len(d) < 26 + 2 * nsc * 8:
            return None
        nprb = nsc // 12
        cfg = self.cfg
        if self.off is None:
            if d[22] == cfg['res_sym']:
                self._try_lock(a, self._grid(d, nsc), nprb)
            return None
        m = (a - self.off) % cfg['period']
        occ = (a - self.off) // cfg['period']
        out = self._finish(a)
        if m not in (cfg['res_slot'], *cfg['trs_slots']):
            return out
        y = self._grid(d, nsc)
        syms = [d[22], d[23]]
        p = self.pend.setdefault(occ, dict(trs={}, res0=None, last=a, nprb=nprb))
        p['last'] = max(p['last'], a)
        if m in cfg['trs_slots']:
            for j, l in enumerate(syms):
                if l in cfg['trs_syms']:
                    p['trs'][(m, l)] = self._trs(y[j], m % 20, l, nprb)
        elif m == cfg['res_slot']:
            p['res0'] = self._res0(y[syms.index(cfg['res_sym'])], m % 20, nprb)
            p['res_abs'] = a
        return out

    def _finish(self, a):
        """Finalise the oldest occasion once newer slots have arrived (slots reach us slightly out of order)."""
        for occ in sorted(self.pend):
            p = self.pend[occ]
            ra = p.get('res_abs')
            if (ra is not None and a - ra >= 4) or a - p['last'] > 80:
                del self.pend[occ]
                if ra is None or p['res0'] is None:
                    continue
                return self._compute(p, occ, ra)
        return None

    # ---- estimation ----------------------------------------------------------------------------------------------------
    def _compute(self, p, occ, ra):
        S, D = p['res0']
        nprb = p['nprb']
        df0 = 12 * 30e3
        c = 0.5 * (coherence(S) + coherence(D))
        if c < 0.4:
            self.bad += 1
            if self.bad >= 20:
                self.off, self.cand, self.cand_n, self.bad = None, None, 0, 0
                self.stat['unlocks'] += 1
            return None
        self.bad = 0
        t_est = _tau_slope(S, df0)
        D = D * np.exp(2j * np.pi * 30e3 * t_est)          # bring D (30 kHz higher) to the frequency of S
        H = [0.5 * (S + D), 0.5 * (S - D)]                  # ports 0 and 1
        i = np.arange(nprb)
        # CIR of both ports; the reference is the stronger port (the other one can sit near the noise floor, see README)
        c0, c1 = _cir(H[0]), _cir(H[1])
        (k0, pk0), (k1, pk1) = _peak(c0), _peak(c1)
        ref = 1 if pk1 > pk0 else 0
        oth = 1 - ref
        tau0, tau1 = [(k - NFFT / 2) / (NFFT * df0) for k in (k0, k1)]
        tau_ref = (tau0, tau1)[ref]
        # timing slope removed (strongest tap of the reference port moved to zero delay), phase referenced to that tap
        Hs = [h * np.exp(2j * np.pi * i * df0 * tau_ref) for h in H]
        theta = float(np.angle(np.mean(Hs[ref])))
        Ha = [h * np.exp(-1j * theta) for h in Hs]
        ca = [_cir(h) for h in Ha]
        mid = NFFT // 2
        win = slice(mid - 4, mid + 5)
        kk = mid - 4 + int(np.argmax(abs(ca[oth][win])))
        dphi = float(np.degrees(np.angle(ca[oth][kk])))
        sl = slice(mid + TAPS[0], mid + TAPS[1])
        h2 = slice(mid - CROP // 2, mid + CROP // 2)
        res0 = dict(df=df0 / 1e3, f0=2.5 * 30, n=int(nprb),
                    a=[_r(20 * np.log10(abs(h) + 1e-12)) for h in H],
                    p=[_r(np.degrees(np.unwrap(np.angle(h))), 0) for h in Hs],
                    cir=[_r(20 * np.log10(abs(x[h2]) + 1e-12)) for x in (c0, c1)],
                    tau=[round(tau0 * 1e9, 1), round(tau1 * 1e9, 1)], pk=[round(pk0, 1), round(pk1, 1)],
                    bin=round(1e9 / (NFFT * df0), 3))
        al = dict(m=[_r(20 * np.log10(abs(x[sl]) + 1e-12)) for x in ca],
                  ph=[_r(np.degrees(np.angle(x[sl])), 0) for x in ca],
                  t0=TAPS[0], ref=ref, theta=round(np.degrees(theta), 1), dphi=round(dphi, 1))
        trs = None
        T = p['trs']
        if (2, 4) in T or (3, 4) in T:
            ref_key = sorted(T)[0]
            ref = T[ref_key]
            acc = np.zeros_like(ref)
            for kx, h in T.items():
                acc += h * np.exp(-1j * np.angle(np.sum(h * np.conj(ref))))
            Ht = acc / len(T)
            cfo = None
            if (2, 4) in T and (3, 4) in T:
                z = np.sum(T[(3, 4)] * np.conj(T[(2, 4)]))
                if (2, 8) in T and (3, 8) in T:
                    z = z + np.sum(T[(3, 8)] * np.conj(T[(2, 8)]))
                cfo = round(float(np.angle(z) / (2 * np.pi * 0.5e-3)), 1)
            dft = 4 * 30e3
            ct = _cir(Ht, 3 * NFFT)
            kt, pkt = _peak(ct)
            taut = (kt - 3 * NFFT / 2) / (3 * NFFT * dft)
            mt = 3 * NFFT // 2
            trs = dict(df=dft / 1e3, f0=(self.cfg['trs_k0'] + 0.0) * 30, n=3 * nprb, a=_r(20 * np.log10(abs(Ht) + 1e-12)),
                       p=_r(np.degrees(np.unwrap(np.angle(Ht * np.exp(2j * np.pi * np.arange(len(Ht)) * dft * taut)))), 0),
                       cir=_r(20 * np.log10(abs(ct[mt - CROP // 2:mt + CROP // 2]) + 1e-12)),
                       tau=round(taut * 1e9, 1), pk=round(pkt, 1), cfo=cfo, coh=round(coherence(Ht), 2), nsym=len(T),
                       bin=round(1e9 / (3 * NFFT * dft), 3))
        self.stat['occ'] += 1
        return dict(t=round(time.time(), 3), occ=int(occ), slot=int(ra), coh=round(c, 2), r0=res0, al=al, trs=trs)
