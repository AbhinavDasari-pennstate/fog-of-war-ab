#!/bin/bash
# Laptop only. Sequential coverage pass for the line maps: the whole suite in ONE process, in collection order,
# no fork, no preimport (like the labeller), per-test coverage contexts. A test that runs after a state-clearing
# test (tests/test_dialect_entry_points.py clears Dialect._classes) then sees, and is credited with, the
# lazily re-run code that the forked and pre-imported runs never execute.
#   bash tools/run_seqcov.sh <src> <outdir (new)> [seed]
set -u
SRC=$(cd "$1" && pwd); OUT=$2; SEED=${3:-0}
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-$HERE/.venv/bin/python}
BIN=$(dirname "$PY")
mkdir "$OUT" || exit 2
cd "$SRC"
SKIP_INTEGRATION=0 COVERAGE_CORE=ctrace MAPS_ROOT="$SRC" MAPS_OUT="$OUT" PATH="$BIN:$PATH" \
  PYTHONPATH="$SRC:$HERE" PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=$SEED \
  "$PY" -m pytest -n 0 -p no:cacheprovider -o addopts="" --timeout 900 --continue-on-collection-errors --junitxml="$OUT/junit.xml" \
  -p tools.covctx -p tools.readaudit -p tools.classuse tests > "$OUT/pytest.log" 2>&1
echo "exit $?" > "$OUT/status"
