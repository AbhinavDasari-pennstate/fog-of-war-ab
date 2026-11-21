#!/bin/bash
# Laptop only. Second recording harness: one pytest run per test file (the parent collects only that file),
# so a lazily loaded module or cache is built, and its sites recorded, by every test that needs it, as when
# each test runs alone in a fresh process. Complements tools/run_wmseed.sh (whole suite collected first).
#   bash tools/wm_perfile.sh <src> <base dir (new)> <tests.json> <workers> <seed> [<seed> ...]
# Results: <base dir>/p<seed>/<test number>.mar ; logs: <base dir>/p<seed>/log.<k>
set -u
SRC=$(cd "$1" && pwd); BASE=$2; TESTS=$3; NW=$4
shift 4
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-$HERE/.venv/bin/python}
BIN=$(dirname "$PY")
mkdir -p "$BASE" || exit 2
FILES=$("$PY" -c "import json,sys; print('\n'.join(sorted({t['file'] for t in json.load(open(sys.argv[1]))['tests']})))" "$TESTS")
for SEED in "$@"; do
  RES="$BASE/p$SEED"; SCR="$BASE/pscr$SEED"
  mkdir "$RES" "$SCR" || exit 2
  cd "$SRC" || exit 2
  K=0
  for F in $FILES; do
    K=$((K+1))
    SKIP_INTEGRATION=0 PATH="$BIN:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=$SEED PYTHON_CPU_COUNT=2 \
      WM_OUT="$SCR" WM_SRC="$SRC" WM_TOOLS="$HERE" WM_RES="$RES" WM_TESTS="$TESTS" \
      PYTHONPATH="$HERE/tools/weakmut_site:$SRC:$HERE" \
      "$PY" -m pytest -n "$NW" -p no:cacheprovider -o addopts="" --timeout 1500 -p tools.wmfork "$F" > "$RES/log.$K" 2>&1
    echo "$F exit $?" >> "$RES/status"
  done
  echo "seed $SEED done $(date +%H:%M:%S)" >> "$BASE/chain.log"
done
echo done >> "$BASE/chain.log"
