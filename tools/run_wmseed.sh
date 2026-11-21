#!/bin/bash
# Laptop only. One recording seed of the weak-mutation map for the whole suite (tools/wmfork.py).
#   bash tools/run_wmseed.sh <src> <seed> <res dir (new, empty)> <scratch dir (new)> <tests.json> [workers] [pytest args...]   (TARGET=<path> limits the run)
# <src> is a clean checkout of sqlglot at the pinned SHA. Results: <res dir>/<test number>.mar, log: <res dir>/pytest.log
set -u
SRC=$(cd "$1" && pwd); SEED=$2; RES=$3; SCR=$4; TESTS=$5; NW=${6:-3}
shift 6
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-$HERE/.venv/bin/python}
BIN=$(dirname "$PY")
mkdir "$RES" "$SCR" || exit 2
cd "$SRC"
SKIP_INTEGRATION=0 PATH="$BIN:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=$SEED PYTHON_CPU_COUNT=2 \
  WM_OUT="$SCR" WM_SRC="$SRC" WM_TOOLS="$HERE" WM_RES="$RES" WM_TESTS="$TESTS" \
  PYTHONPATH="$HERE/tools/weakmut_site:$SRC:$HERE" \
  "$PY" -m pytest -n "$NW" -p no:cacheprovider -o addopts="" --timeout 1500 \
  -p tools.wmfork "$@" ${TARGET:-tests} > "$RES/pytest.log" 2>&1
echo "exit $?" > "$RES/status"
