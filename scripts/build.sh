#!/bin/bash
# Configure + build nrscope/nrscan out-of-tree into prbviz/build
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/build"; cd "$ROOT/build"
cmake "$ROOT/src" -DENABLE_SRSEPC=OFF -DENABLE_GUI=ON -DENABLE_ALL_TEST=OFF
make -j"${JOBS:-16}" nrscope nrscan
echo BUILD_DONE
