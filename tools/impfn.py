#!/usr/bin/env python3
"""Import-time and collection-time function map (map build only): which module bodies run which code while they import or are collected?

    python tools/impfn.py --src <pinned sqlglot checkout> --out maps/impfn.bin [--seeds 0,1,2]

Build it in the scorer's own image (python 3.13.18, the digest in the Dockerfile) with the test dependencies of tools/requirements.lock
installed, as the scorer's uid with a read-only root: the map records the interpreter's `sys.version` and the selector ignores it on any
other interpreter. Run the build once per hash seed (a child process each, PYTHONHASHSEED set) and the results are merged by union.

What is recorded, per seed: sys.setprofile over (1) `import sqlglot` and every sqlglot module and dialect, then (2) the collection of the whole
tests/ directory by `pytest --collect-only` (the test modules and what they call while they import, which tools/impexec.py does not see).
For every call of a Python function or lambda of the checkout (CO_OPTIMIZED code) made while a repo module body (code name <module>) is on the
stack, the innermost such module M is credited:  range[F] -> {(first line, last line): {M}}.
During collection a call with NO repo module body on the stack (a pytest hook or fixture calling into the repo) is credited to the pseudo
module "<nomod>": the selector widens on it. Modules are rel paths of the checkout (sqlglot/... or tests/...).

Why a second map next to maps/impexec.bin: impexec.bin feeds Ctx.imptime and must keep its answers; this one feeds the narrowing of an edit of a
function that ran while a module imported (rules.py `Ctx.import_deps`), which has to be complete (collection included, three seeds) or fall back
to the old widening.

Output: zlib(marshal({"v": 1, "py": sys.version, "seeds": [...], "collect": bool, "base": [...], "range": {rel file: [(lo, hi, [modules])]},
"stats": {...}})).
"""

from __future__ import annotations

import argparse
import importlib
import json
import marshal
import os
import pkgutil
import subprocess
import sys
import zlib

CO_OPTIMIZED = 0x1
NOMOD = "<nomod>"


def record(src: str, collect: bool) -> dict:
    src = os.path.realpath(src)
    pre = src + os.sep
    sys.path.insert(0, src)
    os.chdir(src)
    fcache: dict = {}

    def rel(fn: str):
        r = fcache.get(fn, 0)
        if r == 0:
            r = None
            if fn.startswith(pre) and fn.endswith(".py"):
                r = fn[len(pre):].replace(os.sep, "/")
            fcache[fn] = r
        return r

    mods: list = []  # repo module bodies currently running, innermost last
    stack: list = []  # per live python frame: True when it is a repo module body (pushed on mods)
    byrange: dict = {}
    phase = {"collecting": False}

    def prof(frame, event, arg):
        if event == "call":
            code = frame.f_code
            r = rel(code.co_filename)
            if r is None:
                stack.append(False)
                return
            if code.co_name == "<module>":
                mods.append(r)
                stack.append(True)
                return
            stack.append(False)
            if code.co_flags & CO_OPTIMIZED:
                m = mods[-1] if mods else (NOMOD if phase["collecting"] else None)
                if m is not None:
                    key = (code.co_firstlineno, max([ln for _, _, ln in code.co_lines() if ln] or [code.co_firstlineno]))
                    byrange.setdefault(r, {}).setdefault(key, set()).add(m)
        elif event == "return":
            if stack and stack.pop():
                mods.pop()

    errors: list = []
    sys.setprofile(prof)
    try:
        import sqlglot  # noqa: F401

        base = sorted(x for x in (rel(getattr(mm, "__file__", "") or "") for mm in list(sys.modules.values())) if x)
        for mod in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            if mod.name.rsplit(".", 1)[-1] == "__main__":
                continue
            try:
                importlib.import_module(mod.name)
            except Exception as exc:
                errors.append(f"skip {mod.name} {type(exc).__name__}")
        from sqlglot import dialects

        for name in list(getattr(dialects, "DIALECTS", [])):
            try:
                importlib.import_module("sqlglot.dialects." + name.lower())
            except Exception:
                pass
        for attr in dir(dialects):
            if attr[:1].isupper():
                try:
                    getattr(dialects, attr)
                except Exception:
                    pass
        ncollected = 0
        if collect:
            import pytest

            phase["collecting"] = True

            class Count:
                def pytest_collection_finish(self, session):
                    nonlocal ncollected
                    ncollected = len(session.items)

            os.environ.setdefault("SKIP_INTEGRATION", "0")
            rc = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", src, "tests"], plugins=[Count()])
            phase["collecting"] = False
            errors.append(f"pytest collect exit {int(rc)} items {ncollected}")
    finally:
        sys.setprofile(None)
    return {"base": base, "errors": errors, "ncollected": ncollected if collect else 0,
            "range": {f: sorted((lo, hi, sorted(v)) for (lo, hi), v in d.items()) for f, d in sorted(byrange.items())}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seeds", default="0,1,2")
    ap.add_argument("--no-collect", action="store_true")
    ap.add_argument("--child", action="store_true", help="record one seed and print json")
    a = ap.parse_args()
    if a.child:
        res = record(a.src, not a.no_collect)
        sys.stdout.write(json.dumps(res) + "\n")
        return 0
    seeds = [int(s) for s in a.seeds.split(",")]
    merged: dict = {}
    base: set = set()
    errors: list = []
    ncol = 0
    for s in seeds:
        env = dict(os.environ, PYTHONHASHSEED=str(s), PYTHONDONTWRITEBYTECODE="1")
        cmd = [sys.executable, os.path.abspath(__file__), "--child", "--src", a.src, "--out", a.out, "--seeds", a.seeds]
        if a.no_collect:
            cmd.append("--no-collect")
        r = subprocess.run(cmd, capture_output=True, env=env, cwd=os.path.realpath(a.src))
        if r.returncode:
            print(r.stderr.decode()[-2000:], file=sys.stderr)
            return 1
        res = json.loads(r.stdout.decode().strip().splitlines()[-1])
        base |= set(res["base"])
        errors += [f"seed {s}: {e}" for e in res["errors"]]
        ncol = max(ncol, res["ncollected"])
        for f, rows in res["range"].items():
            d = merged.setdefault(f, {})
            for lo, hi, ms in rows:
                d.setdefault((lo, hi), set()).update(ms)
        print(f"seed {s}: {sum(len(v) for v in res['range'].values())} ranges, collected {res['ncollected']}", file=sys.stderr)
    out = {"v": 1, "py": sys.version, "seeds": seeds, "collect": not a.no_collect and ncol > 0, "base": sorted(base),
           "range": {f: sorted((lo, hi, sorted(v)) for (lo, hi), v in d.items()) for f, d in sorted(merged.items())},
           "stats": {"files": len(merged), "ranges": sum(len(d) for d in merged.values()), "collected": ncol, "notes": errors[:40]}}
    with open(a.out, "wb") as f:
        f.write(zlib.compress(marshal.dumps(out), 9))
    allm = set()
    for d in merged.values():
        for v in d.values():
            allm |= v
    print(f"files {len(merged)} ranges {out['stats']['ranges']} modules {len(allm)} collect {out['collect']} py {sys.version.split()[0]}", file=sys.stderr)
    for e in errors[:12]:
        print(e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
