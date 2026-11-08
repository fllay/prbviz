#pragma once
// Per-PRB power tap on the FFT output grid, used by the web visualizer.
#include <cstdint>
#include <string>
#include "srsran/phy/common/phy_common.h"

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

void close();

} // namespace PRBPowerDump
