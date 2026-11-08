#!/bin/bash
# Run nrscan with a config (writes scan.csv into run/scan/). Radio is exclusive: stop live view first.
#   usage: scripts/scan.sh [config.yaml]
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${1:-$ROOT/config/testbed_n78.yaml}"
"$ROOT/scripts/stop.sh" >/dev/null 2>&1
sleep 3                                            # let the B210 be released (the first scan right after stop.sh can fail with "No devices found")
mkdir -p "$ROOT/run/scan"; cd "$ROOT/run/scan"
cp "$CFG" config.yaml
"$ROOT/build/nrscope/src/nrscan"
