#!/bin/bash
# Restart the viewer on another cell from config/cells.json (used by the viewer's cell dropdown, also fine by hand).
#   usage: scripts/switch.sh <cell id>        e.g. scripts/switch.sh n41_pci610
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ID="$1"
read -r CFG META < <(python3 - "$ROOT" "$ID" <<'PY'
import json, os, sys
root, cid = sys.argv[1:3]
for c in json.load(open(os.path.join(root, 'config/cells.json'))):
    if c['id'] == cid:
        print(os.path.join(root, c['config']), os.path.join(root, c['meta'])); break
PY
)
if [ -z "$CFG" ]; then echo "unknown cell '$ID' (see config/cells.json)" >&2; exit 1; fi
mkdir -p "$ROOT/run"; date +%s > "$ROOT/run/switching"
echo "$(date +%T) switching to $ID: $CFG $META"
export META
"$ROOT/scripts/start.sh" "$CFG"
rm -f "$ROOT/run/switching"
