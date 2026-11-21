"""Which sqlglot lambdas does the TPC-DS test call, outside a query and inside each query? (maps/tpcds_lambdas.json)

    PYTHONHASHSEED=<n> python tools/tpcds_lambdas.py --src <pinned sqlglot> --out <json>

The per-query line coverage in maps/tpcds.z cannot see a lambda that sits on one line: sys.monitoring LINE
gives no event for the body of a one-line lambda, and the creation of the lambda (a line of a table that
runs at import) puts an event on the same line anyway. So the calls are recorded here: for every query,
by stage (P B O E T), the lambdas it called, from a cold fork of the warm base as in tools/tpcds_probe.py
alone mode. A lambda that is CALLED while the modules are imported or while setUpClass builds the schema
and tables (the "base" phase of tools/tpcds_probe.py: import_all, the helpers, the fixture parse, every
table) changes the state every query starts from, so an edit to it is an import-time edit.

Recorded with sys.monitoring PY_START (a code object named <lambda> starts running), keyed
"<path under src>:<line of the lambda keyword>". The union over the hash seeds given on the command
line (the tool re-runs itself once per seed).
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))

mon = sys.monitoring
TOOL = mon.PROFILER_ID
_prefix = ""
called: set = set()
_cur: set = set()


def _start(code, offset):
    f = code.co_filename
    if code.co_name == "<lambda>" and f.startswith(_prefix):
        key = f"{f[len(_prefix):]}:{code.co_firstlineno}"
        called.add(key)
        _cur.add(key)
    return None


def forked(fn):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            payload = json.dumps(fn()).encode()
        except BaseException as e:  # noqa: BLE001
            payload = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
        with os.fdopen(w, "wb") as f:
            f.write(payload)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        data = f.read()
    os.waitpid(pid, 0)
    got = json.loads(data)
    if "error" in got:
        raise SystemExit(got["error"])
    return got


def query(core, qs, schema, tables, i):
    """The lambdas query i calls at each stage, from this (forked) warm base."""
    stages = {}
    _cur.clear()

    def hook(stage):
        stages[stage] = sorted(_cur)
        _cur.clear()

    core.run_query(qs[i][1], qs[i][0], schema, tables, hook=hook)
    return stages


def one(src, out):
    global _prefix
    import tpcds_core as core

    src = os.path.abspath(src)
    _prefix = os.path.realpath(src) + os.sep
    core.setup_path(src)
    mon.use_tool_id(TOOL, "tpcds-lambdas")
    mon.register_callback(TOOL, mon.events.PY_START, _start)
    mon.set_events(TOOL, mon.events.PY_START)
    core.import_all(src)
    helpers = core.load_helpers(src)
    core.queries(src, helpers)
    schema = helpers.TPCDS_SCHEMA
    tables = {t: core.load_table(src, t, cols) for t, cols in schema.items()}
    base = sorted(called)
    qs = core.queries(src, helpers)
    per_q = {}
    for i in range(len(qs)):
        per_q[str(i)] = forked(lambda: query(core, qs, schema, tables, i))
    mon.set_events(TOOL, 0)
    json.dump({"base": base, "q": per_q}, open(out, "w"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seeds", default="0,1,2,3")
    ap.add_argument("--child", action="store_true")
    a = ap.parse_args()
    if a.child:
        one(a.src, a.out)
        return
    base: set = set()
    per_q: dict = {}
    for sd in a.seeds.split(","):
        tmp = a.out + f".{sd}"
        subprocess.run([sys.executable, __file__, "--src", a.src, "--out", tmp, "--child"], check=True,
                       env=dict(os.environ, PYTHONHASHSEED=sd, PYTHONDONTWRITEBYTECODE="1"))
        got = json.load(open(tmp))
        base.update(got["base"])
        for q, stages in got["q"].items():
            d = per_q.setdefault(q, {})
            for st, v in stages.items():
                d.setdefault(st, set()).update(v)
        os.remove(tmp)
    q_out = {q: {st: sorted(v) for st, v in sorted(d.items())} for q, d in sorted(per_q.items(), key=lambda kv: int(kv[0]))}
    json.dump({"pin": os.path.basename(os.path.abspath(a.src)), "called": sorted(base), "q": q_out},
              open(a.out, "w"), separators=(",", ":"))
    n = len({k for d in q_out.values() for v in d.values() for k in v})
    print(f"{len(base)} lambdas called outside a query, {n} distinct ones inside queries -> {a.out}")


if __name__ == "__main__":
    main()
