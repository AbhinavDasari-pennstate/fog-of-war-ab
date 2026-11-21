#!/usr/bin/env python3
"""Laptop only. Merge per-seed recordings of the whole suite (tools/wmfork.py) into maps/weakmut.bin, format 3.

    python tools/wm_all_build.py --src <pinned sqlglot checkout> --tests <tests.json> --res <seed dir> [<seed dir> ...]
        [--old <weakmut.bin in the per-test-process format, used for tests the fork run could not record>]
        [--min-seeds 3] [--out maps/weakmut.bin]

A test is recorded when every seed directory has its result file (the test passed in every seed), or, failing
that, when --old has an entry for it (recorded alone in a fresh process, five seeds). A site whose summary mask
differs between seeds, or is missing in one seed, gets every bit (never dropped). The output keeps only, per
site, the groups (merged mask -> bitmask of tests); a recorded test missing from every group never ran there.
"""
import argparse
import json
import marshal
import os
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "selector"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import weakmut as W  # noqa: E402
from weakmut_build import tree_hash  # noqa: E402

ALLBITS = W.EVAL_BIT | W.ALT_BIT | W.UNKNOWN_BIT | 0x3FF


def parse_key(key):
    rel, l, c, el, ec, kind = key.rsplit(":", 5)
    return rel, (int(l), int(c), int(el), int(ec), kind)


def merge_seeds(per_seed):
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
            out[k] = ALLBITS
    return out, unstable


def load_test(n, tid, dirs):
    """(merged summary, unstable count) of test n over seed directories, or None when a seed has no result."""
    files = [os.path.join(r, f"{n}.mar") for r in dirs]
    if not all(os.path.exists(f) for f in files):
        return None
    per_seed = []
    for f in files:
        rec = marshal.load(open(f, "rb"))
        assert rec["id"] == tid, (n, rec["id"], tid)
        per_seed.append(rec["mask"])
    return merge_seeds(per_seed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--res", nargs="+", required=True)
    ap.add_argument("--res-b", nargs="+", default=[], help="seed directories of the second harness (tools/wm_perfile.sh); a site's "
                    "mask is the OR over both harnesses, and a test needs a result in every directory of both")
    ap.add_argument("--res-c", nargs="+", default=[], help="seed directories of harness C (tools/wmseq_seeds.sh): whole suite in one process, in order")
    ap.add_argument("--res-d", nargs="+", default=[], help="seed directories of harness D (tools/wm_seeds.sh with WM_PRE set): "
                    "fork per test from a parent that first ran the state-clearing tests")
    ap.add_argument("--old")
    ap.add_argument("--min-seeds", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(ROOT, "maps", "weakmut.bin"))
    a = ap.parse_args()
    if len(a.res) < a.min_seeds:
        sys.exit(f"need at least {a.min_seeds} seed directories")
    tests = {t["number"]: t["id"] for t in json.load(open(a.tests))["tests"]}
    old = {}
    if a.old and os.path.exists(a.old):
        d = marshal.loads(zlib.decompress(open(a.old, "rb").read()))
        if d.get("rec") == W.RECORDER_VERSION and d.get("src") == tree_hash(os.path.realpath(a.src)) and "tests" in d:
            old = d["tests"]
    recorded, ids, groups = 0, {}, {}
    n_fork = n_old = n_unrec = unstable_total = 0
    harnesses = [d for d in (a.res, a.res_b, a.res_c, a.res_d) if d]
    for n in sorted(tests):
        parts = [load_test(n, tests[n], d) for d in harnesses]
        have = [p for p in parts if p is not None]
        summ, unstable = {}, 0
        for one, u in have:  # OR over the harnesses that recorded this test
            unstable += u
            for k, m in one.items():
                summ[k] = summ.get(k, 0) | m
        if len(have) == len(parts):
            unstable_total += unstable
            n_fork += 1
        elif n in old and old[n]["id"] == tests[n]:
            # a harness has no result for this test: keep what the others recorded and add the per-process entry
            for rel, per in old[n]["sites"].items():
                for (l, c, el, ec, kind), mask in per.items():
                    key = f"{rel}:{l}:{c}:{el}:{ec}:{kind}"
                    summ[key] = summ.get(key, 0) | mask
            unstable_total += unstable
            n_old += 1
        else:
            n_unrec += 1
            continue
        recorded |= 1 << n
        ids[n] = tests[n]
        for key, mask in summ.items():
            if not mask:
                continue
            rel, k = parse_key(key)
            g = groups.setdefault(rel, {}).setdefault(k, {})
            g[mask] = g.get(mask, 0) | (1 << n)
    sites = {rel: {k: sorted(g.items()) for k, g in per.items()} for rel, per in groups.items()}
    data = {"v": 3, "fmt": "sites", "rec": W.RECORDER_VERSION, "src": tree_hash(os.path.realpath(a.src)),
            "recorded": recorded, "ids": ids, "sites": sites}
    blob = zlib.compress(marshal.dumps(data), 9)
    with open(a.out, "wb") as f:
        f.write(blob)
    nsites = sum(len(p) for p in sites.values())
    print(f"recorded {len(ids)} of {len(tests)} tests ({n_fork} by every harness, {n_old} partly or wholly from --old, {n_unrec} unrecorded); "
          f"{nsites} sites, {sum(len(g) for p in sites.values() for g in p.values())} groups, "
          f"{unstable_total} unstable test-sites set to all bits; {len(blob)} bytes")


if __name__ == "__main__":
    main()
