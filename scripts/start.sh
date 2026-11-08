#!/bin/bash
# Start the PRB visualizer: web server (ZeroMQ SUB) + nrscope (ZeroMQ PUB).
#   usage: scripts/start.sh [config.yaml]        (default: config/testbed_n78.yaml)
#   env:   ZMQ_ENDPOINT (default tcp://127.0.0.1:5556)  HTTP (default 0.0.0.0:8080)  META (default server/meta.json)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${1:-$ROOT/config/testbed_n78.yaml}"
ZMQ_ENDPOINT="${ZMQ_ENDPOINT:-tcp://127.0.0.1:5556}"
HTTP="${HTTP:-0.0.0.0:8080}"
META="${META:-$ROOT/server/meta.json}"
SSBPRB=$(python3 -c "import json,sys;s=json.load(open(sys.argv[1])).get('ssb',{});print('%d,%d'%(s['prb_lo'],s['prb_hi']))" "$META" 2>/dev/null)
RUN="$ROOT/run"; mkdir -p "$RUN/logs"
"$ROOT/scripts/stop.sh" >/dev/null 2>&1
cp "$CFG" "$RUN/config.yaml"                       # nrscope reads ./config.yaml
cp -n "$ROOT/build/nrscope/src/to_google.py" "$RUN/" 2>/dev/null
cd "$RUN"
nohup setsid python3 "$ROOT/server/server.py" --zmq "$ZMQ_ENDPOINT" --http "$HTTP" --meta "$META" \
      > logs/server.log 2>&1 < /dev/null &
echo $! > server.pid
sleep 1
NRSCOPE_PBCHC=1 NRSCOPE_SYM=1 NRSCOPE_ZMQ="$ZMQ_ENDPOINT" NRSCOPE_PRB_CSV=0 NRSCOPE_BIN="$ROOT/build/nrscope/src/nrscope" \
      nohup setsid "$ROOT/scripts/nrscope_loop.sh" > logs/nrscope.log 2>&1 < /dev/null &
echo $! > nrscope.pid
echo "started: server pid $(cat server.pid), nrscope pid $(cat nrscope.pid); open http://$(hostname -I | awk '{print $1}'):${HTTP##*:}/"
