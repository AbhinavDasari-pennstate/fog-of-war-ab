"""Which TPC-DS queries can a patch reach? (stdlib only)

    TpcdsMap.load(path)                       the map written by tools/build_tpcds.py
    scope(changes, tp, src_root)  ->  Scope   what the replay has to run, or why the test stays

A Scope is `keep` (the giant test stays selected, `why` says which rule) or `replay` with a bitmask
of the queries whose own coverage reaches a changed place. Three layers, each of which can only
widen:
  1. A file other than a modified sqlglot/**.py cannot be replayed (data and fixture files, new,
     deleted and renamed files, test files, sqlglotc): keep.
  2. The patched file must parse, and its "shell" (the module with every outermost function body
     blanked: imports, class bodies, class tables, lambdas in them, def headers, decorators,
     defaults, annotations) must be AST-identical to the pristine one. Anything run at import or
     class creation is inside the shell: keep.
  3. Each outermost function whose AST changed is a unit. A change that replaces lines one for one
     is narrowed to the statements it touches; any other change (lines added, lines removed,
     an `else:` or `finally:` line) takes the whole function body. A touched line that ran outside
     every query (importing all of sqlglot, setUpClass) keeps the test. Otherwise the queries are
     the union of the per-query coverage of those lines (0 queries: the changed code is never
     reached by the test, nothing to run).
"""

import ast
import json
import zlib
from dataclasses import dataclass

import diffparse
import pyscope

_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
             ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match)
if hasattr(ast, "TryStar"):
    _COMPOUND += (ast.TryStar,)
_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


STAGES = "PBOET"  # parse + pickle; find_tables, ensure_tables, schema build; optimize(); plan + executor; transpile


@dataclass
class Scope:
    kind: str  # "keep" | "replay"
    plan: dict = None  # query number -> stages ("P", "O", "E", "T") whose coverage reaches a change
    why: str = ""

    @property
    def mask(self) -> int:
        return sum(1 << q for q in (self.plan or {}))


class TpcdsMap:
    def __init__(self, raw: dict):
        self.raw = raw
        self.n = raw["n"]
        self.cov = {f: {int(k): [int(x, 16) for x in v] for k, v in lines.items()} for f, lines in raw["cov"].items()}
        self.base = {f: set(v) for f, v in raw["base"].items()}
        self.q = {int(k): v for k, v in raw["queries"].items()}
        self.unstable = set(raw["unstable"])

    table_cpu: dict = {}

    @classmethod
    def load(cls, path) -> "TpcdsMap":
        import os

        with open(path, "rb") as f:
            m = cls(json.loads(zlib.decompress(f.read())))
        try:
            with open(os.path.join(os.path.dirname(path), "tpcds_tables.json")) as f:
                m.table_cpu = json.load(f)
        except OSError:
            pass
        return m

    FIXED_S = 0.30  # interpreter, map, import of every module, fixture parse, one fork

    def cost(self, plan: dict) -> float:
        """Native CPU seconds the replay takes (build-machine measurements, calibrated against real
        replays within a few percent): the stages each query runs (P always; O or E means O, E and T
        run; T alone is independent) plus loading the tables those queries read plus the fixed start."""
        total = self.FIXED_S
        need = set()
        for i, st in plan.items():
            sc = self.q[i]["stage_cpu"]
            st = set(st)
            ran = {"P"} | st
            if st & {"B", "E"}:
                ran |= {"B", "O", "E", "T"}  # the executor runs to the end
            elif "O" in st:
                ran |= {"B", "O"}  # stops after optimize when the tree is pristine
            if ran & {"B", "O", "E"}:
                need.update(self.q[i]["tables"])
            total += sum(sc.get(x, 0.0) for x in ran)
        return total + sum(self.table_cpu.get(t, 0.05) for t in need)

def _units(tree):
    """Outermost function defs, in source order, keyed (qualified name, ordinal)."""
    out, counts = [], {}

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, _FUNC):
                q = prefix + ch.name
                k = counts.get(q, 0)
                counts[q] = k + 1
                out.append(((q, k), ch))
            elif isinstance(ch, ast.ClassDef):
                walk(ch, prefix + ch.name + ".")
            else:
                walk(ch, prefix)

    walk(tree, "")
    return out


_bind_sig = pyscope.bind_sig


