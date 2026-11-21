#!/usr/bin/env python3
"""Laptop only. Build maps/weakmut.bin: run chosen tests alone under the instrumented sqlglot.

    python tools/weakmut_build.py --src <pinned sqlglot checkout (copied)> --py <venv python> \
        --tests <tests.json> --numbers 1490 1106 --work <dir> [--out maps/weakmut.bin]

Each test runs in its own pytest process (and its spawned workers) with tools/weakmut_site first on
PYTHONPATH; every process appends to $WM_OUT/wm.<pid>.log. The summary of a test is the union
(bitwise OR) of all its processes' masks. An existing weakmut.bin is extended, not replaced.
"""
import argparse
import json
import marshal
import os
import shutil
import subprocess
import sys
import time
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_one(py, src, work, tid, timeout, seed):
    out = os.path.join(work, "wm-" + str(abs(hash(tid)))[:8] + f"-s{seed}")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    env = dict(os.environ, SKIP_INTEGRATION="0", WM_OUT=out, WM_SRC=src, WM_TOOLS=ROOT,
               PYTHONPATH=os.pathsep.join([os.path.join(ROOT, "tools", "weakmut_site"), src]),
               PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED=str(seed))
    t0 = time.time()
    try:
        r = subprocess.run([py, "-m", "pytest", tid, "-p", "no:cacheprovider", "-o", "addopts=", "-q", "-x"],
                           cwd=src, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, time.time() - t0, {}, "timeout"  # no entry is written, so the test is never dropped
    secs = time.time() - t0
    summ = {}
    for f in os.listdir(out):
        if not f.startswith("wm."):
            continue
        with open(os.path.join(out, f)) as fh:
            for line in fh:
                key, _, mask = line.rpartition(" ")
                summ[key] = summ.get(key, 0) | int(mask)
    return r.returncode, secs, summ, r.stdout[-400:]


def pack(summ):
    per = {}
    for key, mask in summ.items():
        rel, l, c, el, ec, kind = key.rsplit(":", 5)
        per.setdefault(rel, {})[(int(l), int(c), int(el), int(ec), kind)] = mask
    return per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--py", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--numbers", type=int, nargs="+", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "maps", "weakmut.bin"))
    ap.add_argument("--timeout", type=int, default=3000)
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1])
    a = ap.parse_args()
    tests = {t["number"]: t["id"] for t in json.load(open(a.tests))["tests"]}
    os.makedirs(a.work, exist_ok=True)
    data = {"v": 1, "tests": {}}
    if os.path.exists(a.out):
        data = marshal.loads(zlib.decompress(open(a.out, "rb").read()))
    for n in a.numbers:
        summ, bad = {}, False
        for seed in a.seeds:
            rc, secs, one, tail = run_one(a.py, os.path.realpath(a.src), a.work, tests[n], a.timeout, seed)
            print(f"{n} {tests[n]} seed {seed} exit {rc} {secs:.1f}s sites {len(one)}", flush=True)
            if rc != 0:
                print(tail, flush=True)
                bad = True  # a failing recorded run proves nothing: no entry, so the test is never dropped
                break
            for k, v in one.items():
                summ[k] = summ.get(k, 0) | v  # union over seeds: a site seen in any run is kept as seen
        if bad:
            continue
        data["tests"][n] = {"id": tests[n], "sites": pack(summ)}
        with open(a.out, "wb") as f:
            f.write(zlib.compress(marshal.dumps(data), 9))
    print("bytes", os.path.getsize(a.out))


if __name__ == "__main__":
    main()
