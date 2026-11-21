"""Local runs only (never the shipped CMD): run selector/main.py unchanged and attribute its CPU to phases and commits.

    docker run ... -v tools/phase_profile.py:/phase_profile.py:ro IMAGE python -S -E /phase_profile.py

stdout is the selector's own stdout, untouched. One line `PHASEJSON {...}` goes to stderr at exit.
Phases are exclusive (the innermost active phase is charged) and measured with time.thread_time(), so the
numbers add up to the main process CPU. CPU of child processes (the TPC-DS replay children) is read from
wait4() per child. Nothing here changes what the selector decides; it only wraps calls with timers.
"""

import builtins
import functools
import json
import os
import resource
import sys
import threading
import time

_T0 = time.process_time()
APP = os.environ.get("BR_APP", "/app")
sys.path.insert(0, APP + "/selector")
sys.path.insert(0, APP)

import ast  # noqa: E402

_tl = threading.local()
ACC: dict = {}  # (commit, phase) -> seconds
CHILD: dict = {}  # commit -> child cpu seconds
CUR = ["(shared)"]


def _st():
    s = getattr(_tl, "s", None)
    if s is None:
        s = _tl.s = {"stack": ["other"], "last": time.thread_time(), "commit": None}
    return s


def _credit(s, now):
    key = (s["commit"] or CUR[0], s["stack"][-1])
    ACC[key] = ACC.get(key, 0.0) + (now - s["last"])
    s["last"] = now


def push(name):
    s = _st()
    _credit(s, time.thread_time())
    s["stack"].append(name)


def pop():
    s = _st()
    _credit(s, time.thread_time())
    s["stack"].pop()


def wrap(obj, name, phase):
    f = getattr(obj, name)

    @functools.wraps(f)
    def w(*a, **k):
        push(phase)
        try:
            return f(*a, **k)
        finally:
            pop()

    setattr(obj, name, w)


def wrap_prop(cls, name, phase):
    p = getattr(cls, name)
    f = p.fget

    @functools.wraps(f)
    def g(self):
        push(phase)
        try:
            return f(self)
        finally:
            pop()

    setattr(cls, name, property(g))


# ---- imports of the selector's own modules (charged to a shared "imports" bucket) ----
_t_imp = time.thread_time()
import diffparse  # noqa: E402
import pack  # noqa: E402
import pyscope  # noqa: E402
import attrrule  # noqa: E402
import weakmut  # noqa: E402
import rules  # noqa: E402
import tpcds_scope  # noqa: E402
import main as M  # noqa: E402
import maps.load as ML  # noqa: E402

IMPORTS = time.thread_time() - _t_imp
STARTUP = _T0  # interpreter start up to the first line of this file (process_time at that point)

# commit text -> stem, read before timing starts
DATA = os.environ.get("BR_DATA", "/data")
STEM = {}
for fn in sorted(os.listdir(DATA + "/commits")):
    if fn.endswith(".patch"):
        with open(os.path.join(DATA, "commits", fn), "rb") as fh:
            STEM[fh.read().decode("utf-8")] = fn[: -len(".patch")]

_ast_parse = ast.parse
_ast_dump = ast.dump
_compile = builtins.compile


def ast_parse(*a, **k):
    push("ast_parse")
    try:
        return _ast_parse(*a, **k)
    finally:
        pop()


def ast_dump(*a, **k):
    push("ast_dump")
    try:
        return _ast_dump(*a, **k)
    finally:
        pop()


def compile_(*a, **k):
    flags = a[3] if len(a) > 3 else k.get("flags", 0)
    push("ast_parse" if flags & ast.PyCF_ONLY_AST else "compile")
    try:
        return _compile(*a, **k)
    finally:
        pop()


ast.parse = ast_parse
ast.dump = ast_dump
builtins.compile = compile_

wrap(ML, "_read", "map_load")
wrap(ML.Maps, "__init__", "map_load")
for _p in ("seqcredit", "importtime", "mentions", "weakmut"):
    wrap_prop(ML.Maps, _p, "map_load")
wrap(tpcds_scope.TpcdsMap, "load", "map_load")
wrap(attrrule.Attr, "__init__", "map_load")
wrap(attrrule, "find", "attrrule")
wrap(attrrule.Attr, "readers", "attrrule")
wrap(weakmut, "drops", "weakmut")
wrap(diffparse, "parse", "patch_parse")
wrap(diffparse, "apply", "patch_parse")
for _n in ("ast_diff", "rebound_spans", "scope_hazards", "default_only", "names_in"):
    wrap(pyscope, _n, "pyscope_walk")
wrap(rules, "_defaults_only", "pyscope_walk")
wrap(rules, "select", "rules_other")
wrap(tpcds_scope, "scope", "tpcds_scope")
wrap(pack, "pack", "pack")

_decide = M.decide


def decide(patch_text, *a, **k):
    CUR[0] = STEM.get(patch_text, "(unknown)")
    return _decide(patch_text, *a, **k)


M.decide = decide

_tcand = M._tpcds_candidate


def tpcds_candidate(*a, **k):
    got = _tcand(*a, **k)
    if got is not None:
        _REG[id(got[2])] = CUR[0]
    return got


_REG: dict = {}
M._tpcds_candidate = tpcds_candidate

_rre = M._run_replay_ex


def run_replay_ex(plan, patched, src_root, timeout, seed, memo=None):
    s = _st()
    s["commit"] = _REG.get(id(patched), "(unknown)")
    push("replay")
    try:
        return _rre(plan, patched, src_root, timeout, seed, memo)
    finally:
        pop()


M._run_replay_ex = run_replay_ex

_waitpid = os.waitpid


def waitpid(pid, opts):
    if hasattr(os, "wait4"):
        p, st, ru = os.wait4(pid, opts)
        if p:
            c = _st()["commit"] or CUR[0]
            CHILD[c] = CHILD.get(c, 0.0) + ru.ru_utime + ru.ru_stime
        return p, st
    return _waitpid(pid, opts)


os.waitpid = waitpid

if __name__ == "__main__":
    s0 = _st()
    s0["last"] = time.thread_time()
    try:
        M.main()
    finally:
        _credit(s0, time.thread_time())
        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        phases: dict = {}
        shared = {"imports": IMPORTS, "interpreter_startup": STARTUP}
        for (c, ph), v in ACC.items():
            phases.setdefault(c, {})[ph] = phases.setdefault(c, {}).get(ph, 0.0) + v
        out = {
            "commits": phases,
            "shared": shared,
            "child_cpu": CHILD,
            "process_cpu": time.process_time(),
            "children_rusage": ru.ru_utime + ru.ru_stime,
        }
        print("PHASEJSON " + json.dumps(out), file=sys.stderr)
