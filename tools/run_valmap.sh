#!/bin/bash
# Laptop only. Records which dict-constant ENTRY VALUES each test reads (tools/valmap_site, tools/vmfork).
#   bash tools/run_valmap.sh <sqlglot checkout at the pinned SHA> <outdir> [workers] [pytest args or test ids ...]
# PY is a python with tools/requirements.lock and tools/requirements-maps.txt installed (default .venv/bin/python).
# Result: <outdir>/vm.<pid>.log files; python tools/build_valmap.py build --run <outdir> ... turns them into maps/valreads.bin.
set -u
SRC=$(cd "$1" && pwd); OUT=$2; NW=${3:-3}
shift 3 2>/dev/null || shift $#
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-$HERE/.venv/bin/python}
BIN=$(dirname "$PY")
SEED=${SEED:-0}
mkdir -p "$OUT"
cd "$SRC"
ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ]; then ARGS=(tests); fi
SKIP_INTEGRATION=0 VM_OUT="$OUT" VM_SRC="$SRC" PATH="$BIN:$PATH" \
  PYTHONPATH="$HERE/tools/valmap_site:$SRC:$HERE" PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=$SEED \
  "$PY" -m pytest -n "$NW" -p no:cacheprovider -o addopts="" --assert=plain --timeout 900 \
  --junitxml="$OUT/junit.xml" -p tools.vmfork -p tools.preimport "${ARGS[@]}" > "$OUT/pytest.log" 2>&1
echo "exit $?" > "$OUT/status"
