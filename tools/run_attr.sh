#!/bin/bash
# Laptop only. Records which class attributes each test reads (tools/attr_site, tools/attrfork).
#   bash tools/run_attr.sh <sqlglot checkout at the pinned SHA> <outdir> [workers] [pytest args or test ids ...]
# PY is a python with tools/requirements.lock and tools/requirements-maps.txt installed (default .venv/bin/python).
# Result: <outdir>/attr.<pid>.log files; python tools/build_attrmap.py build --run <outdir> ... turns them into maps/attrreads.bin.
set -u
SRC=$(cd "$1" && pwd); OUT=$2; NW=${3:-3}
shift 3 2>/dev/null || shift $#
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-$HERE/.venv/bin/python}
BIN=$(dirname "$PY")
SEED=${SEED:-0}
mkdir -p "$OUT"
"$PY" "$HERE/tools/attr_names.py" "$SRC" > "$OUT/names.json"
cd "$SRC"
ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ]; then ARGS=(tests); fi
SKIP_INTEGRATION=0 ATTR_OUT="$OUT" ATTR_SRC="$SRC" ATTR_NAMES="$OUT/names.json" PATH="$BIN:$PATH" \
  PYTHONPATH="$HERE/tools/attr_site:$SRC:$HERE" PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=$SEED \
  "$PY" -m pytest -n "$NW" -p no:cacheprovider -o addopts="" --assert=plain --timeout 900 \
  --junitxml="$OUT/junit.xml" -p tools.attrfork -p tools.preimport "${ARGS[@]}" > "$OUT/pytest.log" 2>&1
echo "exit $?" > "$OUT/status"