def _shell_and_units(tree):
    """({key: (dump of the whole function, its binding set)}, dump of the module with function
    bodies blanked)."""
    units = _units(tree)
    dumps = {key: (ast.dump(node), _bind_sig(node)) for key, node in units}
    for _, node in units:
        node.body = [ast.Pass()]
    return dumps, ast.dump(tree)


def _blocks(change):
    """(removed [(old_lineno, text)], added [text], p) per contiguous edit; p = old line before it."""
    blocks = []
    for h in change.hunks:
        cur = h.old_start if h.old_count else h.old_start + 1
        blk = None
        for op, text in h.lines:
            if op == " ":
                blk = None
                cur += 1
            else:
                if blk is None:
                    blk = ([], [], cur - 1)
                    blocks.append(blk)
                if op == "-":
                    blk[0].append((cur, text))
                    cur += 1
                else:
                    blk[1].append(text)
    return blocks


def _stmt_lines(unit_node, ln):
    """Lines of the innermost statement holding ln (a compound statement counts only its header),
    or None when ln belongs to no statement's own text (an else: line, a comment ...)."""
    best = None
    for node in ast.walk(unit_node):
        if isinstance(node, ast.stmt) and node.lineno <= ln <= (node.end_lineno or node.lineno):
            if best is None or (node.end_lineno - node.lineno) < (best.end_lineno - best.lineno):
                best = node
    if best is None:
        return None
    if isinstance(best, _COMPOUND):
        first = min([best.lineno] + [d.lineno for d in getattr(best, "decorator_list", [])])
        body = getattr(best, "body", None) or getattr(best, "cases", None)
        hdr_end = max(best.lineno, (body[0].lineno - 1) if body and hasattr(body[0], "lineno") else best.lineno)
        if not first <= ln <= hdr_end:
            return None
        return range(first, hdr_end + 1)
    return range(best.lineno, best.end_lineno + 1)


def _keep(why):
    return Scope("keep", 0, why)


