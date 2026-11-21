"""v0 selector: provable no-ops answer [], everything else runs the whole suite packed.

A commit answers [] only when every changed file is a modified .py file whose AST
(docstrings included) is unchanged, outside sqlglot/typing/ and sqlglotc/, whose
text is read by tests. Any doubt (parse failure, patch that does not apply, other
file types) runs every test over the best worker count.

stdout carries answer lines only; reasons go to stderr.
"""

import ast
import json
import os
import re
import subprocess
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import diffparse  # noqa: E402
import pack  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/app/sqlglot-src"))
SOURCE_READERS = ("sqlglot/typing/", "sqlglotc/")
CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def _cookie(text: str) -> list[str | None]:
    """The PEP 263 encoding declaration on each of the first two lines, if any."""
    out: list[str | None] = []
    for line in text.split("\n")[:2]:
        m = CODING.match(line)
        out.append(m.group(1) if m else None)
    return out


def _is_noop(change: "diffparse.FileChange", src_root: Path) -> tuple[bool, str]:
    path = change.path_new
    if change.status != "modified" or change.path_old != path:
        return False, f"{path}: {change.status}"
    if not path.endswith(".py"):
        return False, f"{path}: not python"
    if path.startswith(SOURCE_READERS):
        return False, f"{path}: source read by tests"
    if any("\r" in text for h in change.hunks for _, text in h.lines):
        return False, f"{path}: carriage return in a changed hunk"
    old = (src_root / path).read_bytes().decode("utf-8")
    new = diffparse.apply(old, change)
    if _cookie(old) != _cookie(new):
        return False, f"{path}: encoding declaration changed"
    same = ast.dump(ast.parse(old, type_comments=True)) == ast.dump(
        ast.parse(new, type_comments=True)
    )
    return same, f"{path}: ast {'identical' if same else 'changed'}"


_FULL: dict[int, list[list[int]]] = {}


def _full(tests: list[dict]) -> list[list[int]]:
    """Every test packed; computed once per tests list."""
    durations = {t["number"]: round(t["seconds"] * 1000 + 1e-9) for t in tests}
    key = hash(tuple(durations.items()))
    if key not in _FULL:
        _FULL[key] = pack.pack(durations)
    return [list(w) for w in _FULL[key]]


def _pack(selected, tests: list[dict]) -> list[list[int]]:
    dur = {t["number"]: round(t["seconds"] * 1000 + 1e-9) for t in tests}
    return pack.pack({n: dur[n] for n in selected if n in dur})


