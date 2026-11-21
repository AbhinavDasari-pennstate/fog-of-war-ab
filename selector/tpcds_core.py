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
import collections
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


def import_all(src):
    """Import every sqlglot module and every dialect, as a whole pytest session has by the time
    the test runs; the recorder and the replay both start from this state."""
    import pkgutil

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


def state_fp(_items=None):
    """What a query can leave behind in sqlglot's globals for the next one: for every module and
    class dict of sqlglot, the size and a digest of the keys of each mutable container, the size of
    each lru_cache and the position of each counter. Values are not read (cheap, and reading them
    could run sqlglot code). Not a proof of equal state, a tripwire for the usual carriers of
    cross-query state (caches, registries, id counters)."""
    import functools
    import itertools

    items = []

    def one(owner, name, v):
        t = type(v)
        if t in (dict, set, list, collections.deque, collections.OrderedDict, collections.defaultdict, collections.Counter):
            n = len(v)
            keys = 0
            if n <= 48 and t is not list:  # small registries: the keys too (cheap)
                try:
                    keys = sha(sorted(map(repr, v)))
                except Exception:
                    keys = -1
            items.append((owner, name, t.__name__, n, keys))
        elif t is functools._lru_cache_wrapper:
            items.append((owner, name, "lru", v.cache_info().currsize))
        elif t is itertools.count:
            items.append((owner, name, "count", repr(v)))

    for mname in sorted(sys.modules):
        mod = sys.modules[mname]
        if not mname.startswith("sqlglot") or mod is None:
            continue
        for name, v in sorted(vars(mod).items()):
            if name.startswith("__") and name.endswith("__"):  # __builtins__ differs with -S, __all__ ...
                continue
            if isinstance(v, type) and (v.__module__ or "") == mname:
                for n2, v2 in sorted(vars(v).items()):
                    one(f"{mname}.{name}", n2, v2)
            else:
                one(mname, name, v)
    if _items is not None:
        _items.extend(items)
    return sha(items)


class Stop(Exception):
    pass


# What the pinned executor does with a hash seed (audit of executor/python.py, env.py, table.py, context.py, planner.py):
#   python.py set_operation  UNION (distinct): list(set(left.rows).union(set(right.rows))), row order follows the string hashes
#   env.py ARRAYUNIQUEAGG    list(set(acc))
#   python.py _execute, planner dag: sets of Step objects (hash by id, not by seed; scheduling only)
#   INTERSECT / EXCEPT keep the left order (Counter and a membership set); joins and groups are insertion-ordered dicts.
# An optimized tree that holds a set operation or ArrayUniqueAgg (or anything unknown) is never reused across seeds.
_SEED_SENSITIVE = frozenset(("Union", "Intersect", "Except", "SetOperation", "ArrayUniqueAgg", "Anonymous"))
# generators/python.py compiles Distinct to set(...): FIRST, ARRAYAGG and the like expose its order. Only an order-free
# aggregate directly around it is safe.
_ORDER_FREE_PARENTS = frozenset(("Count", "Min", "Max", "Sum", "Avg"))


def _names(node):
    return {c.__name__ for c in type(node).__mro__}


def seed_sensitive(tree) -> bool:
    try:
        for node in tree.walk():
            n = _names(node)
            if n & _SEED_SENSITIVE:
                return True
            if "Distinct" in n and not (node.parent is not None and type(node.parent).__name__ in _ORDER_FREE_PARENTS):
                return True
        return False
    except BaseException:  # noqa: BLE001
        return True


class NeedFull(Exception):
    """A probe run (no tables loaded) reached a point where the executor must run."""


STAGES = ("P", "B", "O", "E", "T")
# P parse + pickle round trip; B find_tables, ensure_tables, the schema build: everything execute() does
# before it calls optimize(); O the optimize() call; E plan + executor + result pickle; T transpile.
# B and E feed the executor without passing through the optimized tree, so a changed line in either
# means the executor must run. Only a change confined to O can be judged by the optimized tree.


def run_query(sql, meta, schema, tables, want=("B", "O", "E", "T"), hook=None, expected=None, stop_at_o=False, probe=False):
    """One query through the pipeline. Stage P always runs. `want` names the stages that execute a
    changed line (the recorder asks for all of them).
      B or E wanted, or (with `expected`) a P digest that differs from the pristine one: the executor
        runs to the end and its result decides;
      only O wanted: optimize() runs; if its output tree digest is the pristine one the run stops
        there (the executor's inputs are then identical: B and E run no changed line); otherwise the
        executor runs to the end;
      T (transpile, which only reads the SQL string) runs when wanted, and always after an executor run;
      otherwise only P ran, and its digest decides.
    `stop_at_o` (the recorder) stops after optimize whatever the digest.
    `probe` (the replay, later seeds): the tables are not touched (execute gets none), which is exact for a run
      that stops inside optimize; a run that would need the executor raises NeedFull instead and the caller
      repeats the query without `probe`.
    Returns {stage: digest} for the stages that ran, "ok" (the shared tables and schema came out as
    they went in), "tables" (what find_tables gave), "G" (global fingerprint keyed by stages run).
    `hook(stage)` is called when a stage ends. Raises on any error."""
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
    full = runs and (bool(want & {"B", "E"}) or p_diff) and not stop_at_o
    if runs and (full or "O" in want or stop_at_o):
        used = sorted({t.name for t in find_tables(expression)})
        if probe:
            if full:
                raise NeedFull()
            before = {}
        else:
            before = {n: _rows_hash(tables[n]) for n in used}
        orig = ex.optimize
        state = {"stopped": False}

        def optimize(*a, **k):
            end("B")
            res = orig(*a, **k)
            out["O"] = digest(res)
            end("O")
            if not full and (stop_at_o or (expected is not None and out["O"] in expected["O"])):
                state["stopped"] = True
                raise Stop()
            if probe:
                raise NeedFull()
            out["risky"] = seed_sensitive(res)
            return res

        ex.optimize = optimize
        result = None
        try:
            result = ex.execute(expression, schema=schema, tables={} if probe else {n: tables[n] for n in used})
        except Stop:
            pass
        finally:
            ex.optimize = orig
        if not state["stopped"]:
            if "O" not in out:
                raise RuntimeError("optimize was not called")
            result = pickle.loads(pickle.dumps(result))  # back to the parent
            out["E"] = digest((list(result.columns), [tuple(r) for r in result.rows]))
            end("E")
            want.add("T")  # the state fingerprint is comparable only for the whole pipeline
    if runs and "T" in want and not stop_at_o:
        out["T"] = sha(transpile(sql, write="duckdb")[0])
        end("T")
    out["G"] = {"".join(st for st in "PEOT" if st in out): state_fp()}
    out["ok"] = (probe or all(_rows_hash(tables[n]) == before[n] for n in used)) and repr(schema) == schema_before
    out["tables"] = used
    return out
