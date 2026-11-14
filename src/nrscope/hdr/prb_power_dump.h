#pragma once
// Per-PRB power tap on the FFT output grid, used by the web visualizer.
#include <cstdint>
#include <string>
#include "srsran/phy/common/phy_common.h"
#include "srsran/phy/phch/dci_nr.h"

namespace PRBPowerDump {

void set_filename(const std::string& filename);

// Latest RX gain in dB (called from the AGC callback / radio init).
void set_rx_gain(float gain_db);

// Average |x|^2 per PRB over the 14 symbols of the slot, for the PRBs fully inside the real FFT window.
//   abs_slot : (sf_round*1024 + sfn)*20 + slot_idx  (monotonic, lets consumers reorder rows)
//   sfn      : system frame number (0..1023)
// Outputs (all optional, env-gated):
//   CSV   : prb_power.csv in CWD unless NRSCOPE_PRB_CSV=0
//   ZMQ   : PUB socket bound at NRSCOPE_ZMQ="tcp://host:port", 2-frame message ("prb", row) per slot
//   ZMQ   : with NRSCOPE_SYM=1 also topic "sym": per-symbol rows (version 2, 14 symbols x nprb)
//   UDP   : (legacy) binary datagram per slot to NRSCOPE_STREAM="host:port"
// NRSCOPE_PRB_DECIM=N keeps every Nth call.
void write_slot(const cf_t* sf_symbols, uint32_t nof_prb, uint32_t slot_idx, uint64_t abs_slot, uint32_t sfn,
                int center_re, uint32_t span_re);

// Equalised PDSCH symbols of a decoded grant (SIB1), ZeroMQ topic "sib1c" (header version 5, flags bit0 = CRC ok, bits1.. = bits/symbol,
// "prb_lo" field = number of symbols; payload = float32 evm, then n x (int8 re, int8 im) scaled by 80).
void send_pdsch_constellation(const cf_t* d, uint32_t n, uint32_t mod_bits, bool crc, float evm, uint64_t abs_slot, uint32_t sfn,
                              uint32_t slot_idx);

// A DCI found by the PDCCH blind search (CRC ok), ZeroMQ topic "dci" (header version 7, "nsym" field = 1), published when NRSCOPE_DCI is set.
// 22-byte header, then: u16 rnti | u8 rnti_type | u8 format | u8 coreset_id | u8 L (log2 of the aggregation level) | u16 ncce (first CCE) |
//                       u8 dir (0 = DL grant, 1 = UL grant) | u8 ss_type | u16 reserved
// Grant contents of a DCI (version 8 of the "dci" message adds them after the 12 payload bytes above, 24 payload bytes in all):
//   u8 flags (bit0 = grant fields valid) | u8 mcs | u8 ndi | u8 rv | u8 pid (HARQ process) | u8 time domain assignment | u16 bwp (PRBs of the BWP the
//   frequency assignment refers to, 0 = unknown) | u32 frequency domain assignment (RIV for resource allocation type 1)
struct DciGrant {
  uint8_t  mcs, ndi, rv, pid, tda;
  uint16_t bwp;
  uint32_t fda;
};
void send_dci_ctx(const srsran_dci_ctx_t* ctx, bool ul, uint64_t abs_slot, uint32_t sfn, uint32_t slot_idx, const DciGrant* g = nullptr);
void send_dci_dl(const srsran_dci_dl_nr_t* d, uint64_t abs_slot, uint32_t sfn, uint32_t slot_idx);
void send_dci_ul(const srsran_dci_ul_nr_t* d, uint64_t abs_slot, uint32_t sfn, uint32_t slot_idx);

// Measurements of a tracked SSB, ZeroMQ topic "meas" (header version 8, flags bit0 = PBCH CRC ok, "sfn" = SFN). After the header: 6 x float32
// (rsrp_dB, epre_dB, n0_dB, snr_dB, cfo_hz, delay_us; dB values relative to full scale), u32 nof_re, u32 ssb_idx.
void ssb_meas_tap(const srsran_csi_trs_measurements_t* m, bool crc, uint32_t ssb_idx, uint32_t sfn);

void close();

} // namespace PRBPowerDump
