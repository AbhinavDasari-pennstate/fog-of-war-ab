"""Per-query TPC-DS pipeline shared by the map recorder (tools/tpcds_probe.py) and the run-time
replay (selector/tpcds_replay.py). Stdlib only; sqlglot is imported from the tree it is given.

One query goes through exactly the steps tests/test_executor.py::TestExecutor::test_execute_tpcds
takes, minus duckdb and pandas:
  parent  : expression = parse_one(sql); the pool pickles (expression, meta)
  worker  : unpickle; mp_execute (find_tables, execute with the shared schema and tables);
            the result is pickled back
  parent  : transpile(sql, write="duckdb")[0] is what duckdb runs (the test compares duckdb's
            answer with the executor's, so an identical string plus an identical result means an
            identical comparison)
The digests below are sha256 over repr(), so a type change (1 versus 1.0 versus True) shows.
"""

import ast
import csv
import gzip
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import pickle
import sys

TPCDS_DIR = "optimizer/tpc-ds/"
SQL_FIXTURE = TPCDS_DIR + "tpc-ds.sql"


def sha(obj) -> str:
    return hashlib.sha256(repr(obj).encode("utf-8", "surrogatepass")).hexdigest()[:20]


# ---- the patched tree as an import overlay ----------------------------------------------------
class _PatchedLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, path, text):
        super().__init__(fullname, path)
        self._text = text

    def get_code(self, fullname):  # never read or write a .pyc: the cached one is the pristine file
        return self.source_to_code(self._text.encode("utf-8"), self.path)


