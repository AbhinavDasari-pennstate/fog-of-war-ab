"""Replay TPC-DS queries on a patched tree and compare with the pristine digests.

Run as a child process (python -S -E tpcds_replay.py), job as JSON on stdin:
    {"src": pinned tree, "patched": {relpath: new text}, "plan": {"<q>": "OE"}, "map": path to tpcds.z,
     "per_query_s": float}
Optional job fields for the later seeds of one plan: "memo" {"<q>": [O digests]} (optimized trees an earlier
seed already ran through the executor and found right; the answer is also printed as "memo") and "lazy"
(the tables are loaded only by a query that really needs the executor).
Prints one JSON line {"same": bool, "why": str, "ran": n, "memo": {...}}. `same` is True only when every replayed
query gave, at every stage it ran, a digest the pristine build had recorded. Any difference, error,
timeout or crash is same=False.

Each query runs in its own fork of the warm parent (tables loaded once, patched modules imported
once), so a query sees the state the test's pool worker has when it starts: imported modules and
the shared tables and schema, nothing from an earlier query.
"""

import json
import os
import select
import signal
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tpcds_core as core  # noqa: E402


def _expected(tp_raw, q):
    return tp_raw["queries"][str(q)]


def _fork_run(q, sql, meta, schema, tables, want, limit, expected=None, probe=False):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            signal.alarm(max(1, int(limit)))
            out = core.run_query(sql, meta, schema, tables, want=want, expected=expected, probe=probe)
            payload = json.dumps(out).encode()
        except core.NeedFull:
            payload = json.dumps({"need_full": True}).encode()
        except BaseException as e:  # noqa: BLE001
            payload = json.dumps({"error": f"{type(e).__name__}: {str(e)[:200]}"}).encode()
        try:
            os.write(w, payload)
        finally:
            os._exit(0)
    os.close(w)
    chunks = []
    deadline = time.monotonic() + limit + 2
    killed = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            os.kill(pid, signal.SIGKILL)
            killed = True
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            continue
        b = os.read(r, 1 << 16)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    _, status = os.waitpid(pid, 0)
    if killed or not chunks:
        return {"error": "timeout or crash"}
    return json.loads(b"".join(chunks))


def _limit(e, job):
    return job.get("per_query_s") or max(10.0, 25 * e["cpu"])


class _LazyTables(dict):
    """Tables read from the fixtures on first use. The load happens in the forked query child, so the warm
    parent never holds a table it did not need."""

    def __init__(self, src, schema):
        super().__init__()
        self._src, self._schema = src, schema

    def __missing__(self, name):
        t = core.load_table(self._src, name, self._schema[name])
        self[name] = t
        return t


def _judge(q, e, meta, sql, schema, tables, want, job, memo=()):
    """Does query q still pass? Only what the test can see decides: the executor's result and the
    string duckdb is given (a different intermediate tree does not matter). A query the test does
    not execute is only parsed and pickled, so for it only an error counts.

    `memo`: optimized-tree digests that an earlier seed of this plan already ran through the executor and
    found right. The executor and everything it runs before optimize() are unchanged by this patch (no B or
    E in `want`), so a tree with a digest it has already seen gives the same result: the run stops after
    optimize() like one that matched the pristine tree. A tree nobody has seen runs in full.
    Returns (ok, why, O digest to remember or None)."""
    exec_same = not (set(want) & set("BE"))
    seen = tuple(memo) if exec_same else ()
    ex = e
    if seen:
        ex = dict(e)
        ex["O"] = list(e["O"]) + list(seen)
    lazy = isinstance(tables, _LazyTables)
    out = _fork_run(q, sql, meta, schema, tables, want, _limit(e, job), ex, probe=lazy and exec_same)
    if out.get("need_full"):
        out = _fork_run(q, sql, meta, schema, tables, want, _limit(e, job), e)
        seen = ()
    if "error" in out:
        return False, f"query {q}: {out['error']}", None
    if not out["ok"]:
        return False, f"query {q}: shared tables or schema changed", None
    # a tree seen by an earlier seed: that run already compared sqlglot's global state after the whole
    # pipeline, and the executor did not run here, so the fingerprint of a stop after optimize() is not comparable
    reused = bool(seen) and "E" not in out and out.get("O") in seen and out.get("O") not in e["O"]
    if not reused:
        for k, g in out.get("G", {}).items():  # sqlglot's global containers after the run, by stages run
            if g not in e["G"].get(k, ()):
                return False, f"query {q}: sqlglot's global state after stages {k} differs from the pristine run", None
    if "E" in out and out["tables"] != e["tables"]:
        return False, f"query {q}: find_tables gave different tables", None
    if not e["E"] and not e["tables"] and "E" in out:
        return False, f"query {q}: ran the executor on a query the test does not execute", None
    for st, name in (("E", "result"), ("T", "transpile")):
        if st in out and out[st] not in e[st]:
            return False, f"query {q}: the {name} differs", None
    # remember a tree the executor ran on and the test would accept (it is not the pristine tree)
    keep = out.get("O") if ("E" in out and out.get("O") not in e["O"]) else None
    return True, "", keep


def main():
    job = json.loads(sys.stdin.read())
    src = job["src"]
    tp = json.loads(zlib.decompress(open(job["map"], "rb").read()))
    plan = {int(q): st for q, st in job["plan"].items()}
    t0 = time.process_time()
    try:
        core.setup_path(src, job.get("patched"))
        failed = sorted(x[0] for x in core.import_all(src))
        if failed != tp["import_failed"]:
            raise RuntimeError(f"modules that fail to import differ: {failed}")
        helpers = core.load_helpers(src)
        qs = core.queries(src, helpers)
        if len(qs) != tp["n"]:
            raise RuntimeError("fixture length differs from the map")
        schema = helpers.TPCDS_SCHEMA
        import hashlib
        for i, (meta, sql) in enumerate(qs):
            if hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()[:16] != tp["meta_sha"][i]:
                raise RuntimeError("query meta differs from the map")
        need = set()
        for q, st in plan.items():
            if set(st) & set("BOE"):
                need.update(tp["queries"][str(q)]["tables"])
        memo_in = job.get("memo")
        if job.get("lazy") and memo_in is not None:
            tables = _LazyTables(src, schema)
        else:
            tables = {t: core.load_table(src, t, schema[t]) for t in sorted(need)}
        order = sorted(plan, key=lambda q: tp["queries"][str(q)]["cpu"])
        ran = 0
        memo_out = {}
        for q in order:
            e = tp["queries"][str(q)]
            if e["mutates"]:
                return _done(False, f"query {q} mutates shared state in the pristine run", ran)
            meta, sql = qs[q]
            ok, why, keep = _judge(q, e, meta, sql, schema, tables, tuple(plan[q]), job, (memo_in or {}).get(str(q), ()))
            ran += 1
            if not ok:
                return _done(False, why, ran)
            if keep is not None:
                memo_out.setdefault(str(q), []).append(keep)
        for q, ds in (memo_in or {}).items():  # what earlier seeds learned stays valid for the next
            memo_out.setdefault(q, [])
            memo_out[q] += [d for d in ds if d not in memo_out[q]]
        return _done(True, "every replayed query matches", ran, memo_out)
    except BaseException as ex:  # noqa: BLE001
        return _done(False, f"{type(ex).__name__}: {str(ex)[:200]}", 0)


def _done(same, why, ran, memo=None):
    print(json.dumps({"same": same, "why": why, "ran": ran, "memo": memo or {}}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
