#!/bin/bash
# Laptop only. Record several seeds one after the other (one seed at a time).
#   bash tools/wm_seeds.sh <src> <base dir (new)> <tests.json> <workers> <seed> [<seed> ...]
# Writes <base dir>/s<seed>/ (results) and <base dir>/scr<seed>/ (scratch) for each seed.
set -u
SRC=$1; BASE=$2; TESTS=$3; NW=$4
shift 4
HERE=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$BASE" || exit 2
for SEED in "$@"; do
  bash "$HERE/tools/run_wmseed.sh" "$SRC" "$SEED" "$BASE/s$SEED" "$BASE/scr$SEED" "$TESTS" "$NW"
  echo "seed $SEED $(cat "$BASE/s$SEED/status" 2>/dev/null) $(date +%H:%M:%S)" >> "$BASE/chain.log"
done
echo done >> "$BASE/chain.log"
