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


def tree_hash(src):
    import hashlib

    h = hashlib.sha256()
    for dp, dn, fn in sorted(os.walk(os.path.join(src, "sqlglot"))):
        dn.sort()
        for f in sorted(fn):
            if f.endswith(".py"):
                h.update(os.path.relpath(os.path.join(dp, f), src).encode())
                h.update(open(os.path.join(dp, f), "rb").read())
    return h.hexdigest()


def merge(per_seed):
    """Union over seeds; a site whose mask differs between seeds (or is missing in one) gets every bit, so it is never dropped."""
    import weakmut as W

    keys = set()
    for one in per_seed:
        keys |= set(one)
    out, unstable = {}, 0
    for k in keys:
        masks = [one.get(k, 0) for one in per_seed]
        if len(set(masks)) == 1:
            out[k] = masks[0]
        else:
            unstable += 1
            out[k] = W.EVAL_BIT | W.ALT_BIT | W.UNKNOWN_BIT | 0x3FF
    return out, unstable


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
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2, 3, 4])
    ap.add_argument("--allow-few-seeds", action="store_true")
    a = ap.parse_args()
    tests = {t["number"]: t["id"] for t in json.load(open(a.tests))["tests"]}
    os.makedirs(a.work, exist_ok=True)
    sys.path.insert(0, os.path.join(ROOT, "selector"))
    import weakmut as W

    src = os.path.realpath(a.src)
    stamp = {"rec": W.RECORDER_VERSION, "src": tree_hash(src)}
    data = {"v": 2, **stamp, "tests": {}}
    if os.path.exists(a.out):
        old = marshal.loads(zlib.decompress(open(a.out, "rb").read()))
        if old.get("rec") == stamp["rec"] and old.get("src") == stamp["src"]:
            data = old  # same recorder and same source: extend
    for n in a.numbers:
        per_seed, bad = [], False
        for seed in a.seeds:
            rc, secs, one, tail = run_one(a.py, src, a.work, tests[n], a.timeout, seed)
            print(f"{n} {tests[n]} seed {seed} exit {rc} {secs:.1f}s sites {len(one)}", flush=True)
            if rc != 0:
                print(tail, flush=True)
                bad = True
                break
            per_seed.append(one)
        if bad or len(per_seed) < 5 and not a.allow_few_seeds:
            data["tests"].pop(n, None)  # a failed or short recording proves nothing: the test is never dropped
            print(f"{n}: entry removed", flush=True)
        else:
            summ, unstable = merge(per_seed)
            print(f"{n}: {len(summ)} sites, {unstable} sites differ between seeds (all bits set)", flush=True)
            data["tests"][n] = {"id": tests[n], "sites": pack(summ)}
        with open(a.out, "wb") as f:
            f.write(zlib.compress(marshal.dumps(data), 9))
    print("bytes", os.path.getsize(a.out))


if __name__ == "__main__":
    main()
