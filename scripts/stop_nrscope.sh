#!/bin/bash
# Stop nrscope only (the web server keeps running); used by the viewer's scan job. stop.sh stops both.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/run" 2>/dev/null || exit 0
if [ -f nrscope.pid ]; then kill "$(cat nrscope.pid)" 2>/dev/null; rm -f nrscope.pid; fi
pkill -f nrscope_loop.sh 2>/dev/null
sleep 1
pkill -f "$ROOT/build/nrscope/src/nrscope" 2>/dev/null
echo stopped nrscope
