#!/usr/bin/env python3
"""Laptop only. Turn tools/run_valmap.sh recordings into maps/valreads.bin.

    python tools/build_valmap.py build --maps maps --run <dir seed A> <dir seed B> ... [--out maps/valreads.bin]

Each --run dir is one full recording (one hash seed). Per site ("<path>:<line>" of a module- or class-level dict
display with distinct constant keys) and entry key (repr of the constant):
  reads    mask of the tests that read the entry's value (d[k], d.get(k), pop, setdefault) or read all values
           (items, values, copy, ==, repr, dict(d), {**d}, update(d), f(**d), copy.copy ...), union over runs
  req      sites whose values were read in bulk only by Expr.__init_subclass__ (required_args): see selector/valrule.py
  derived  the entry (or, with key "*", every entry) was read while a module or class body ran, or outside any
           test, in any run; or whose test set differs between runs: the selector keeps today's rule for it
Also stored: the sites that were created in some run ("S"), and the tests that are unrecorded: tests that failed
while recording, or whose protocol never started in some run (the selector always adds those).
"""
import argparse
import glob
import json
import marshal
import os
import sys
import zlib
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "maps"))


def failed_tests(d, num):
    bad = set()
    xml = os.path.join(d, "junit.xml")
    if os.path.exists(xml):
        for tc in ET.parse(xml).getroot().iter("testcase"):
            if tc.find("failure") is None and tc.find("error") is None:
                continue
            parts = (tc.get("classname") or "").split(".")
            for i in range(len(parts), 0, -1):
                tid = "/".join(parts[:i]) + ".py::" + "::".join(parts[i:] + [tc.get("name")])
                if tid in num:
                    bad.add(num[tid])
                    break
            else:
                raise SystemExit(f"failed test {tc.get('classname')}::{tc.get('name')} not found in the map ids: refusing to build")
    return bad


def read_run(d, num):
    derived, per, created, started, req, bad_sites = set(), {}, set(), set(), set(), set()
    for f in glob.glob(os.path.join(d, "vm.*.log")):
        with open(f, errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                tag, site, key = parts
                if tag == "S":
                    created.add(site)
                elif tag == "T":
                    t = num.get(site)
                    if t is not None:
                        started.add(t)
                elif tag == "D":
                    derived.add((site, key))
                elif tag == "R":
                    req.add(site)
                elif tag == "B":
                    bad_sites.add(site)
                else:
                    t = num.get(tag)
                    if t is not None:
                        per[(site, key)] = per.get((site, key), 0) | (1 << t)
    created -= bad_sites
    return derived, per, created, started, failed_tests(d, num), req, bad_sites


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--maps", default=os.path.join(ROOT, "maps"))
    ap.add_argument("--run", nargs="+", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "maps", "valreads.bin"))
    a = ap.parse_args()
    from load import load

    m = load(a.maps)
    num = {t: n for n, t in enumerate(m.ids) if t is not None}
    runs = [read_run(d, num) for d in a.run]
    derived, created, reqs, bad = set(), set(), set(), set()
    keys = set()
    for dv, per, cr, _, _, rq, bs in runs:
        derived |= dv
        created |= cr
        reqs |= rq
        bad |= bs
        keys |= set(per)
    reads, differ = {}, 0
    for k in keys:
        masks = [per.get(k, 0) for _, per, _, _, _, _, _ in runs]
        if len(set(masks)) > 1:
            differ += 1
            derived.add(k)
        u = 0
        for x in masks:
            u |= x
        reads[k] = u
    allnum = {n for n, t in enumerate(m.ids) if t is not None}
    unrec = set()
    for _, _, _, started, badt, _, _ in runs:
        unrec |= (allnum - started) | badt
    pure = ("tests/test_errors.py", "tests/test_helper.py", "tests/test_time.py", "tests/test_sqlglotc.py")
    unrec = {n for n in unrec if not m.ids[n].startswith(pure)} | {n for _, _, _, _, badt, _, _ in runs for n in badt}
    sets, sid = [0], {0: 0}
    table = {}
    for (site, key), mask in reads.items():
        i = sid.get(mask)
        if i is None:
            i = sid[mask] = len(sets)
            sets.append(mask)
        table.setdefault(site, {})[key] = i
    der = {}
    for site, key in derived:
        der.setdefault(site, []).append(key)
    out = {
        "v": 1,
        "sets": sets,
        "reads": table,
        "derived": {s: sorted(v) for s, v in der.items()},
        "sites": sorted(created - bad),
        "req": sorted(reqs),
        "unrecorded": sorted(unrec),
        "runs": len(runs),
    }
    with open(a.out, "wb") as f:
        f.write(zlib.compress(marshal.dumps(out), 9))
    print(f"read pairs {len(reads)} derived {len(derived)} differ-between-runs {differ} sites created {len(created)} "
          f"req sites {len(reqs)} sets {len(sets)} unrecorded tests {len(unrec)} bytes {os.path.getsize(a.out)}")


if __name__ == "__main__":
    main()
