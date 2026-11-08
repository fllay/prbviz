#!/usr/bin/env python3
"""PRB grid server: per-slot rows from nrscope (ZeroMQ SUB, or legacy UDP) -> reorder by absolute slot -> SSE + static page."""
import argparse, base64, json, math, os, queue, socket, struct, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HDR = struct.Struct('<HBBQHBBhHH')  # magic,ver,flags,abs_slot,sfn,slot_idx,rsvd,gain_x10,prb_lo,nprb = 22 bytes
assert HDR.size == 22
HERE = os.path.dirname(os.path.abspath(__file__))


try:
    import numpy as np
except ImportError:      # constellation feature needs numpy
    np = None


def _pss_seq(nid2):
    x = [0, 1, 1, 0, 1, 1, 1] + [0] * 120
    for i in range(120):
        x[i + 7] = (x[i + 4] + x[i]) % 2
    n = np.arange(127)
    return 1 - 2 * np.array(x)[(n + 43 * nid2) % 127]


def _sss_seq(pci):
    n1, n2 = pci // 3, pci % 3
    x0 = [1, 0, 0, 0, 0, 0, 0] + [0] * 120
    x1 = [1, 0, 0, 0, 0, 0, 0] + [0] * 120
    for i in range(120):
        x0[i + 7] = (x0[i + 4] + x0[i]) % 2
        x1[i + 7] = (x1[i + 1] + x1[i]) % 2
    m0, m1 = 15 * (n1 // 112) + 5 * n2, n1 % 112
    n = np.arange(127)
    return (1 - 2 * np.array(x0)[(n + m0) % 127]) * (1 - 2 * np.array(x1)[(n + m1) % 127])


NFFT = 512


def _slope_metric(W, dd):
    """W: (n,127) candidate windows; dd: known +-1 sequence.  Returns (rho, slope_bin) per window. The FFT over subcarriers of y*d
    peaks at the linear phase slope (timing offset), so the metric is insensitive to it."""
    F = np.abs(np.fft.fft(W * dd, NFFT, axis=1))
    e = np.sum(np.abs(W) ** 2, axis=1)
    return F.max(axis=1) / np.sqrt(e * 127 + 1e-20), F.argmax(axis=1)


def _equalise(y, d):
    """y: 127 received REs, d: known +-1 sequence -> (points normalised so that ideal = d, evm_rms)"""
    k = np.arange(127)
    v = y * d
    F = np.fft.fft(v, 4 * NFFT)
    ib = int(np.argmax(np.abs(F)))
    b0 = 2 * np.pi * (ib if ib < 2 * NFFT else ib - 4 * NFFT) / (4 * NFFT)
    z = y * np.exp(-1j * b0 * k)                         # remove linear phase (timing offset) across subcarriers
    z = z * np.exp(-1j * np.angle(np.sum(z * d)))         # remove common phase
    sc = np.mean(np.real(z * d))
    if sc <= 0:
        return None, 9.9, b0
    z = z / sc
    return z, float(np.sqrt(np.mean(np.abs(z - d) ** 2))), b0


class Hub:
    def __init__(self, reorder, batch_ms):
        self.reorder = reorder          # slots held back to absorb out-of-order rows
        self.batch_s = batch_ms / 1000.0
        self.lock = threading.Lock()
        self.rows = {}                  # abs -> (lo, np, gain, bytes)
        self.srows = {}                 # abs -> per-symbol bytes (nsym x np), topic 'sym'
        self.nsym = 14
        self.sym_clients = 0
        self.iq_clients = 0
        self.iq = {}                    # abs -> (sc0, nsc, complex array nsym x nsc)
        self.pci = None
        self.iq_last_ok = 0.0
        self.iq_last_try = 0.0
        self.sib_last = 0.0
        self.sib_cache = []             # last few SIB1 constellation frames, replayed to newly connected pages
        self.scs = 30.0
        self.pss_d = {}
        self.sss_d = None
        self.newest = None
        self.next_emit = None
        self.lo = None
        self.np = None
        self.clients = []
        self.cl_lock = threading.Lock()
        self.stat = dict(rx=0, bad=0, late=0, missing=0, emitted=0, resync=0, clients=0)
        self.last_gain = 0
        # --- slot alignment (nrscope's slot index can sit / slip relative to air time; lock it to the SSB slot)
        self.expected = 0               # slot index the SSB / deepest UL slot must land on
        self.mode = 'ssb'               # 'ssb' | 'ul' (deepest slot of the TDD period) | 'none'
        self.N = 20                     # period (slots) used by the alignment tracker
        self.W = 1                      # run length (slots) of the reference pattern
        self.ref = (14, 20)             # ssb mode: absolute PRB range [lo,hi) of the SSB (testbed default)
        self.bg = [(27, 37)]            # ssb mode: absolute PRB ranges used as background
        self.shift = 0                  # total slot shift applied to raw abs slot
        self.ema = [None] * 20
        self.cnt = 0
        self.best = None
        self.cand = None
        self.cand_n = 0
        self.last_raw = None
        self.stat['slips'] = 0

    def _reset_align(self):
        self.rows.clear(); self.srows.clear(); self.next_emit = None; self.newest = None
        self.ema = [None] * self.N; self.cnt = 0; self.best = None; self.cand = None; self.cand_n = 0
        self.shift = 0; self.last_raw = None

    def _track(self, raw, lo, payload, gain):
        """Lock nrscope's slot index to air time.
        mode 'ssb': reference = the SSB burst. Per slot, contrast = mean dB over PRBs self.ref minus mean dB over self.bg
                    (absolute PRB numbers); the best run of self.W consecutive slots in a self.N-slot cycle is the reference.
        mode 'ul' : reference = the pair of deepest (uplink) slots of the TDD period (gain-normalised power)."""
        if self.mode == 'none':
            return
        N, W = self.N, self.W
        if self.mode == 'ssb':
            A = B = 0.0; na = nb = 0
            for i, q in enumerate(payload):
                if not q:
                    continue
                prb = lo + i
                if self.ref[0] <= prb < self.ref[1]:
                    A += 10 ** ((q / 1.8 - 100) / 10); na += 1
                elif any(r0 <= prb < r1 for r0, r1 in self.bg):
                    B += 10 ** ((q / 1.8 - 100) / 10); nb += 1
            if not na or not nb:
                return
            c = 10 * math.log10(A / na) - 10 * math.log10(B / nb)
        else:
            tot = 0.0; n = 0
            for q in payload:
                if q:
                    tot += 10 ** ((q / 1.8 - 100) / 10); n += 1
            if not n:
                return
            c = -(10 * math.log10(tot / n) - gain / 10.0)       # high score = low power at constant gain
        i = raw % N
        e = self.ema[i]
        self.ema[i] = c if e is None else 0.8 * e + 0.2 * c
        self.cnt += 1
        if self.cnt < 20 * (N // 10):
            return
        self.cnt = 0
        if any(v is None for v in self.ema):
            return
        sc = [sum(self.ema[(k + j) % N] for j in range(W)) for k in range(N)]
        order = sorted(range(N), key=lambda k: -sc[k]); b, s2 = order[0], order[1]
        if self.mode == 'ssb':
            if sc[b] < 8 * W / (1 if W == 1 else 2) or sc[b] - sc[s2] < 6:
                return
        elif sc[b] - sc[s2] < 8:
            return
        h = N // 2
        if self.best is None:
            self.best = b
            self.shift = (self.expected - b) % N
            self.rows.clear(); self.next_emit = None; self.newest = None
            self.cand = None; self.cand_n = 0
            self.stat['resync'] += 1
            print('align[%s]: reference slot found at raw slot %d -> shift %+d' % (self.mode, b, self.shift), flush=True)
        elif b != self.best:
            if self.cand == b:
                self.cand_n += 1
            else:
                self.cand = b; self.cand_n = 1
            if self.cand_n >= 3:
                d = ((self.best - b + h) % N) - h
                self.shift += d
                print('align[%s]: slot slip %+d (raw slot %d -> %d), shift now %+d' % (self.mode, d, self.best, b, self.shift), flush=True)
                self.best = b; self.cand = None; self.cand_n = 0
                self.stat['slips'] += 1
        else:
            self.cand = None; self.cand_n = 0

    # --- input side: ZeroMQ SUB (primary) or UDP (legacy); both deliver the same binary row
    def zmq_loop(self, endpoint):
        import zmq
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.RCVHWM, 200000)
        sock.setsockopt(zmq.SUBSCRIBE, b'prb')
        sock.setsockopt(zmq.SUBSCRIBE, b'sym')
        sock.setsockopt(zmq.SUBSCRIBE, b'iq')
        sock.setsockopt(zmq.SUBSCRIBE, b'sib1c')
        sock.connect(endpoint)             # nrscope BINDs; we may start before or after it and reconnect automatically
        while True:
            parts = sock.recv_multipart()
            if len(parts) != 2:
                self.stat['bad'] += 1
                continue
            if parts[0] == b'sym':
                self.ingest_sym(parts[1])
            elif parts[0] == b'iq':
                self.ingest_iq(parts[1])
            elif parts[0] == b'sib1c':
                self.ingest_sib1c(parts[1])
            else:
                self.ingest(parts[1])

    def ingest_sym(self, d):
        if len(d) < HDR.size or not self.sym_clients:
            return
        magic, ver, nsym, abs_slot, sfn, slot_idx, _r, gain, lo, npr = HDR.unpack_from(d, 0)
        if magic != 0x5052 or ver != 2 or nsym == 0 or len(d) < HDR.size + nsym * npr:
            self.stat['bad'] += 1
            return
        with self.lock:
            abs_slot += self.shift
            if self.next_emit is not None and abs_slot < self.next_emit:
                return
            if self.np is not None and (npr != self.np or lo != self.lo):
                return
            self.nsym = nsym
            self.srows[abs_slot] = d[HDR.size:HDR.size + nsym * npr]

    def ingest_sib1c(self, d):
        """Equalised SIB1 PDSCH symbols from nrscope's SIB1 decoder -> constellation frame for the browsers (<= 10/s)."""
        if np is None or not self.iq_clients or len(d) < 26:
            return
        now = time.time()
        if now - self.sib_last < 0.1:
            return
        magic, ver, flags, abs_slot, sfn, slot_idx, _r, gain, n, _z = HDR.unpack_from(d, 0)
        if magic != 0x5052 or ver != 5 or len(d) < 26 + 2 * n or n == 0:
            self.stat['bad'] += 1
            return
        self.sib_last = now
        evm_rx = struct.unpack_from('<f', d, 22)[0]
        a = np.frombuffer(d, dtype=np.int8, count=2 * n, offset=26).astype(np.float32) / 80.0
        p = a[0::2] + 1j * a[1::2]
        mod = flags >> 1
        evm = None
        if mod == 2 and n > 8:                                  # QPSK: error vector magnitude against the nearest ideal point
            q = p / np.sqrt(np.mean(np.abs(p) ** 2) + 1e-12)
            ideal = (np.sign(q.real) + 1j * np.sign(q.imag)) / np.sqrt(2)
            evm = float(np.sqrt(np.mean(np.abs(q - ideal) ** 2)) * 100)
        step = max(1, n // 900)
        pts = [[round(float(v.real), 2), round(float(v.imag), 2)] for v in p[::step]]
        msg = dict(sib1=dict(pts=pts, n=int(n), mod=int(mod), crc=int(flags & 1), evm=None if evm is None else round(evm, 1),
                             evm_rx=round(float(evm_rx), 3), sfn=int(sfn), slot=int(slot_idx), abs=int(abs_slot)))
        line = json.dumps(msg, separators=(',', ':'))
        self.sib_cache.append((now, msg))
        # keep the 6 best (lowest EVM) frames of the last 5 minutes: the grid timing drifts, so only early frames are clean
        keep = [x for x in self.sib_cache if now - x[0] < 300]
        keep.sort(key=lambda x: x[1]['sib1']['evm'] if x[1]['sib1']['evm'] is not None else 999)
        self.sib_cache = sorted(keep[:6], key=lambda x: x[0])
        with self.cl_lock:
            for q_ in list(self.clients):
                if getattr(q_, 'iq', False):
                    try:
                        q_.put_nowait(line)
                    except queue.Full:
                        pass

    def ingest_iq(self, d):
        if True:                                                # PSS/SSS path (raw IQ around the SSB) is disabled: SIB1 constellation is used instead
            return
        if np is None or not self.iq_clients or self.pci is None or len(d) < HDR.size:
            return
        magic, ver, nsym, abs_slot, sfn, slot_idx, _r, gain, sc0, nsc = HDR.unpack_from(d, 0)
        if magic != 0x5052 or ver != 3 or nsym == 0 or nsc < 127 or len(d) < HDR.size + nsym * nsc * 8:
            self.stat['bad'] += 1
            return
        arr = np.frombuffer(d, dtype=np.float32, count=nsym * nsc * 2, offset=HDR.size).reshape(nsym, nsc, 2)
        y = arr[:, :, 0] + 1j * arr[:, :, 1]
        a = abs_slot + self.shift
        self.iq[a] = (sc0, nsc, y)
        for k in [k for k in self.iq if k < a - 6]:
            del self.iq[k]
        try:
            self._process_iq(a)
        except Exception as ex:                          # never let the constellation feature disturb the data path
            self.stat['iq_err'] = self.stat.get('iq_err', 0) + 1
            self.stat['iq_last_err'] = repr(ex)

    def _process_iq(self, a):
        sc0, nsc, cur = self.iq[a]
        prev = self.iq.get(a - 1)
        if prev is not None and prev[0] == sc0 and prev[1] == nsc:
            Y = np.concatenate([prev[2], cur], axis=0)
            base = prev[2].shape[0]
        else:
            Y = cur
            base = 0
        T = Y.shape[0]
        nid2 = self.pci % 3
        if nid2 not in self.pss_d:
            self.pss_d[nid2] = _pss_seq(nid2)
        if self.sss_d is None:
            self.sss_d = _sss_seq(self.pci)
        dp = self.pss_d[nid2]
        now = time.time()
        if now - self.iq_last_try < 0.04:               # attempts are cheap but not free: <= 25/s
            return
        self.iq_last_try = now
        from numpy.lib.stride_tricks import sliding_window_view
        prof = 10 * np.log10(np.mean(np.abs(Y) ** 2, axis=1) + 1e-20)
        cand = [t for t in range(T - 2) if prof[t] > prof.min() + 12]
        if not cand or len(cand) > 10:
            return
        mid = (nsc - 127) // 2
        o_lo, o_hi = max(0, mid - 16), min(nsc - 127, mid + 16)
        best = None
        for t in cand:
            Wd = sliding_window_view(Y[t], 127)[o_lo:o_hi + 1]
            for flip in (0, 1):
                dd = dp[::-1] if flip else dp
                rho, _ = _slope_metric(Wd, dd)
                j = int(np.argmax(rho))
                if best is None or rho[j] > best[0]:
                    best = (float(rho[j]), t, o_lo + j, flip)
        if best is None or best[0] < 0.45:
            self.stat['iq_miss'] = self.stat.get('iq_miss', 0) + 1
            return
        rho_p, t, o, flip = best
        ds = self.sss_d[::-1] if flip else self.sss_d
        dpp = dp[::-1] if flip else dp
        zp, evp, bp = _equalise(Y[t, o:o + 127], dpp)
        Ws = sliding_window_view(Y[t + 2], 127)[max(0, o - 3):min(nsc - 127, o + 3) + 1]
        rs, _ = _slope_metric(Ws, ds)
        js = int(np.argmax(rs))
        bs = (float(rs[js]), max(0, o - 3) + js)
        zs, evs, _b = _equalise(Y[t + 2, bs[1]:bs[1] + 127], ds)
        if zp is None or zs is None:
            return
        if now - self.iq_last_ok < 0.1:
            return
        self.iq_last_ok = now
        rel = t - base                                  # symbol index relative to symbol 0 of slot a (negative = previous slot)
        msg = dict(const=dict(abs=a, pci=self.pci, pss=[[round(float(v.real), 3), round(float(v.imag), 3)] for v in zp],
                              sss=[[round(float(v.real), 3), round(float(v.imag), 3)] for v in zs], rho=[round(rho_p, 3), round(bs[0], 3)],
                              evm=[round(evp * 100, 1), round(evs * 100, 1)], tau=round(float(-bp / (2 * np.pi * self.scs * 1e3) * 1e6), 2), sym=rel, sc=int(sc0 + o), flip=int(flip)))
        line = json.dumps(msg, separators=(',', ':'))
        with self.cl_lock:
            for q in list(self.clients):
                if getattr(q, 'iq', False):
                    try:
                        q.put_nowait(line)
                    except queue.Full:
                        pass

    def udp_loop(self, host, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        s.bind((host, port))
        while True:
            d, _ = s.recvfrom(2048)
            self.ingest(d)

    def ingest(self, d):
        if True:
            if len(d) < HDR.size:
                self.stat['bad'] += 1
                return
            magic, ver, flags, abs_slot, sfn, slot_idx, _r, gain, lo, npr = HDR.unpack_from(d, 0)
            if magic != 0x5052 or ver != 1 or len(d) < HDR.size + npr:
                self.stat['bad'] += 1
                return
            payload = d[HDR.size:HDR.size + npr]
            with self.lock:
                if self.last_raw is not None and abs_slot < self.last_raw - 20000:
                    self._reset_align()          # nrscope restarted: counters start over
                    self.stat['resync'] += 1
                if self.last_raw is None or abs_slot > self.last_raw:
                    self.last_raw = abs_slot
                self._track(abs_slot, lo, payload, gain)
                abs_slot += self.shift
                if self.np is not None and (npr != self.np or lo != self.lo):
                    # carrier layout changed: start over
                    self.rows.clear(); self.srows.clear(); self.next_emit = None; self.newest = None
                    self.stat['resync'] += 1
                self.lo, self.np = lo, npr
                if self.next_emit is not None and abs_slot < self.next_emit:
                    self.stat['late'] += 1
                    return
                self.rows[abs_slot] = (gain, payload)
                if self.newest is None or abs_slot > self.newest:
                    self.newest = abs_slot
                self.stat['rx'] += 1

    # --- batching side
    def emit_loop(self):
        while True:
            time.sleep(self.batch_s)
            with self.lock:
                if self.newest is None or not self.rows or (self.best is None and self.mode != 'none'):
                    continue
                if self.next_emit is None:
                    self.next_emit = min(self.rows)
                limit = self.newest - self.reorder
                if limit - self.next_emit > 6000 or self.next_emit - self.newest > 6000:
                    self.next_emit = max(min(self.rows), limit - 40)
                    self.stat['resync'] += 1
                if limit < self.next_emit:
                    continue
                n = limit - self.next_emit + 1
                slot0 = self.next_emit
                data = bytearray()
                sdata = bytearray() if self.sym_clients else None
                szero = bytes(self.np * self.nsym)
                gains = []
                miss = 0
                zero = bytes(self.np)
                for a in range(slot0, limit + 1):
                    sr = self.srows.pop(a, None)
                    if sdata is not None:
                        sdata += sr if sr is not None else szero
                    r = self.rows.pop(a, None)
                    if r is None:
                        miss += 1
                        gains.append(self.last_gain)
                        data += zero
                    else:
                        self.last_gain = r[0]
                        gains.append(r[0])
                        data += r[1]
                self.next_emit = limit + 1
                for k in [k for k in self.rows if k < self.next_emit]:
                    del self.rows[k]
                for k in [k for k in self.srows if k < self.next_emit]:
                    del self.srows[k]
                self.stat['missing'] += miss
                self.stat['emitted'] += n
                msg = dict(slot0=slot0, n=n, lo=self.lo, np=self.np, gain=gains, miss=miss, align=self.shift, amode=self.mode,
                           ts=time.time(), data=base64.b64encode(bytes(data)).decode())
            line = json.dumps(msg, separators=(',', ':'))
            sline = None
            if sdata is not None:
                msg['nsym'] = self.nsym
                msg['sym'] = base64.b64encode(bytes(sdata)).decode()
                sline = json.dumps(msg, separators=(',', ':'))
            with self.cl_lock:
                for q in list(self.clients):
                    try:
                        q.put_nowait(sline if (getattr(q, 'sym', False) and sline) else line)
                    except queue.Full:
                        pass  # slow client: drop batches


def make_handler(hub, meta_path):
    class H(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass

        def _send(self, code, ctype, body, extra=None):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Access-Control-Allow-Origin', '*')
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split('?')[0]
            if p in ('/', '/index.html'):
                with open(os.path.join(HERE, 'index.html'), 'rb') as f:
                    self._send(200, 'text/html; charset=utf-8', f.read())
            elif p == '/demo':
                with open(os.path.join(HERE, 'demo.html'), 'rb') as f:
                    self._send(200, 'text/html; charset=utf-8', f.read())
            elif p == '/meta':
                try:
                    with open(meta_path, 'rb') as f:
                        body = f.read()
                except OSError:
                    body = b'{}'
                self._send(200, 'application/json', body)
            elif p == '/stats':
                st = dict(hub.stat)
                st['clients'] = len(hub.clients)
                st['newest'] = hub.newest
                st['np'] = hub.np
                st['lo'] = hub.lo
                self._send(200, 'application/json', json.dumps(st).encode())
            elif p == '/stream':
                q = queue.Queue(maxsize=400)
                q.sym = 'sym=1' in self.path
                q.iq = 'iq=1' in self.path
                if q.iq:
                    for t_, m_ in list(hub.sib_cache):          # replay the last SIB1 constellations (they only exist while the grid timing is good)
                        mm = dict(sib1=dict(m_['sib1'], age=round(time.time() - t_, 1)))
                        q.put_nowait(json.dumps(mm, separators=(',', ':')))
                with hub.cl_lock:
                    hub.clients.append(q)
                    hub.sym_clients = sum(1 for x in hub.clients if getattr(x, 'sym', False))
                    hub.iq_clients = sum(1 for x in hub.clients if getattr(x, 'iq', False))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Connection', 'keep-alive')
                self.end_headers()
                try:
                    last = time.time()
                    while True:
                        try:
                            line = q.get(timeout=5)
                            self.wfile.write(b'data: ' + line.encode() + b'\n\n')
                            self.wfile.flush()
                            last = time.time()
                        except queue.Empty:
                            self.wfile.write(b': keepalive\n\n')
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with hub.cl_lock:
                        if q in hub.clients:
                            hub.clients.remove(q)
                        hub.sym_clients = sum(1 for x in hub.clients if getattr(x, 'sym', False))
                        hub.iq_clients = sum(1 for x in hub.clients if getattr(x, 'iq', False))
            else:
                self._send(404, 'text/plain', b'not found')
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--zmq', default='tcp://127.0.0.1:5556', help='nrscope ZeroMQ PUB endpoint to subscribe to')
    ap.add_argument('--udp', default=None, help='legacy: listen for UDP rows on host:port instead of ZeroMQ')
    ap.add_argument('--http', default='0.0.0.0:8080')
    ap.add_argument('--meta', default=os.path.join(HERE, 'meta.json'))
    ap.add_argument('--reorder', type=int, default=8, help='slots held back to reorder (0.5 ms each)')
    ap.add_argument('--batch-ms', type=int, default=20)
    a = ap.parse_args()
    hub = Hub(a.reorder, a.batch_ms)
    try:
        with open(a.meta) as mf:
            meta = json.load(mf)
    except Exception:
        meta = {}
    if meta.get('pci') is not None:
        hub.pci = int(meta['pci'])
    if meta.get('scs_khz'):
        hub.scs = float(meta['scs_khz'])
    al = meta.get('align') or {}
    if al.get('mode') == 'ul':                         # {"mode":"ul","period":10,"deepest_idx":4}
        hub.mode, hub.N, hub.W, hub.expected = 'ul', int(al.get('period', 10)), 2, int(al.get('deepest_idx', 0))
    elif al.get('mode') == 'ssb':                      # {"mode":"ssb","period":40,"width":3,"expected":1,"ref":[126,146],"bg":[[112,124],[148,160]]}
        hub.mode, hub.N, hub.W = 'ssb', int(al.get('period', 20)), int(al.get('width', 1))
        hub.expected = int(al.get('expected', 0))
        hub.ref = tuple(al['ref']); hub.bg = [tuple(x) for x in al['bg']]
    elif al.get('mode') == 'none' or not meta.get('ssb'):
        hub.mode = 'none'
    else:
        hub.mode, hub.N, hub.expected = 'ssb', 20, int(meta['ssb']['slot_idx'][0])
    hub.ema = [None] * hub.N
    hh, hp = a.http.rsplit(':', 1)
    if a.udp:
        uh, up = a.udp.rsplit(':', 1)
        threading.Thread(target=hub.udp_loop, args=(uh, int(up)), daemon=True).start()
        src = 'udp ' + a.udp
    else:
        threading.Thread(target=hub.zmq_loop, args=(a.zmq,), daemon=True).start()
        src = 'zmq ' + a.zmq
    threading.Thread(target=hub.emit_loop, daemon=True).start()

    def stats():
        prev = dict(hub.stat)
        while True:
            time.sleep(5)
            cur = dict(hub.stat)
            print('rx/s=%.0f emitted/s=%.0f missing=%d late=%d bad=%d resync=%d clients=%d align=%+d slips=%d' % (
                (cur['rx'] - prev['rx']) / 5, (cur['emitted'] - prev['emitted']) / 5,
                cur['missing'], cur['late'], cur['bad'], cur['resync'], len(hub.clients), hub.shift, hub.stat.get('slips', 0)), flush=True)
            prev = cur
    threading.Thread(target=stats, daemon=True).start()
    srv = ThreadingHTTPServer((hh, int(hp)), make_handler(hub, a.meta))
    srv.daemon_threads = True
    print('prbviz: %s  http %s  align=%s' % (src, a.http, hub.mode), flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
