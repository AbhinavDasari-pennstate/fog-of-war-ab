"""One recording process for the TPC-DS replay map (see tools/build_tpcds.py).

    PYTHONHASHSEED=<n> python tools/tpcds_probe.py --src <pinned sqlglot> --mode alone|fwd|rev|batch|shuf<k> --out <json>

Traces executed lines under <src>/sqlglot with sys.monitoring, per phase:
  base   : importing every sqlglot module (what a whole pytest session imports) and the test's
           setUpClass work (tables, schema, fixture parse)
  q<i>   : one query through selector/tpcds_core.run_query
Modes: alone = each query in a fork of the warm base (own cold lazy state), fwd and rev = one
process, in or against test order, batch = the test's own order (every parse first, then each
result), shuf<k> = a seeded shuffle.
"""

import argparse
import json
import os
import pkgutil
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
import tpcds_core as core  # noqa: E402

mon = sys.monitoring
TOOL = mon.COVERAGE_ID
_prefix = ""
_cur: dict[str, set] = {}


def _line(code, ln):
    f = code.co_filename
    if f.startswith(_prefix):
        _cur.setdefault(f[len(_prefix):], set()).add(ln)
    return mon.DISABLE


def start(src):
    global _prefix
    _prefix = os.path.realpath(src) + os.sep
    mon.use_tool_id(TOOL, "tpcds")
    mon.register_callback(TOOL, mon.events.LINE, _line)
    mon.set_events(TOOL, mon.events.LINE)


def take():
    """Lines seen since the last take (restart so every line can fire once more)."""
    global _cur
    out = {f: sorted(v) for f, v in _cur.items()}
    _cur = {}
    mon.restart_events()
    return out


def import_all(src):
    import sqlglot

    names = []
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        names.append(m.name)
    failed = []
    for n in sorted(names):
        try:
            __import__(n)
        except Exception as e:  # compiled or optional modules
            failed.append((n, type(e).__name__))
    from sqlglot.dialects.dialect import Dialect

    for name in sorted(Dialect.classes) if hasattr(Dialect, "classes") else []:
        try:
            Dialect.get_or_raise(name)
        except Exception:
            pass
    return failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--mode", default="alone")
    ap.add_argument("--out", required=True)
    ap.add_argument("--trace", action="store_true", help="record coverage (slower)")
    a = ap.parse_args()
    src = os.path.abspath(a.src)
    if a.trace:
        start(src)
    core.setup_path(src)
    t0 = time.process_time()
    failed = import_all(src)
    helpers = core.load_helpers(src)
    qs = core.queries(src, helpers)
    schema = helpers.TPCDS_SCHEMA
    tables = {t: core.load_table(src, t, cols) for t, cols in schema.items()}
    base = take() if a.trace else {}
    base_cpu = time.process_time() - t0
    n = len(qs)
    order = list(range(n))
    if a.mode == "rev":
        order.reverse()
    elif a.mode.startswith("shuf"):
        random.Random(int(a.mode[4:] or 0)).shuffle(order)
    res = {}

    def one(i):
        c0 = time.process_time()
        cov = {}
        stage_cpu = {}
        last = [time.process_time()]

        def hook(stage):
            now = time.process_time()
            stage_cpu[stage] = round(now - last[0], 4)
            if a.trace:
                cov[stage] = take()
            last[0] = time.process_time()

        d = core.run_query(qs[i][1], qs[i][0], schema, tables, hook=hook)
        d["cpu"] = round(time.process_time() - c0, 4)
        d["stage_cpu"] = stage_cpu
        if a.trace:
            d["cov"] = cov
        return d

    if a.mode == "alone":
        for i in order:
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                try:
                    d = one(i)
                    payload = json.dumps(d).encode()
                except BaseException as e:  # noqa: BLE001
                    payload = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
                with os.fdopen(w, "wb") as f:
                    f.write(payload)
                os._exit(0)
            os.close(w)
            with os.fdopen(r, "rb") as f:
                data = f.read()
            os.waitpid(pid, 0)
            res[i] = json.loads(data)
    elif a.mode == "batch":
        # the test: every parse_one first (parent), then each result in order
        from sqlglot import parse_one

        for i in order:
            parse_one(qs[i][1])
        if a.trace:
            take()
        for i in order:
            res[i] = one(i)
    else:
        for i in order:
            res[i] = one(i)
    json.dump({"mode": a.mode, "seed": os.environ.get("PYTHONHASHSEED"), "py": sys.version,
               "n": n, "base": base, "base_cpu": round(base_cpu, 3), "import_failed": failed,
               "meta": [m for m, _ in qs], "res": {str(k): v for k, v in res.items()}},
              open(a.out, "w"))


if __name__ == "__main__":
    main()
