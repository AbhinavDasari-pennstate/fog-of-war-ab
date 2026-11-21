#!/bin/bash
# Laptop only. Runs the suite once, either instrumented (map build) or plain (flakiness check).
#   bash tools/run_instrumented.sh instr <src> <outdir> [workers]    instrumented run into <outdir>
#   bash tools/run_instrumented.sh plain <src> <junit.xml> [workers] plain run, junit written to the file
# <src> is a checkout of sqlglot at the pinned SHA; PY is a python with tools/requirements.lock and
# tools/requirements-maps.txt installed (default .venv/bin/python of this repo or the sibling checkout).
set -u
MODE=$1; SRC=$(cd "$2" && pwd); OUT=$3; NW=${4:-6}
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-$HERE/.venv/bin/python}
BIN=$(dirname "$PY")
cd "$SRC"
if [ "$MODE" = instr ]; then
  rm -rf "$OUT"; mkdir -p "$OUT"
  SKIP_INTEGRATION=0 COVERAGE_CORE=ctrace MAPS_ROOT="$SRC" MAPS_OUT="$OUT" PATH="$BIN:$PATH" \
    PYTHONPATH="$SRC:$HERE" PYTHONDONTWRITEBYTECODE=1 \
    "$PY" -m pytest -n "$NW" -p no:cacheprovider -o addopts="" --timeout 900 --junitxml="$OUT/junit.xml" \
    -p tools.covctx -p tools.preimport -p tools.readaudit -p tools.classuse tests > "$OUT/pytest.log" 2>&1
  echo "exit $?" > "$OUT/status"
else
  SKIP_INTEGRATION=0 PATH="$BIN:$PATH" PYTHONPATH="$SRC" PYTHONDONTWRITEBYTECODE=1 \
    "$PY" -m pytest -n "$NW" -p no:cacheprovider -o addopts="" --timeout 300 --junitxml="$OUT" tests
fi
