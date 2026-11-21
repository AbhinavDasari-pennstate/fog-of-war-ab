#!/bin/bash
# Laptop only. Harness C of the weak-mutation map: the whole suite in one process, in order, no fork (tools/wmseq.py).
#   bash tools/run_wmseq.sh <src> <seed> <res dir (new)> <scratch dir (new)> <tests.json> [pytest args...]   (TARGET=<path> limits the run)
set -u
SRC=$(cd "$1" && pwd); SEED=$2; RES=$3; SCR=$4; TESTS=$5
shift 5
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-$HERE/.venv/bin/python}
BIN=$(dirname "$PY")
mkdir "$RES" "$SCR" || exit 2
cd "$SRC"
SKIP_INTEGRATION=0 PATH="$BIN:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=$SEED PYTHON_CPU_COUNT=2 \
  WM_OUT="$SCR" WM_SRC="$SRC" WM_TOOLS="$HERE" WM_RES="$RES" WM_TESTS="$TESTS" \
  PYTHONPATH="$HERE/tools/weakmut_site:$SRC:$HERE" \
  "$PY" -m pytest -n 0 -p no:cacheprovider -o addopts="" --timeout 1500 --continue-on-collection-errors \
  -p tools.wmseq "$@" ${TARGET:-tests} > "$RES/pytest.log" 2>&1
echo "exit $?" > "$RES/status"
