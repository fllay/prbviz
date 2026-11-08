#!/bin/bash
# Stop nrscope and the web server started by start.sh.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/run" 2>/dev/null || exit 0
for n in nrscope server; do
  if [ -f $n.pid ]; then kill "$(cat $n.pid)" 2>/dev/null; rm -f $n.pid; fi
done
pkill -f nrscope_loop.sh 2>/dev/null
sleep 1
pkill -f "$ROOT/build/nrscope/src/nrscope" 2>/dev/null
pkill -f "$ROOT/server/server.py" 2>/dev/null
echo stopped
