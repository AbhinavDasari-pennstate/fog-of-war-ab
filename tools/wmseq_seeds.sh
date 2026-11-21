#!/bin/bash
# Laptop only. Harness C for several seeds, one after the other.
#   bash tools/wmseq_seeds.sh <src> <base dir (new)> <tests.json> <seed> [<seed> ...]   -> <base dir>/c<seed>/
set -u
SRC=$1; BASE=$2; TESTS=$3
shift 3
HERE=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$BASE" || exit 2
for SEED in "$@"; do
  bash "$HERE/tools/run_wmseq.sh" "$SRC" "$SEED" "$BASE/c$SEED" "$BASE/cscr$SEED" "$TESTS"
  echo "seed $SEED $(cat "$BASE/c$SEED/status" 2>/dev/null) $(date +%H:%M:%S)" >> "$BASE/chain.log"
done
echo done >> "$BASE/chain.log"
