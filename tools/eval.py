#!/usr/bin/env python3
"""Evaluation harness: run a selector on a commit set and score it with the kit's score.py.

    python3 tools/eval.py --set sample|synth-dev|synth-holdout|stress [--docker] [--selector PATH]

Cost comes from vendor/score.py (the kit's scorer, unchanged); this file never
re-implements it. Exit code 1 on any miss for holdout and stress sets, 2 on a
rejected output.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import resource
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "selector"))
import pack  # noqa: E402


def _load_scorer():
    spec = importlib.util.spec_from_file_location("kit_score", ROOT / "vendor" / "score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = _load_scorer()

DEFAULT_SAMPLE = Path(os.environ.get("BR_SAMPLE", str(ROOT.parent / "kits" / "rwx" / "x" / "sample")))
DATA_ROOT = Path(os.environ.get("BR_DATASETS", str(ROOT.parent / "rwx-data")))
DEFAULT_SRC = Path(os.environ.get("BR_SRC", str(ROOT.parent / "sqlglot-pin")))
DOCKER_FLAGS = [
    "--rm", "--platform", "linux/amd64", "--network", "none", "--cpus", "2",
    "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256", "--read-only",
    "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp",
]


@dataclass
class Dataset:
    name: str
    data_dir: Path
    tests: list[dict]
    answers: dict


def load_set(name: str, sample_dir: Path | None = None) -> Dataset:
    d = (sample_dir or DEFAULT_SAMPLE) if name == "sample" else DATA_ROOT / name
    tests = json.loads((d / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((d / "answers.json").read_text("utf-8"))["commits"]
    return Dataset(name, d, tests, answers)


def run_native(ds: Dataset, selector: Path, src: Path | None = None):
    """Run the selector as a plain process. Returns (stdout bytes, stderr text, cpu seconds)."""
    env = dict(os.environ, BR_DATA=str(ds.data_dir), BR_SRC=str(src or DEFAULT_SRC))
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    r = subprocess.run([sys.executable, str(selector)], env=env, capture_output=True)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return r.stdout, r.stderr.decode("utf-8", "replace"), cpu


def run_docker(ds: Dataset, image: str):
    """Run the image with the scorer's flags. Returns (stdout, stderr, wall seconds)."""
    cmd = ["docker", "run", *DOCKER_FLAGS,
           "--volume", f"{ds.data_dir / 'commits'}:/data/commits:ro",
           "--volume", f"{ds.data_dir / 'tests.json'}:/data/tests.json:ro", image]
    t0 = time.monotonic()
    r = subprocess.run(cmd, capture_output=True)
    return r.stdout, r.stderr.decode("utf-8", "replace"), time.monotonic() - t0


def _durations(ds: Dataset) -> dict[int, int]:
    return {t["number"]: S._duration_milliseconds(t["seconds"]) for t in ds.tests}


def _cost(workers: list[list[int]], dur: dict[int, int]) -> int:
    workers = [w for w in workers if w]
    if not workers:
        return 0
    return max(sum(dur[n] for n in w) for w in workers) + S.WORKER_COST_MILLISECONDS * len(workers)


def score(stdout: bytes, ds: Dataset) -> dict:
    """Score with the kit's scorer, plus per-commit detail. ok=False carries the rejection code."""
    ev = S.evaluate_run(stdout, ds.tests, ds.answers)
    if not ev["ok"]:
        return {"ok": False, "code": ev["code"], "line": ev["line"]}
    dur = _durations(ds)
    rows = {}
    for line in stdout.decode("utf-8").splitlines():
        row = json.loads(line)
        rows[row["commit"]] = [[int(n) for n in w] for w in row["workers"]]
    costs, misses = {}, []
    for c, workers in rows.items():
        costs[c] = _cost(workers, dur)
        chosen = {n for w in workers for n in w}
        lost = [n for n in ds.answers[c]["failing"] if n not in chosen]
        if lost:
            misses.append((c, lost))
    misses.sort()
    total = sum(costs.values()) + S.MISS_PENALTY_MILLISECONDS * len(misses)
    assert total == ev["simulated_milliseconds"], "harness cost disagrees with score.py"
    assert [m[0] for m in misses] == ev["missed_commits"]
    return {"ok": True, "simulated_ms": total, "misses": misses, "costs": costs,
            "selected": {c: sum(len(w) for w in ws) for c, ws in rows.items()}}


