#include "nrscope/hdr/prb_power_dump.h"

#include <algorithm>
#include <arpa/inet.h>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <mutex>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>
#include <zmq.h>

extern "C" {
extern void (*srsran_pbch_nr_const_cb)(const cf_t* symbols, uint32_t nof_symbols, bool crc_ok, const uint32_t* mib_fields);
}

namespace PRBPowerDump {

namespace {
std::mutex            g_mutex;
std::ofstream         g_file;
bool                  g_opened      = false;
std::string           g_filename    = "prb_power.csv";
uint64_t              g_row_counter = 0;
std::atomic<uint64_t> g_call_counter{0};
std::atomic<float>    g_rx_gain{0.0f};

constexpr uint32_t kScPerPrb = 12; // SRSRAN_NRE
constexpr uint32_t kSymbols  = 14; // SRSRAN_NSYMB_PER_SLOT_NR

uint32_t decimation()
{
  static const uint32_t d = [] {
    const char* e = std::getenv("NRSCOPE_PRB_DECIM");
    long        v = e ? std::atol(e) : 1;
    return (uint32_t)(v > 0 ? v : 1);
  }();
  return d;
}

bool csv_enabled()
{
  static const bool en = [] {
    const char* e = std::getenv("NRSCOPE_PRB_CSV");
    return !(e && e[0] == '0');
  }();
  return en;
}

// UDP stream ---------------------------------------------------------------
struct Stream {
  int         fd = -1;
  sockaddr_in addr{};
  Stream()
  {
    const char* e = std::getenv("NRSCOPE_STREAM");
    if (!e || !*e) {
      return;
    }
    std::string s(e);
    auto        p = s.rfind(':');
    if (p == std::string::npos) {
      return;
    }
    std::string host = s.substr(0, p);
    int         port = std::atoi(s.substr(p + 1).c_str());
    addr.sin_family  = AF_INET;
    addr.sin_port    = htons((uint16_t)port);
    if (inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
      return;
    }
    fd = ::socket(AF_INET, SOCK_DGRAM, 0);
  }
};

Stream& stream()
{
  static Stream s;
  return s;
}

// ZeroMQ PUB stream --------------------------------------------------------
// NRSCOPE_ZMQ="tcp://127.0.0.1:5556" -> nrscope BINDs a PUB socket there; consumers SUB-connect.
// Each slot is one 2-frame message: frame 0 = topic "prb", frame 1 = the binary row below.
struct ZmqPub {
  void*      ctx  = nullptr;
  void*      sock = nullptr;
  std::mutex mtx; // zmq sockets are not thread-safe; workers call from several threads
  ZmqPub()
  {
    const char* e = std::getenv("NRSCOPE_ZMQ");
    if (!e || !*e) {
      return;
    }
    ctx = zmq_ctx_new();
    if (!ctx) {
      return;
    }
    sock = zmq_socket(ctx, ZMQ_PUB);
    if (!sock) {
      return;
    }
    int hwm = 200000, linger = 0;
    zmq_setsockopt(sock, ZMQ_SNDHWM, &hwm, sizeof(hwm));
    zmq_setsockopt(sock, ZMQ_LINGER, &linger, sizeof(linger));
    if (zmq_bind(sock, e) != 0) {
      fprintf(stderr, "[prbviz] zmq_bind(%s) failed: %s\n", e, zmq_strerror(zmq_errno()));
      zmq_close(sock);
      sock = nullptr;
      return;
    }
    fprintf(stderr, "[prbviz] ZeroMQ PUB bound on %s\n", e);
  }
  ~ZmqPub()
  {
    if (sock) {
      zmq_close(sock);
    }
    if (ctx) {
      zmq_ctx_term(ctx);
    }
  }
  void publish(const char* topic, const uint8_t* data, size_t len)
  {
    if (!sock) {
      return;
    }
    std::lock_guard<std::mutex> lk(mtx);
    zmq_send(sock, topic, strlen(topic), ZMQ_SNDMORE | ZMQ_DONTWAIT);
    zmq_send(sock, data, len, ZMQ_DONTWAIT);
  }
};

ZmqPub& zpub()
{
  static ZmqPub z;
  return z;
}

// Row layout (little endian, 22-byte header + nprb bytes):
//   u16 magic 0x5052 | u8 version=1 | u8 flags | u64 abs_slot | u16 sfn | u8 slot_idx | u8 rsvd |
//   i16 gain_x10 | u16 prb_lo | u16 nprb | nprb x u8 q, with dB = q/1.8 - 100 (0 = missing)
constexpr float kQScale  = 1.8f;
constexpr float kQOffset = 100.0f;

void send_datagram(uint64_t abs_slot, uint32_t sfn, uint32_t slot_idx, int prb_lo, const std::vector<float>& db)
{
  Stream& s = stream();
  ZmqPub& z = zpub();
  if (s.fd < 0 && !z.sock) {
    return;
  }
  uint8_t  buf[22 + 512];
  size_t   n     = std::min<size_t>(db.size(), 512);
  uint16_t magic = 0x5052;
  uint16_t sfn16 = (uint16_t)sfn;
  int16_t  gain  = (int16_t)std::lround(g_rx_gain.load() * 10.0f);
  uint16_t lo    = (uint16_t)prb_lo;
  uint16_t np    = (uint16_t)n;
  memcpy(buf + 0, &magic, 2);
  buf[2] = 1;
  buf[3] = 0;
  memcpy(buf + 4, &abs_slot, 8);
  memcpy(buf + 12, &sfn16, 2);
  buf[14] = (uint8_t)slot_idx;
  buf[15] = 0;
  memcpy(buf + 16, &gain, 2);
  memcpy(buf + 18, &lo, 2);
  memcpy(buf + 20, &np, 2);
  for (size_t i = 0; i < n; i++) {
    float q = (db[i] + kQOffset) * kQScale;
    q       = std::min(255.0f, std::max(0.0f, q));
    buf[22 + i] = (uint8_t)std::lround(q);
  }
  if (s.fd >= 0) {
    ::sendto(s.fd, buf, 22 + n, 0, (const sockaddr*)&s.addr, sizeof(s.addr));
  }
  z.publish("prb", buf, 22 + n);
}

// Per-symbol row (topic "sym", ZeroMQ only, enabled by NRSCOPE_SYM=1): same 22-byte header with version=2 and flags = number
// of symbols (14); payload = nsym x nprb bytes, symbol-major (symbol 0 all PRBs, symbol 1 ...), same q encoding as above.
bool sym_enabled()
{
  static const bool en = [] {
    const char* e = std::getenv("NRSCOPE_SYM");
    return e && e[0] != '0';
  }();
  return en && zpub().sock != nullptr;
}

void send_symbols(uint64_t abs_slot, uint32_t sfn, uint32_t slot_idx, int prb_lo, uint32_t nprb, const std::vector<float>& sdb)
{
  ZmqPub& z = zpub();
  if (!z.sock || nprb == 0 || nprb > 512) {
    return;
  }
  const size_t nsym = sdb.size() / nprb;
  std::vector<uint8_t> buf(22 + sdb.size());
  uint16_t magic = 0x5052, sfn16 = (uint16_t)sfn, lo = (uint16_t)prb_lo, np = (uint16_t)nprb;
  int16_t  gain = (int16_t)std::lround(g_rx_gain.load() * 10.0f);
  memcpy(buf.data() + 0, &magic, 2);
  buf[2] = 2;
  buf[3] = (uint8_t)nsym;
  memcpy(buf.data() + 4, &abs_slot, 8);
  memcpy(buf.data() + 12, &sfn16, 2);
  buf[14] = (uint8_t)slot_idx;
  buf[15] = 0;
  memcpy(buf.data() + 16, &gain, 2);
  memcpy(buf.data() + 18, &lo, 2);
  memcpy(buf.data() + 20, &np, 2);
  for (size_t i = 0; i < sdb.size(); i++) {
    float q = (sdb[i] + kQOffset) * kQScale;
    q       = std::min(255.0f, std::max(0.0f, q));
    buf[22 + i] = (uint8_t)std::lround(q);
  }
  z.publish("sym", buf.data(), buf.size());
}

void ensure_open_locked()
{
  if (g_opened) {
    return;
  }
  g_file.open(g_filename, std::ios::out | std::ios::trunc);
  g_opened = true; // only try once, even if open() failed
}
} // namespace

void set_filename(const std::string& filename)
{
  std::lock_guard<std::mutex> lock(g_mutex);
  if (!g_opened) {
    g_filename = filename;
  }
}

void set_rx_gain(float gain_db)
{
  g_rx_gain.store(gain_db);
}

// Raw complex resource elements around the SSB (topic "iq", ZeroMQ only, needs NRSCOPE_SYM=1 and NRSCOPE_SSB_PRB="lo,hi"
// = absolute PRB range of the SSB). Sent only for slots that look like they carry SSB symbols (2..8 symbols clearly above the
// quietest symbol inside the SSB PRBs). Header: version 3, flags = nsym, "prb_lo" field = first subcarrier, "nprb" field = number
// of subcarriers; payload = nsym x nsc complex float32 (re, im), symbol-major.
static bool ssb_prb_range(int& lo, int& hi)
{
  static int  l = -1, h = -1;
  static bool init = false;
  if (!init) {
    init = true;
    const char* e = std::getenv("NRSCOPE_SSB_PRB");
    if (e && sscanf(e, "%d,%d", &l, &h) == 2 && h > l) {
    } else {
      l = h = -1;
    }
  }
  lo = l;
  hi = h;
  return l >= 0;
}

static void maybe_send_iq(const cf_t* sf_symbols, int nof_re, int lo_re, int hi_re, int prb_lo, uint32_t nprb, uint64_t abs_slot,
                          uint32_t sfn, uint32_t slot_idx, const std::vector<float>& sdb)
{
  ZmqPub& z = zpub();
  int     slo, shi;
  if (!z.sock || !ssb_prb_range(slo, shi)) {
    return;
  }
  const int p0 = std::max(slo, prb_lo), p1 = std::min(shi, prb_lo + (int)nprb);
  if (p1 <= p0) {
    return;
  }
  float prof[kSymbols], mn = 1e9f;
  for (uint32_t sym = 0; sym < kSymbols; sym++) {
    double a = 0;
    for (int p = p0; p < p1; p++) {
      a += sdb[(size_t)sym * nprb + (p - prb_lo)];
    }
    prof[sym] = (float)(a / (p1 - p0));
    mn        = std::min(mn, prof[sym]);
  }
  int loud = 0;
  for (uint32_t sym = 0; sym < kSymbols; sym++) {
    if (prof[sym] > mn + 15.0f) {
      loud++;
    }
  }
  if (loud < 2 || loud > 8) {
    return;
  }
  static std::atomic<int64_t> last_us{0};
  const int64_t now = std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
  if (now - last_us.load() < 1500) { // at most ~650 messages/s
    return;
  }
  last_us.store(now);
  const int sc0 = std::max(lo_re, slo * (int)kScPerPrb - 12), sc1 = std::min(hi_re, shi * (int)kScPerPrb + 12);
  if (sc1 <= sc0) {
    return;
  }
  const size_t       nsc = (size_t)(sc1 - sc0);
  std::vector<uint8_t> buf(22 + kSymbols * nsc * 8);
  uint16_t magic = 0x5052, sfn16 = (uint16_t)sfn, lo = (uint16_t)sc0, np = (uint16_t)nsc;
  int16_t  gain = (int16_t)std::lround(g_rx_gain.load() * 10.0f);
  memcpy(buf.data() + 0, &magic, 2);
  buf[2] = 3;
  buf[3] = (uint8_t)kSymbols;
  memcpy(buf.data() + 4, &abs_slot, 8);
  memcpy(buf.data() + 12, &sfn16, 2);
  buf[14] = (uint8_t)slot_idx;
  buf[15] = 0;
  memcpy(buf.data() + 16, &gain, 2);
  memcpy(buf.data() + 18, &lo, 2);
  memcpy(buf.data() + 20, &np, 2);
  float* out = (float*)(buf.data() + 22);
  for (uint32_t sym = 0; sym < kSymbols; sym++) {
    const cf_t* row = sf_symbols + (size_t)sym * nof_re;
    for (size_t k = 0; k < nsc; k++) {
      cf_t v           = row[sc0 + k];
      *out++           = __real__ v;
      *out++           = __imag__ v;
    }
  }
  z.publish("iq", buf.data(), buf.size());
}

void send_pdsch_constellation(const cf_t* d, uint32_t n, uint32_t mod_bits, bool crc, float evm, uint64_t abs_slot, uint32_t sfn,
                              uint32_t slot_idx)
{
  ZmqPub& z = zpub();
  if (!z.sock || d == nullptr || n == 0) {
    return;
  }
  n = std::min<uint32_t>(n, 4000);
  std::vector<uint8_t> buf(22 + 4 + (size_t)n * 2);
  uint16_t magic = 0x5052, sfn16 = (uint16_t)sfn, np = (uint16_t)n, zero16 = 0;
  int16_t  gain = (int16_t)std::lround(g_rx_gain.load() * 10.0f);
  memset(buf.data(), 0, buf.size());
  memcpy(buf.data() + 0, &magic, 2);
  buf[2] = 5;
  buf[3] = (uint8_t)((crc ? 1 : 0) | (mod_bits << 1));
  memcpy(buf.data() + 4, &abs_slot, 8);
  memcpy(buf.data() + 12, &sfn16, 2);
  buf[14] = (uint8_t)slot_idx;
  memcpy(buf.data() + 16, &gain, 2);
  memcpy(buf.data() + 18, &np, 2);
  memcpy(buf.data() + 20, &zero16, 2);
  memcpy(buf.data() + 22, &evm, 4);
  int8_t* out = (int8_t*)(buf.data() + 26);
  for (uint32_t i = 0; i < n; i++) {
    float re = std::min(127.0f, std::max(-127.0f, __real__ d[i] * 80.0f));
    float im = std::min(127.0f, std::max(-127.0f, __imag__ d[i] * 80.0f));
    out[2 * i]     = (int8_t)std::lround(re);
    out[2 * i + 1] = (int8_t)std::lround(im);
  }
  z.publish("sib1c", buf.data(), buf.size());
}


void send_dci_ctx(const srsran_dci_ctx_t* c, bool ul, uint64_t abs_slot, uint32_t sfn, uint32_t slot_idx)
{
  static const bool on = std::getenv("NRSCOPE_DCI") != nullptr;
  ZmqPub& z = zpub();
  if (!on || !z.sock || c == nullptr) {
    return;
  }
  uint8_t  buf[22 + 12];
  uint16_t magic = 0x5052, sfn16 = (uint16_t)sfn, zero16 = 0, one16 = 1, rnti = c->rnti, ncce = (uint16_t)c->location.ncce;
  int16_t  gain = (int16_t)std::lround(g_rx_gain.load() * 10.0f);
  memset(buf, 0, sizeof(buf));
  memcpy(buf + 0, &magic, 2);
  buf[2] = 7;
  buf[3] = 1;
  memcpy(buf + 4, &abs_slot, 8);
  memcpy(buf + 12, &sfn16, 2);
  buf[14] = (uint8_t)slot_idx;
  memcpy(buf + 16, &gain, 2);
  memcpy(buf + 18, &zero16, 2);
  memcpy(buf + 20, &one16, 2);
  memcpy(buf + 22, &rnti, 2);
  buf[24] = (uint8_t)c->rnti_type;
  buf[25] = (uint8_t)c->format;
  buf[26] = (uint8_t)c->coreset_id;
  buf[27] = (uint8_t)c->location.L;
  memcpy(buf + 28, &ncce, 2);
  buf[30] = ul ? 1 : 0;
  buf[31] = (uint8_t)c->ss_type;
  z.publish("dci", buf, sizeof(buf));
}

// Equalised PBCH symbols (432 QPSK points per SSB), ZeroMQ topic "pbchc" (header version 6, flags bit0 = CRC ok, bits1..6 = bits/symbol (2),
// bit7 = MIB trailer present, "prb_lo" field = number of symbols; payload = float32 (0), n x (int8 re, int8 im) scaled by 80, then
// optionally 10 x uint32 MIB fields: sfn, ssb_idx, hrf, scs_common, k_ssb, dmrs_typeA_pos, coreset0_idx, ss0_idx, cell_barred, intra_freq_resel).
// Called from srsran_pbch_nr_decode().
void pbch_const_tap(const cf_t* d, uint32_t n, bool crc, const uint32_t* mib_fields)
{
  ZmqPub& z = zpub();
  if (!z.sock || d == nullptr || n == 0) {
    return;
  }
  n = std::min<uint32_t>(n, 4000);
  const size_t         trailer = mib_fields ? 40 : 0;
  std::vector<uint8_t> buf(22 + 4 + (size_t)n * 2 + trailer);
  uint16_t magic = 0x5052, np = (uint16_t)n;
  int16_t  gain = (int16_t)std::lround(g_rx_gain.load() * 10.0f);
  memset(buf.data(), 0, buf.size());
  memcpy(buf.data() + 0, &magic, 2);
  buf[2] = 6;
  buf[3] = (uint8_t)((crc ? 1 : 0) | (2 << 1) | (mib_fields ? 0x80 : 0));
  memcpy(buf.data() + 16, &gain, 2);
  memcpy(buf.data() + 18, &np, 2);
  int8_t* out = (int8_t*)(buf.data() + 26);
  for (uint32_t i = 0; i < n; i++) {
    float re = std::min(127.0f, std::max(-127.0f, __real__ d[i] * 80.0f));
    float im = std::min(127.0f, std::max(-127.0f, __imag__ d[i] * 80.0f));
    out[2 * i]     = (int8_t)std::lround(re);
    out[2 * i + 1] = (int8_t)std::lround(im);
  }
  if (mib_fields) {
    memcpy(buf.data() + 26 + (size_t)n * 2, mib_fields, 40);
  }
  z.publish("pbchc", buf.data(), buf.size());
}

void write_slot(const cf_t* sf_symbols, uint32_t nof_prb, uint32_t slot_idx, uint64_t abs_slot, uint32_t sfn,
                int center_re, uint32_t span_re)
{
  if (sf_symbols == nullptr || nof_prb == 0 || span_re == 0) {
    return;
  }
  if (g_call_counter.fetch_add(1) % decimation() != 0) {
    return;
  }

  const int nof_re = (int)(nof_prb * kScPerPrb);
  const int lo_re  = std::max(0, center_re - (int)(span_re / 2));
  const int hi_re  = std::min(nof_re, center_re + (int)(span_re / 2)); // exclusive
  const int prb_lo = (lo_re + (int)kScPerPrb - 1) / (int)kScPerPrb;     // first fully-inside PRB
  const int prb_hi = hi_re / (int)kScPerPrb;                            // exclusive
  if (prb_hi <= prb_lo) {
    return;
  }

  std::vector<float> db(prb_hi - prb_lo);
  const bool         sym_on = sym_enabled();
  const uint32_t     nprb   = (uint32_t)(prb_hi - prb_lo);
  std::vector<float> sdb;
  if (sym_on) {
    sdb.assign((size_t)kSymbols * nprb, -120.0f);
  }
  for (int prb = prb_lo; prb < prb_hi; prb++) {
    double sum = 0.0;
    for (uint32_t sym = 0; sym < kSymbols; sym++) {
      const cf_t* row = sf_symbols + (size_t)sym * nof_re;
      double      ss  = 0.0;
      for (uint32_t sc = 0; sc < kScPerPrb; sc++) {
        cf_t  v  = row[prb * kScPerPrb + sc];
        float re = __real__ v;
        float im = __imag__ v;
        ss += (double)(re * re + im * im);
      }
      sum += ss;
      if (sym_on) {
        float sd = 10.0f * log10f((float)(ss / kScPerPrb > 1e-12 ? ss / kScPerPrb : 1e-12));
        sdb[(size_t)sym * nprb + (prb - prb_lo)] = std::isfinite(sd) ? sd : -120.0f;
      }
    }
    double mean = sum / (double)(kSymbols * kScPerPrb);
    float  d    = 10.0f * log10f((float)(mean > 1e-12 ? mean : 1e-12));
    db[prb - prb_lo] = std::isfinite(d) ? d : -120.0f;
  }

  send_datagram(abs_slot, sfn, slot_idx, prb_lo, db);
  if (sym_on) {
    send_symbols(abs_slot, sfn, slot_idx, prb_lo, nprb, sdb);
    maybe_send_iq(sf_symbols, nof_re, lo_re, hi_re, prb_lo, nprb, abs_slot, sfn, slot_idx, sdb);
  }

  if (!csv_enabled()) {
    return;
  }
  std::lock_guard<std::mutex> lock(g_mutex);
  ensure_open_locked();
  if (!g_file.is_open()) {
    return;
  }
  if (g_row_counter == 0) {
    g_file << "row,slot_idx";
    for (int prb = prb_lo; prb < prb_hi; prb++) {
      g_file << ",prb" << prb;
    }
    g_file << "\n";
  }
  g_file << g_row_counter << "," << slot_idx;
  for (float d : db) {
    g_file << "," << d;
  }
  g_file << "\n";
  g_row_counter++;
  if ((g_row_counter & 0xFF) == 0) {
    g_file.flush();
  }
}

void close()
{
  std::lock_guard<std::mutex> lock(g_mutex);
  if (g_file.is_open()) {
    g_file.close();
  }
  g_opened = false;
}

} // namespace PRBPowerDump

namespace {
const bool g_pbch_tap_registered = [] {
  const char* e = std::getenv("NRSCOPE_PBCHC");
  if (e != nullptr && *e != '\0' && *e != '0') {
    srsran_pbch_nr_const_cb = &PRBPowerDump::pbch_const_tap;
  }
  return true;
}();
} // namespace
