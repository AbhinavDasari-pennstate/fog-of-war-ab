"""Replay TPC-DS queries on a patched tree and compare with the pristine digests.

Run as a child process (python -S -E tpcds_replay.py), job as JSON on stdin:
    {"src": pinned tree, "patched": {relpath: new text}, "plan": {"<q>": "OE"}, "map": path to tpcds.z,
     "per_query_s": float}
Prints one JSON line {"same": bool, "why": str, "ran": n}. `same` is True only when every replayed
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


def _fork_run(q, sql, meta, schema, tables, want, limit, expected=None):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            signal.alarm(max(1, int(limit)))
            out = core.run_query(sql, meta, schema, tables, want=want, expected=expected)
            payload = json.dumps(out).encode()
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
    return job.get("per_query_s") or max(5.0, 8 * e["cpu"])


def _judge(q, e, meta, sql, schema, tables, want, job):
    """Does query q still pass? Only what the test can see decides: the executor's result and the
    string duckdb is given (a different intermediate tree does not matter). A query the test does
    not execute is only parsed and pickled, so for it only an error counts."""
    out = _fork_run(q, sql, meta, schema, tables, want, _limit(e, job), e)
    if "error" in out:
        return False, f"query {q}: {out['error']}"
    if not out["ok"]:
        return False, f"query {q}: shared tables or schema changed"
    if "E" in out and out["tables"] != e["tables"]:
        return False, f"query {q}: find_tables gave different tables"
    if not e["E"] and not e["tables"] and "E" in out:
        return False, f"query {q}: ran the executor on a query the test does not execute"
    for st, name in (("E", "result"), ("T", "transpile")):
        if st in out and out[st] not in e[st]:
            return False, f"query {q}: the {name} differs"
    return True, ""


def main():
    job = json.loads(sys.stdin.read())
    src = job["src"]
    tp = json.loads(zlib.decompress(open(job["map"], "rb").read()))
    plan = {int(q): st for q, st in job["plan"].items()}
    t0 = time.process_time()
    try:
        core.setup_path(src, job.get("patched"))
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
            if "O" in st or "E" in st:
                need.update(tp["queries"][str(q)]["tables"])
        tables = {t: core.load_table(src, t, schema[t]) for t in sorted(need)}
        order = sorted(plan, key=lambda q: tp["queries"][str(q)]["cpu"])
        ran = 0
        for q in order:
            e = tp["queries"][str(q)]
            if e["mutates"]:
                return _done(False, f"query {q} mutates shared state in the pristine run", ran)
            meta, sql = qs[q]
            ok, why = _judge(q, e, meta, sql, schema, tables, tuple(plan[q]), job)
            ran += 1
            if not ok:
                return _done(False, why, ran)
        return _done(True, "every replayed query matches", ran)
    except BaseException as ex:  # noqa: BLE001
        return _done(False, f"{type(ex).__name__}: {str(ex)[:200]}", 0)


def _done(same, why, ran):
    print(json.dumps({"same": same, "why": why, "ran": ran}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
