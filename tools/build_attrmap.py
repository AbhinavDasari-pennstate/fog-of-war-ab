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
    return derived, per, seen_tests, bad


def bare_derived(src):
    """Class-level names that another statement of the same class loads as a bare name: their value flows into
    something built while the class body runs (derived tables, comprehensions, defaults)."""
    import ast

    sys.path.insert(0, os.path.join(ROOT, "selector"))
    import attrrule

    out = set()

    def visit(body, stack, mod):
        for node in body:
            if isinstance(node, ast.ClassDef):
                st = stack + [node.name]
                for stmt in node.body:
                    name = None
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                        name = stmt.targets[0].id
                    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                        name = stmt.target.id
                    if name and attrrule.bare_use(node, stmt, name):
                        out.add((f"{mod}:{'.'.join(st)}", name))
                visit(node.body, st, mod)

    for dp, dn, fn in os.walk(os.path.join(src, "sqlglot")):
        for f in fn:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                visit(ast.parse(open(p, "rb").read(), p).body, [], attrrule.module_of(os.path.relpath(p, src)))
    return out


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
    derived = bare_derived(os.path.realpath(a.src))
    static_n = len(derived)
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
    failed = set()
    for _, _, seen, bad in runs:
        unrec |= allnum - seen
        failed |= bad
    # tests that read no attribute of a sqlglot class by design (pure helpers, the C build tests, which the
    # srcreaders map covers) need no entry; any other test with no recording is always added by the selector
    pure = ("tests/test_errors.py", "tests/test_helper.py", "tests/test_time.py", "tests/test_sqlglotc.py")
    unrec = {n for n in unrec if not m.ids[n].startswith(pure)} | failed  # a test that failed while recorded proves nothing
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
          f"static bare-name derived {static_n} failed-while-recording {len(failed)} unrecorded tests {len(unrec)} enums {len(out['enums'])} bytes {os.path.getsize(a.out)}")


if __name__ == "__main__":
    main()
