#!/usr/bin/env bash
# Pre-submission checks (plan T11). Run: bash tools/presubmit.sh
# Prints one line per check and ends with PASS, or FAIL: <check> at the first failure.
# Manual items it cannot check (master-reviewer PASS, Codex review, the public-URL
# clone build) are listed at the end of a PASS.
set -u
cd "$(dirname "$0")/.." || exit 2
ROOT="$(pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
IMAGE="${IMAGE:-fog-of-war}"
SAMPLE="${SAMPLE:-$(cd "$ROOT/../kits/rwx/x/sample" && pwd)}"
DATA="${BR_DATASETS:-$ROOT/../rwx-data}"
TMPD="$(mktemp -d "${TMPDIR:-/tmp}/presubmit.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT

fail() { echo "FAIL: $1"; [ -n "${2:-}" ] && echo "$2"; exit 1; }
ok() { echo "ok   $1"; }

# 1. clean tree, committed HEAD
[ -z "$(git status --porcelain --untracked-files=all)" ] || fail "working tree not clean" "$(git status --short)"
ok "working tree clean at $(git rev-parse --short HEAD)"

# 2. repo hygiene: size, modes, dashes, attribution, private data
SIZE_KB=$(du -sk .git | cut -f1)
FILES_KB=$(git ls-files -z | xargs -0 du -ck | tail -1 | cut -f1)
[ $((SIZE_KB + FILES_KB)) -lt 20000 ] || fail "repo size over 20 MB (.git ${SIZE_KB} KB + files ${FILES_KB} KB)"
ok "repo size .git ${SIZE_KB} KB + files ${FILES_KB} KB (limit 20000 KB)"
BADMODE=$(git ls-files -s | awk '$1 != "100644" {print $4 " " $1}')
[ -z "$BADMODE" ] || fail "committed file modes not 644" "$BADMODE"
ok "all committed files mode 644"
DASH=$(git grep -n -P "\x{2013}|\x{2014}" || true)
[ -z "$DASH" ] || fail "em or en dash in a tracked file" "$DASH"
ok "no em or en dashes"
PRIV=$(git ls-files | grep -E '(^|/)(rwx-data|output.*\.jsonl|answers\.json|\.venv)(/|$)|\.coverage' || true)
[ -z "$PRIV" ] || fail "private or generated files are tracked" "$PRIV"
ok "no private data or generated files tracked"
ATTR=$(git log --format=%B | grep -i -E 'co-authored-by|generated with' || true)
[ -z "$ATTR" ] || fail "attribution line in commit history" "$ATTR"
ok "no attribution lines in commits"

# 3. our unit tests (zero tests is a failure)
"$PY" -m pytest tests_sel -q -o addopts="" > "$TMPD/unit.log" 2>&1
RC=$?
tail -1 "$TMPD/unit.log"
[ $RC -eq 0 ] || fail "unit tests (exit $RC)" "$(tail -15 "$TMPD/unit.log")"
grep -E '^[0-9]+ passed' "$TMPD/unit.log" > /dev/null || fail "unit tests ran zero tests"
ok "unit tests"

# 4. synthetic sets: 0 misses where the data exists
for SET in synth-holdout stress; do
  if [ -f "$DATA/$SET/answers.json" ]; then
    "$PY" tools/eval.py --set "$SET" > "$TMPD/$SET.log" 2>&1
    RC=$?
    [ $RC -eq 0 ] || fail "eval $SET (exit $RC)" "$(head -12 "$TMPD/$SET.log")"
    ok "eval $SET: $(grep '^Simulated' "$TMPD/$SET.log"), $(grep '^Misses' "$TMPD/$SET.log")"
  else
    echo "skip eval $SET (no labelled data yet at $DATA/$SET)"
  fi
done

# 5. build, then the sample in Docker under the kit flags 3 times
docker build --platform linux/amd64 --tag "$IMAGE" . > "$TMPD/build.log" 2>&1 || fail "docker build" "$(tail -15 "$TMPD/build.log")"
ok "docker build (amd64)"
FIRST=""
for I in 1 2 3; do
  make -s sample SAMPLE="$SAMPLE" IMAGE="$IMAGE" > "$TMPD/sample$I.log" 2>&1 || fail "make sample run $I" "$(cat "$TMPD/sample$I.log")"
  grep -q '^Rejected' "$TMPD/sample$I.log" && fail "make sample run $I rejected" "$(cat "$TMPD/sample$I.log")"
  grep -q '^Misses:    0 ' "$TMPD/sample$I.log" || fail "make sample run $I has misses" "$(cat "$TMPD/sample$I.log")"
  SIM=$(grep '^Simulated' "$TMPD/sample$I.log")
  [ -z "$FIRST" ] && FIRST="$SIM"
  [ "$SIM" = "$FIRST" ] || fail "make sample run $I differs from run 1 ($SIM vs $FIRST)"
done
ok "make sample x3: $FIRST, 0 misses, no rejection"

# 6. CPU inside the container (emulated on Apple Silicon, so an upper bound) under 1 s
"$PY" tools/eval.py --set sample --docker --image "$IMAGE" > "$TMPD/dock.log" 2>&1 || fail "eval --docker" "$(cat "$TMPD/dock.log")"
CPU=$(grep -E '^CPU \(container' "$TMPD/dock.log" | sed -E 's/.*: ([0-9.]+) s$/\1/')
[ -n "$CPU" ] || fail "no CPU reading from the container" "$(tail -5 "$TMPD/dock.log")"
awk -v c="$CPU" 'BEGIN{exit !(c < 1.0)}' || fail "container CPU $CPU s is not under 1 s"
ok "container CPU $CPU s (< 1 s)"
SIMD=$(grep '^Simulated' "$TMPD/dock.log")
[ "$SIMD" = "$FIRST" ] || fail "eval --docker ($SIMD) disagrees with make sample ($FIRST)"
ok "eval --docker agrees with make sample: $SIMD"

# 7. fresh clone of HEAD builds and scores the same
git clone -q "$ROOT" "$TMPD/clone" || fail "git clone of HEAD"
docker build -q --platform linux/amd64 --tag "${IMAGE}-fresh" "$TMPD/clone" > "$TMPD/fresh.log" 2>&1 || fail "fresh clone docker build" "$(tail -15 "$TMPD/fresh.log")"
make -s -C "$ROOT" sample SAMPLE="$SAMPLE" IMAGE="${IMAGE}-fresh" > "$TMPD/fresh_sample.log" 2>&1 || fail "fresh clone sample run" "$(cat "$TMPD/fresh_sample.log")"
FRESH=$(grep '^Simulated' "$TMPD/fresh_sample.log")
[ "$FRESH" = "$FIRST" ] || fail "fresh clone scores differently ($FRESH vs $FIRST)"
ok "fresh clone of HEAD builds and scores the same: $FRESH"

echo "manual before pushing: master-reviewer PASS on the diff; Codex read-only review; build from the public URL after the push."
echo "PASS"
