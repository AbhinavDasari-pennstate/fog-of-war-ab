#!/usr/bin/env python3
"""Laptop only. Turn tools/run_attr.sh recordings into maps/attrreads.bin.

    python tools/build_attrmap.py build --src <sqlglot checkout> --maps maps --run <dir seed A> <dir seed B> ... [--out maps/attrreads.bin]

Each --run dir is one full recording (one hash seed). Per (class, attribute):
  reads    mask of the tests that read the attribute through the class or a subclass (union over runs)
  derived  read while a module or class body ran, or outside any test, in any run; or whose test set differs between runs
Also stored: the list of recorded attribute names, the sqlglot classes that are enums, and the tests with no
recording in some run (the selector always adds those).
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


def read_run(d, num):
    derived, per = set(), {}
    seen_tests = set()
    for f in glob.glob(os.path.join(d, "attr.*.log")):
        with open(f, errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                tag, q, n = parts
                if tag == "D":
                    derived.add((q, n))
                else:
                    t = num.get(tag)
                    if t is None:
                        continue
                    seen_tests.add(t)
                    per[(q, n)] = per.get((q, n), 0) | (1 << t)
    bad = set()
    xml = os.path.join(d, "junit.xml")
    if os.path.exists(xml):
        for tc in ET.parse(xml).getroot().iter("testcase"):
            if tc.find("failure") is not None or tc.find("error") is not None:
                cls = tc.get("classname", "").replace(".", "/")
                # classname is a dotted module path plus class: match on the name part when possible
                bad.add((tc.get("classname"), tc.get("name")))
    return derived, per, seen_tests, bad


def enum_classes(src):
    sys.path.insert(0, src)
    import enum
    import importlib
    import pkgutil

    import sqlglot

    out = set()
    for mod in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            m = importlib.import_module(mod.name)
        except Exception:
            continue
        for obj in list(vars(m).values()):
            if isinstance(obj, type) and (obj.__module__ or "").startswith("sqlglot"):
                for c in obj.__mro__:
                    if issubclass(c, enum.Enum) or (getattr(c, "__module__", "") == "enum"):
                        out.add(f"{obj.__module__}:{obj.__qualname__}")
                        break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--src", required=True)
    ap.add_argument("--maps", default=os.path.join(ROOT, "maps"))
    ap.add_argument("--run", nargs="+", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "maps", "attrreads.bin"))
    a = ap.parse_args()
    from load import load

    m = load(a.maps)
    num = {t: n for n, t in enumerate(m.ids) if t is not None}
    runs = [read_run(d, num) for d in a.run]
    names = json.load(open(os.path.join(a.run[0], "names.json")))
    derived = set()
    keys = set()
    for dv, per, _, _ in runs:
        derived |= dv
        keys |= set(per)
    reads, differ = {}, 0
    for k in keys:
        masks = [per.get(k, 0) for _, per, _, _ in runs]
        if len(set(masks)) > 1:
            differ += 1
            derived.add(k)
        u = 0
        for x in masks:
            u |= x
        reads[k] = u
    allnum = {n for n, t in enumerate(m.ids) if t is not None}
    unrec = set()
    for _, _, seen, _ in runs:
        unrec |= allnum - seen
    # tests that read no attribute of a sqlglot class by design (pure helpers, the C build tests, which the
    # srcreaders map covers) need no entry; any other test with no recording is always added by the selector
    pure = ("tests/test_errors.py", "tests/test_helper.py", "tests/test_time.py", "tests/test_sqlglotc.py")
    unrec = {n for n in unrec if not m.ids[n].startswith(pure)}
    sets, sid = [0], {0: 0}
    table = {}
    for (q, n), mask in reads.items():
        i = sid.get(mask)
        if i is None:
            i = sid[mask] = len(sets)
            sets.append(mask)
        table.setdefault(q, {})[n] = i
    der = {}
    for q, n in derived:
        der.setdefault(q, []).append(n)
    out = {
        "v": 1,
        "names": sorted(names),
        "sets": sets,
        "reads": table,
        "derived": {q: sorted(v) for q, v in der.items()},
        "enums": sorted(enum_classes(os.path.realpath(a.src))),
        "unrecorded": sorted(unrec),
        "runs": len(runs),
    }
    with open(a.out, "wb") as f:
        f.write(zlib.compress(marshal.dumps(out), 9))
    print(f"pairs {len(reads)} derived {len(derived)} differ-between-runs {differ} sets {len(sets)} "
          f"unrecorded tests {len(unrec)} enums {len(out['enums'])} bytes {os.path.getsize(a.out)}")


if __name__ == "__main__":
    main()