def _scope_file(change, tp: TpcdsMap, src_root) -> tuple[dict, str] | str:
    """({query: stages}, why) for one modified sqlglot file; (None, why) for an import-time edit (every
    query must be replayed); a string (the reason to keep) when the file cannot be judged."""
    from pathlib import Path

    path = change.path_new
    old_text = (Path(src_root) / path).read_bytes().decode("utf-8")
    new_text = diffparse.apply(old_text, change)
    try:
        old_tree = ast.parse(old_text, path)
        new_tree = ast.parse(new_text, path)
        compile(new_tree, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
        return f"{path}: patched file does not parse or compile ({type(e).__name__})"
    old_units = _units(old_tree)
    old_nodes = dict(old_units)
    old_d, old_shell = _shell_and_units(old_tree)
    new_d, new_shell = _shell_and_units(new_tree)
    if old_shell != new_shell:
        return None, f"{path}: import-time edit (module or class level, a def header or a decorator)"
    if set(old_d) != set(new_d):
        return None, f"{path}: import-time edit (functions added or removed)"
    changed = [k for k in old_d if old_d[k][0] != new_d[k][0]]
    rebinds = {k for k in changed if old_d[k][1] != new_d[k][1]}
    if not changed:
        return {}, f"{path}: AST of every function unchanged"
    # old_tree bodies were blanked by _shell_and_units: reparse for the statement lookups
    tree = ast.parse(old_text, path)
    nodes = dict(_units(tree))
    cov = tp.cov.get(path, {})
    base = tp.base.get(path, set())
    blocks = _blocks(change)
    plan: dict[int, set] = {}
    notes = []
    for key in changed:
        node = nodes[key]
        span_lo = min([node.lineno] + [d.lineno for d in node.decorator_list])
        span_hi = node.end_lineno
        body_lo = node.body[0].lineno
        mine = []
        for removed, added, p in blocks:
            if removed:
                hit = any(span_lo <= ln <= span_hi for ln, _ in removed)
            else:
                hit = span_lo <= p <= span_hi or span_lo <= p + 1 <= span_hi
            if hit:
                mine.append((removed, added, p))
        lines: set = set()
        narrow = bool(mine)
        for removed, added, p in mine:
            if not removed or len(added) != len(removed) or any(not body_lo <= ln <= span_hi for ln, _ in removed):
                narrow = False
                break
            for ln, _ in removed:
                got = _stmt_lines(node, ln)
                if got is None:
                    narrow = False
                    break
                lines.update(got)
            if not narrow:
                break
        if key in rebinds:
            narrow = False
        whole = set(range(body_lo, span_hi + 1))

        def reach(ls):
            m4 = [0] * 5
            for ln in ls:
                for si, v in enumerate(cov.get(ln, (0,) * 5)):
                    m4[si] |= v
            return m4

        if not narrow:
            lines = whole
        ms = reach(lines)
        if narrow and not any(ms):  # the changed statements were never reached: whoever ran the function decides
            narrow = False
            lines = whole
            ms = reach(lines)
        if lines & base:
            return None, f"{path}:{key[0]}: import-time edit (the changed code also runs at import or in setUpClass)"
        for si, v in enumerate(ms):
            q = 0
            while v:
                if v & 1:
                    plan.setdefault(q, set()).add(STAGES[si])
                v >>= 1
                q += 1
        notes.append(f"{key[0]} {'stmt' if narrow else 'func'} {'/'.join(str(bin(v).count('1')) for v in ms)}q")
    return plan, f"{path}: " + ", ".join(notes)


import os

IMPORT_REPLAY = os.environ.get("BR_IMPORT_REPLAY") == "1"  # replay every query for an import-time edit (off by default)
SQL_FIXTURE = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"


def _pairs(src_root, text=None):
    """(meta, sql) of every pair, through the pinned tests/helpers.py. With `text`, the fixture is
    read from a temporary copy holding that text instead of the file on disk."""
    import importlib.util
    import os
    import tempfile

    spec = importlib.util.spec_from_file_location("br_helpers_scope", os.path.join(src_root, "tests", "helpers.py"))
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    if text is None:
        return [(m, q) for m, q, _ in h.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")]
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "optimizer", "tpc-ds")
        os.makedirs(d)
        with open(os.path.join(d, "tpc-ds.sql"), "w", encoding="utf-8", newline="") as f:
            f.write(text)
        h.FIXTURES_DIR = tmp
        return [(m, q) for m, q, _ in h.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")]


def _fixture_same(change, src_root) -> bool:
    """True when the patched tpc-ds.sql gives the test the same (meta, sql) list. The test reads
    nothing else from the file (the third part of each pair, the optimized SQL, is not used)."""
    from pathlib import Path

    old = (Path(src_root) / SQL_FIXTURE).read_bytes().decode("utf-8")
    new = diffparse.apply(old, change)
    return _pairs(src_root) == _pairs(src_root, new)


def scope(per_file, tp: TpcdsMap, src_root) -> Scope:
    """per_file: [(change, selects_1106)] for every changed file. Files the rules say the test does
    not depend on are ignored; every other file must be a modified sqlglot/**.py."""
    plan: dict[int, set] = {}
    notes = []
    import_time = False
    for change, depends in per_file:
        if not depends:
            continue
        path = change.path_new
        if path == SQL_FIXTURE and change.status == "modified" and change.path_old == path:
            try:
                same = _fixture_same(change, src_root)
            except Exception as e:
                return _keep(f"{path}: {type(e).__name__}: {e}")
            if same:
                notes.append(f"{path}: the (meta, sql) pairs the test reads are unchanged")
                continue
            return _keep(f"{path}: a query the test runs changed")
        if (change.status != "modified" or change.path_old != path or not path.endswith(".py")
                or not path.startswith("sqlglot/")):
            return _keep(f"{path}: {change.status}, not a modified sqlglot module")
        try:
            got = _scope_file(change, tp, src_root)
        except Exception as e:  # any doubt keeps the test
            return _keep(f"{path}: {type(e).__name__}: {e}")
        if isinstance(got, str):
            return _keep(got)
        p, why = got
        notes.append(why)
        if p is None:
            import_time = True
            continue
        for q, st in p.items():
            plan.setdefault(q, set()).update(st)
    if import_time and not IMPORT_REPLAY:
        return _keep("; ".join(notes) + " (import-time replay is off: it never passed the CPU gate with 5 seeds)")
    if import_time:
        # An edit that runs at import or class creation can be read by any query: the child imports the
        # patched tree fresh (every module, as a pytest session has), and every query is replayed.
        return Scope("replay", {q: ("E" if tp.q[q]["tables"] else "P") for q in range(tp.n)}, "; ".join(notes))
    return Scope("replay", {q: "".join(x for x in STAGES if x in st) for q, st in plan.items()}, "; ".join(notes))