def decide(patch_text: str, src_root: Path, tests: list[dict], maps=None) -> list[list[int]]:
    """Workers for one commit. With maps: rules R0 to R9; without: the v0 rule."""
    if maps is not None:
        import rules

        try:
            changes = diffparse.parse(patch_text)
            chosen, why = rules.select(changes, maps, tests, src_root=src_root)
            print(f"  {why}", file=sys.stderr)
            return _pack(chosen, tests)
        except Exception as exc:  # R0: anything unparsable means the widest answer
            print(f"  fallback to all tests: {type(exc).__name__}: {exc}", file=sys.stderr)
            return _full(tests)
    try:
        changes = diffparse.parse(patch_text)
        for change in changes:
            ok, why = _is_noop(change, src_root)
            print(f"  {why}", file=sys.stderr)
            if not ok:
                return _full(tests)
        return []
    except Exception as exc:  # any doubt means the widest answer
        print(f"  fallback to all tests: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _full(tests)


def _load_maps(tests: list[dict]):
    """The committed maps, or None (v0 rule) when missing or when test ids disagree with tests.json."""
    try:
        root = Path(os.environ.get("BR_MAPS", str(Path(__file__).resolve().parent.parent / "maps")))
        sys.path.insert(0, str(root.parent))
        from maps.load import load

        maps = load(str(root))
        for t in tests:
            if maps.ids[t["number"]] != t["id"]:
                print("map ids differ from tests.json: v0 rule", file=sys.stderr)
                return None
        return maps
    except Exception as exc:
        print(f"maps unavailable ({type(exc).__name__}: {exc}): v0 rule", file=sys.stderr)
        return None


TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
# Run-time replay budgets (3 min batch limit, CPU is scored). The budget is spent in ESTIMATED seconds
# (the map's build-time measurements times a slack), cheapest replay first, so the same batch always
# gets the same decisions whatever the machine load. The wall clock is only an emergency stop.
REPLAY_EST_BUDGET_S = float(os.environ.get("BR_REPLAY_EST", "150"))
REPLAY_EMERGENCY_WALL_S = float(os.environ.get("BR_REPLAY_WALL", "140"))
REPLAY_PER_COMMIT_S = float(os.environ.get("BR_REPLAY_COMMIT", "40"))
# The replay must cost clearly less than the giant it replaces (build-machine seconds x slack).
# A replay is run only when its estimated CPU is below this share of the simulated seconds the drop
# saves (a replay that finds a failure costs less than its estimate, one that passes costs about it).
REPLAY_WORTH = 0.8
REPLAY_SLACK = 1.15
REPLAY_OVERHEAD_S = 1.0


def _cost(workers, dur) -> int:
    """The scorer's cost of one commit in ms: busiest worker plus 2 s per worker."""
    ws = [w for w in workers if w]
    if not ws:
        return 0
    return max(sum(dur[n] for n in w) for w in ws) + 2000 * len(ws)


def _tpcds_candidate(patch_text: str, src_root: Path, tests: list[dict], maps, chosen, tp):
    """(Scope, patched files) when the TPC-DS giant is in `chosen` only for changes the replay can judge."""
    import rules
    import tpcds_scope

    num = next((t["number"] for t in tests if t["id"] == TPCDS_ID), None)
    if num is None or num not in chosen or tp is None:
        return None
    changes = diffparse.parse(patch_text)
    ctx = rules.Ctx(maps, tests, src_root)
    per_file, patched = [], {}
    for ch in changes:
        try:
            got, _ = rules._one(ctx, ch)
            depends = bool(got >> num & 1)
        except Exception:  # the rules widen on any doubt
            depends = True
        per_file.append((ch, depends))
        if ch.status == "modified" and ch.path_new == ch.path_old and ch.path_new.startswith("sqlglot/") and ch.path_new.endswith(".py"):
            old = (src_root / ch.path_new).read_bytes().decode("utf-8")
            patched[ch.path_new] = diffparse.apply(old, ch)
    sc = tpcds_scope.scope(per_file, tp, src_root)
    return num, sc, patched


def _run_replay(plan, patched, src_root: Path, timeout: float, seed: int):
    """Spawn the replay child; True only when it reports every replayed query identical."""
    job = {"src": str(src_root), "patched": patched, "plan": {str(q): st for q, st in plan.items()},
           "map": str(MAPS_ROOT / "tpcds.z")}
    env = {"PYTHONHASHSEED": str(seed), "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"}
    try:
        r = subprocess.run([sys.executable, "-S", "-E", str(Path(__file__).resolve().parent / "tpcds_replay.py")],
                           input=json.dumps(job).encode(), capture_output=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "replay timed out"
    try:
        out = json.loads(r.stdout.decode().strip().splitlines()[-1])
        return bool(out["same"]), out["why"]
    except Exception:
        return False, f"replay child failed (exit {r.returncode})"


MAPS_ROOT = Path(os.environ.get("BR_MAPS", str(Path(__file__).resolve().parent.parent / "maps")))


def _load_tpcds():
    try:
        import tpcds_scope

        return tpcds_scope.TpcdsMap.load(MAPS_ROOT / "tpcds.z")
    except Exception as exc:
        print(f"tpcds map unavailable ({type(exc).__name__}: {exc}): giant stays", file=sys.stderr)
        return None


def main(data: Path = DATA, src_root: Path = SRC) -> None:
    tests = json.loads((data / "tests.json").read_text(encoding="utf-8"))["tests"]
    maps = _load_maps(tests)
    tp = _load_tpcds() if maps is not None and os.environ.get("BR_NO_REPLAY") != "1" else None
    dur = {t["number"]: round(t["seconds"] * 1000 + 1e-9) for t in tests}
    rows: dict[str, list] = {}
    pending = []  # (estimated cost, commit, num, scope, patched, chosen)
    for patch in sorted((data / "commits").glob("*.patch")):
        print(f"{patch.stem}:", file=sys.stderr)
        text = None
        try:
            text = patch.read_bytes().decode("utf-8")
            workers = decide(text, src_root, tests, maps)
        except Exception as exc:  # one full-suite line per commit, whatever happens
            print(f"  fallback to all tests: {type(exc).__name__}: {exc}", file=sys.stderr)
            workers = _full(tests)
        rows[patch.stem] = workers
        if tp is not None and workers and text is not None:
            try:
                chosen = {n for w in workers for n in w}
                cand = _tpcds_candidate(text, src_root, tests, maps, chosen, tp)
                if cand is not None:
                    num, sc, patched = cand
                    print(f"  tpcds: {sc.kind} {sc.why[:200]}", file=sys.stderr)
                    if sc.kind == "replay":
                        est = tp.cost(sc.plan) * REPLAY_SLACK + (REPLAY_OVERHEAD_S if sc.plan else 0.0)
                        pending.append((est, patch.stem, num, sc, patched, chosen))
            except Exception as exc:
                print(f"  tpcds refine failed ({type(exc).__name__}: {exc}): giant stays", file=sys.stderr)
    # replays, cheapest first, inside the budgets; two at a time (the container has 2 cores). Which
    # replays are submitted depends only on the estimates, so the same batch gets the same decisions.
    wall0 = time.monotonic()
    spent = 0.0
    import resource
    from concurrent.futures import ThreadPoolExecutor

    base_cpu = lambda: (lambda r: r.ru_utime + r.ru_stime)(resource.getrusage(resource.RUSAGE_CHILDREN))  # noqa: E731
    pending.sort(key=lambda x: (x[0], x[1]))  # cheapest first: the budget buys the most drops
    jobs = []  # (commit, num, chosen, future, est)

    def job(sc, patched, seed, limit):
        w0 = time.monotonic()
        ok, why = _run_replay(sc.plan, patched, src_root, limit, seed)
        return ok, why + f"; {len(sc.plan)} queries, est {{est:.1f}}s, wall {time.monotonic() - w0:.1f}s"

    with ThreadPoolExecutor(max_workers=2) as pool:
        for est, commit, num, sc, patched, chosen in pending:
            gain = (_cost(_pack(chosen, tests), dur) - _cost(_pack(chosen - {num}, tests), dur)) / 1000.0
            if est > gain * REPLAY_WORTH:
                print(f"{commit}: tpcds replay est {est:.1f}s vs giant {gain:.1f}s: giant stays", file=sys.stderr)
                continue
            if not sc.plan:
                rows[commit] = _pack(chosen - {num}, tests)
                print(f"{commit}: tpcds replay identical: giant dropped (no query reaches the changed code)", file=sys.stderr)
                continue
            if spent + est > REPLAY_EST_BUDGET_S:
                print(f"{commit}: tpcds replay estimate budget spent: giant stays", file=sys.stderr)
                continue
            left_wall = REPLAY_EMERGENCY_WALL_S - (time.monotonic() - wall0)
            if left_wall < 5:
                print(f"{commit}: tpcds replay emergency wall stop: giant stays", file=sys.stderr)
                continue
            spent += est
            seed = zlib.crc32(commit.encode()) % 8
            jobs.append((commit, num, chosen, pool.submit(job, sc, patched, seed, min(REPLAY_PER_COMMIT_S, left_wall)), est))
        for commit, num, chosen, fut, est in jobs:
            ok, why = fut.result()
            why = why.replace("{est:.1f}", f"{est:.1f}")
            print(f"{commit}: tpcds replay {'identical: giant dropped' if ok else 'giant stays'} ({why})", file=sys.stderr)
            if ok:
                rows[commit] = _pack(chosen - {num}, tests)
    for commit in sorted(rows):
        print(json.dumps({"commit": commit, "workers": rows[commit]}))
    print(f"v0 done: {len(tests)} tests known", file=sys.stderr)


if __name__ == "__main__":
    main()
