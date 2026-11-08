#!/bin/bash
# Keeps nrscope running: it has an intermittent crash right after decoding SIB1 (race on the SIB1 struct, rach_decoder.cc),
# and it exits when no cell is found. Restart it after a short pause; the server re-aligns by itself.
BIN="${NRSCOPE_BIN:?}"
while true; do
  "$BIN"; rc=$?
  echo "[prbviz] nrscope exited (rc=$rc) at $(date +%T), restarting in 2 s"
  sleep 2
done