class _Overlay(importlib.abc.MetaPathFinder):
    def __init__(self, src, files):
        self.src = os.path.realpath(src)
        self.files = {os.path.join(self.src, rel): text for rel, text in files.items()}

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("sqlglot"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.origin is None:
            return None
        origin = os.path.realpath(spec.origin)
        if origin in self.files:
            spec.loader = _PatchedLoader(fullname, spec.origin, self.files[origin])
        return spec


def setup_path(src, patched=None):
    """Put the tree on sys.path; serve `patched` ({relpath: new text}) instead of the files on disk."""
    sys.dont_write_bytecode = True
    src = os.path.abspath(src)
    if src not in sys.path:
        sys.path.insert(0, src)
    if patched:
        sys.meta_path.insert(0, _Overlay(src, patched))


def load_helpers(src):
    """tests/helpers.py by path (it only needs os), so the tests package is never imported."""
    spec = importlib.util.spec_from_file_location("br_helpers", os.path.join(src, "tests", "helpers.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def queries(src, helpers=None):
    h = helpers or load_helpers(src)
    return [(meta, sql) for meta, sql, _ in h.load_sql_fixture_pairs(SQL_FIXTURE)]


def _open_file(file_name):
    with open(file_name, "rb") as f:
        gzipped = f.read(2) == b"\x1f\x8b"
    if gzipped:
        return gzip.open(file_name, "rt", newline="")
    return open(file_name, encoding="utf-8", newline="")


def load_table(src, table, columns):
    """The rows the test builds in setUpClass.setup (same code), as a sqlglot Table."""
    from sqlglot.executor.table import Table

    file_name = f"{os.path.join(src, 'tests', 'fixtures')}/{TPCDS_DIR}{table}.csv.gz"
    reader = csv.reader(_open_file(file_name), delimiter="|")
    rows = []
    ctypes = []
    next(reader)
    for row in reader:
        if not ctypes:
            for i, v in enumerate(row):
                try:
                    ctypes.append(type(ast.literal_eval(v)))
                except (ValueError, SyntaxError):
                    ctypes.append(str)
        rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return Table(columns=columns, rows=rows)


def table_names(expression):
    from sqlglot import find_tables

    return sorted({t.name for t in find_tables(expression)})


# ---- digests built from raw fields only ---------------------------------------------------------
_SKIP = frozenset(("parent", "_hash"))


def raw(o, seen=None):
    """Nested tuples of primitives describing o. Reads slots and __dict__ with object.__getattribute__
    and never calls a sqlglot method (__eq__, __hash__, __repr__ ...), so a patched one cannot hide a
    difference. `parent` and the cached `_hash` (hash-seed dependent) are left out."""
    import enum

    t = type(o)
    if o is None or t is int or t is str or t is bool:
        return o
    if t is float:
        return ("f", repr(o))
    if t is bytes:
        return ("b", o)
    if seen is None:
        seen = set()
    if t is list or t is tuple:
        return (t.__name__, tuple(raw(x, seen) for x in o))
    if t is dict:
        return ("d", tuple((raw(k, seen), raw(v, seen)) for k, v in o.items()))
    if t is set or t is frozenset:
        return ("s", tuple(sorted(repr(raw(x, seen)) for x in o)))
    if isinstance(o, enum.Enum):
        return ("e", t.__qualname__, str(o.value) if not isinstance(o.value, (int, str)) else o.value, o.name)
    names = set()
    for c in t.__mro__:
        sl = c.__dict__.get("__slots__", ())
        names.update((sl,) if isinstance(sl, str) else sl)
    try:
        d = object.__getattribute__(o, "__dict__")
    except AttributeError:
        d = {}
    names.update(d)
    if (names - _SKIP) and (t.__module__ or "").startswith(("sqlglot", "tests")):
        if id(o) in seen:
            return ("cycle", t.__qualname__)
        seen.add(id(o))
        out = []
        for n in sorted(names - _SKIP):
            try:
                v = object.__getattribute__(o, n)
            except AttributeError:
                continue
            out.append((n, raw(v, seen)))
        seen.discard(id(o))
        return ("o", f"{t.__module__}.{t.__qualname__}", tuple(out))
    return ("r", t.__qualname__, repr(o))


def digest(o):
    return sha(raw(o))


def _rows_hash(table):
    return hash(tuple(table.rows)), hash(table.columns), repr(table.column_range)


class Stop(Exception):
    pass


STAGES = ("P", "O", "E", "T")  # parse + pickle, find_tables + optimize, plan + executor, transpile


def run_query(sql, meta, schema, tables, want=("O", "E", "T"), hook=None, expected=None):
    """One query through the pipeline. Stage P (parse, pickle round trip) always runs.
    Without `expected` (the recorder), O, E and T run when named in `want`.
    With `expected` (the replay, the pristine digests of this query): `want` names the stages that
    execute a changed line. A stage that runs no changed line, fed an input identical to the
    pristine one, gives the pristine output, so the run stops as soon as a stage with digests
    equal to pristine is followed only by such stages:
      O runs when O or E is wanted, or when P differs; it stops there when its digest is pristine
        and E is not wanted; otherwise E runs;
      T (transpile, which only reads the SQL string) runs when wanted.
    Returns {stage: digest} for the stages that ran, "ok" (the shared tables and schema came out
    as they went in), "tables". `hook(stage)` is called when a stage ends. Raises on any error."""
    import sqlglot.executor as ex
    from sqlglot import find_tables, parse_one, transpile

    sys.setrecursionlimit(20000)
    want = set(want)
    out = {}

    def end(stage):
        if hook:
            hook(stage)

    schema_before = repr(schema)
    expression = parse_one(sql)
    payload = pickle.dumps((expression, meta))
    expression, meta = pickle.loads(payload)  # the worker's copy
    out["P"] = digest((expression, meta))
    end("P")
    p_diff = expected is not None and out["P"] not in expected["P"]
    used = []
    before = {}
    runs = bool(meta.get("execute"))
    if runs and (want & {"O", "E"} or p_diff):
        used = sorted({t.name for t in find_tables(expression)})
        before = {n: _rows_hash(tables[n]) for n in used}
        state = {"stopped": False}
        orig = ex.optimize

        def optimize(*a, **k):
            res = orig(*a, **k)
            out["O"] = digest(res)
            end("O")
            if expected is None:
                stop = "E" not in want
            else:
                stop = "E" not in want and out["O"] in expected["O"]
            if stop:
                state["stopped"] = True
                raise Stop()
            return res

        ex.optimize = optimize
        result = None
        try:
            result = ex.execute(expression, schema=schema, tables={n: tables[n] for n in used})
        except Stop:
            pass
        finally:
            ex.optimize = orig
        if state["stopped"]:
            pass
        elif "O" not in out:
            raise RuntimeError("optimize was not called")
        else:
            result = pickle.loads(pickle.dumps(result))  # back to the parent
            out["E"] = digest((list(result.columns), [tuple(r) for r in result.rows]))
            end("E")
    if runs and "T" in want:
        out["T"] = sha(transpile(sql, write="duckdb")[0])
        end("T")
    out["ok"] = all(_rows_hash(tables[n]) == before[n] for n in used) and repr(schema) == schema_before
    out["tables"] = used
    return out