def floor_ms(ds: Dataset) -> dict[str, int]:
    """Cost of the best possible answer per commit: optimal packing of the true failing set."""
    dur = _durations(ds)
    return {c: _cost(pack.pack({n: dur[n] for n in a["failing"]}), dur) for c, a in ds.answers.items()}


def _s(ms: int) -> str:
    return f"{ms // 1000}.{ms % 1000:03d}"


def render(ds: Dataset, res: dict, stderr: str, cpu_s: float | None, cpu_label: str = "CPU") -> str:
    if not res["ok"]:
        where = "" if res["line"] is None else f" on line {res['line']}"
        return f"Rejected: {res['code']}{where}"
    floor = floor_ms(ds)
    costs = res["costs"]
    missed = {c for c, _ in res["misses"]}
    out = [f"set {ds.name}: {len(ds.answers)} commits, {len(ds.tests)} tests"]
    out.append(f"Simulated: {_s(res['simulated_ms'])} s")
    out.append(f"Misses:    {len(res['misses'])}")
    for c, lost in res["misses"]:
        ids = {t["number"]: t["id"] for t in ds.tests}
        out.append(f"  missed {c}: " + ", ".join(f"{n} ({ids[n]})" for n in lost))
    out.append(f"Floor:     {_s(sum(floor.values()))} s (optimal packing of the true failing sets)")
    over = sum(costs[c] - floor[c] for c in costs if c not in missed)
    out.append(f"Over-selection: {_s(over)} s above the floor on commits without a miss")
    kinds = sorted({a["kind"] for a in ds.answers.values()})
    out.append("")
    out.append(f"{'kind':<10}{'commits':>8}{'cost s':>10}{'floor s':>10}{'over s':>10}{'misses':>8}")
    for k in kinds:
        cs = [c for c, a in ds.answers.items() if a["kind"] == k]
        cost = sum(costs[c] for c in cs)
        fl = sum(floor[c] for c in cs)
        ms = sum(1 for c in cs if c in missed)
        out.append(f"{k:<10}{len(cs):>8}{_s(cost):>10}{_s(fl):>10}{_s(cost - fl - 0):>10}{ms:>8}")
    worst = sorted(((costs[c] - floor[c], c) for c in costs if c not in missed), reverse=True)[:5]
    out.append("")
    out.append("largest over-selection: " + ", ".join(f"{c} +{_s(d)}" for d, c in worst))
    r9 = sum(1 for l in stderr.splitlines() if "fallback" in l or "R9" in l)
    out.append(f"fallback (R9) hits: {r9}")
    if cpu_s is not None:
        out.append(f"{cpu_label}: {cpu_s:.3f} s")
        out.append(f"Total (simulated + {cpu_label}): {(res['simulated_ms'] / 1000 + cpu_s):.3f} s")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="sample", choices=["sample", "synth-dev", "synth-holdout", "stress"])
    ap.add_argument("--docker", action="store_true")
    ap.add_argument("--image", default="fog-of-war")
    ap.add_argument("--selector", default=str(ROOT / "selector" / "main.py"))
    ap.add_argument("--src", default=None)
    a = ap.parse_args(argv)
    ds = load_set(a.set)
    if a.docker:
        stdout, stderr, secs = run_docker(ds, a.image)
        cpu, label = None, "CPU"
    else:
        stdout, stderr, cpu = run_native(ds, Path(a.selector), Path(a.src) if a.src else None)
        label = "CPU (native)"
    res = score(stdout, ds)
    print(render(ds, res, stderr, cpu, label))
    if a.docker:
        print(f"wall (docker): {secs:.1f} s; CPU in container needs the T10 wrapper")
    if not res["ok"]:
        return 2
    if res["misses"] and a.set in ("synth-holdout", "stress"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
