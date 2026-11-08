#!/bin/bash
# Keeps nrscope running: it has an intermittent crash right after decoding SIB1 (race on the SIB1 struct, rach_decoder.cc),
# and it exits when no cell is found. Restart it after a short pause; the server re-aligns by itself.
BIN="${NRSCOPE_BIN:?}"
# NRSCOPE_LOG=<file>: send everything (nrscope output and the restart messages) through rotlog.py, which keeps NRSCOPE_LOG_KEEP (5) files of
# NRSCOPE_LOG_MB (20) MB each (<file>, <file>.1 ... .4) instead of one file that grows without limit.
if [ -n "$NRSCOPE_LOG" ]; then
  exec > >(python3 "$(dirname "$0")/rotlog.py" "$NRSCOPE_LOG" "${NRSCOPE_LOG_MB:-20}" "${NRSCOPE_LOG_KEEP:-5}") 2>&1
fi
while true; do
  "$BIN"; rc=$?
  echo "[prbviz] nrscope exited (rc=$rc) at $(date +%T), restarting in 2 s"
  sleep 2
done
