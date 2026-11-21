#!/bin/bash
# Laptop only. Fork-per-test coverage run (tools/forkrun.py) whose parent first ran the state-clearing tests (MAPS_PRE).
#   bash tools/run_forkpre.sh <src> <outdir (new)> [workers]
set -u
SRC=$(cd "$1" && pwd); OUT=$2; NW=${3:-3}
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-$HERE/.venv/bin/python}
BIN=$(dirname "$PY")
mkdir "$OUT" || exit 2
cd "$SRC"
SKIP_INTEGRATION=0 COVERAGE_CORE=ctrace MAPS_ROOT="$SRC" MAPS_OUT="$OUT" MAPS_FORK=1 MAPS_PRE=tests/test_dialect_entry_points.py PATH="$BIN:$PATH" \
  PYTHONPATH="$SRC:$HERE" PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
  "$PY" -m pytest -n "$NW" -p no:cacheprovider -o addopts="" --timeout 900 --continue-on-collection-errors --junitxml="$OUT/junit.xml" \
  -p tools.covctx -p tools.preimport -p tools.readaudit -p tools.classuse -p tools.forkrun tests > "$OUT/pytest.log" 2>&1
echo "exit $?" > "$OUT/status"
