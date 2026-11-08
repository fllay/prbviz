#!/bin/bash
# Restart the viewer on another cell (used by the viewer's cell dropdown and Scan panel, also fine by hand).
#   scripts/switch.sh <cell id>                              a cell of config/cells.json, e.g. n41_pci610
#   scripts/switch.sh --custom <id> <config> <meta> [name]   a cell found by the scan (files made by the server)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$1" = "--custom" ]; then
  ID="$2"; CFG="$3"; META="$4"; NAME="$5"
  case "$ID" in (*[!A-Za-z0-9_]*|"") echo "bad id" >&2; exit 1;; esac
else
  ID="$1"
  read -r CFG META < <(python3 - "$ROOT" "$ID" <<'PY'
import json, os, sys
root, cid = sys.argv[1:3]
for c in json.load(open(os.path.join(root, 'config/cells.json'))):
    if c['id'] == cid:
        print(os.path.join(root, c['config']), os.path.join(root, c['meta'])); break
PY
)
  NAME=""
fi
if [ -z "$CFG" ] || [ ! -f "$CFG" ] || [ ! -f "$META" ]; then echo "unknown cell '$ID' (see config/cells.json)" >&2; exit 1; fi
mkdir -p "$ROOT/run"; date +%s > "$ROOT/run/switching"
echo "$(date +%T) switching to $ID: $CFG $META"
export META CELL_ID="$ID" CELL_NAME="$NAME"
"$ROOT/scripts/start.sh" "$CFG"
rm -f "$ROOT/run/switching"
