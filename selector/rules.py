"""Static test selection: rules R0 to R9 from plan/plan.md ("Selector rules").

    select(changes, maps, tests, src_root=None) -> (set of test numbers, reason)

changes: diffparse.parse() output. maps: maps.load.Maps. tests: the "tests" list of
tests.json. src_root: the pinned sqlglot checkout (old file text). Any doubt widens (R9).
Stdlib only, never imports sqlglot.
"""

import ast
import json
import bisect
import os
import re
from pathlib import Path

import diffparse
import attrrule
import pyscope
import valrule
import weakmut

DEFAULT_SRC = os.environ.get("BR_SRC", "/app/sqlglot-src")
FIXTURE_ROOT = "tests/fixtures"
TPCDS_FIXTURE = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
# Words that name no class-level definition; looking them up would only widen.
_TARGET = re.compile(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*(?::[^=]*)?=(?!=)")
STOP = frozenset({"True", "False", "None", "self", "cls", "lambda", "not", "and", "or", "in", "is",
                  "if", "else", "return", "for", "def", "class", "import", "from", "as"})


def _bits(mask: int) -> set:
    out = set()
    n = 0
    while mask:
        low = (mask & -mask).bit_length() - 1
        out.add(n + low)
        mask >>= low + 1
        n += low + 1
    return out


_INDEX: dict = {}  # id(tests) -> (tests, len, first, last, all, by_file)


def _index(tests):
    """(bitmask of every test, {file: bitmask of its tests}); built once for a tests list, not once per Ctx."""
    hit = _INDEX.get(id(tests))
    if hit is not None and hit[0] is tests and hit[1] == len(tests) and hit[2] is (tests[0] if tests else None)             and hit[3] is (tests[-1] if tests else None):
        return hit[4], hit[5]
    all_ = 0
    by_file: dict[str, int] = {}
    for t in tests:
        all_ |= 1 << t["number"]
        by_file[t["file"]] = by_file.get(t["file"], 0) | (1 << t["number"])
    if len(_INDEX) >= 4:
        _INDEX.pop(next(iter(_INDEX)))
    _INDEX[id(tests)] = (tests, len(tests), tests[0] if tests else None, tests[-1] if tests else None, all_, by_file)
    return all_, by_file


class Ctx:
    def __init__(self, maps, tests, src_root):
        self.m = maps
        self.src = Path(src_root)
        self.tests = tests
        self.all, self.by_file = _index(tests)
        self.notes: list[str] = []
        self.single = False  # the patch changes one file
        self.narrow = None  # (start line, mask) of a class-level assignment narrowed by maps/attrreads.bin
        self.old_tree = None  # the parsed old text of the file under edit (the AST proof of _import_only)
        self._attr = None
        self._val = None
        self.patched = None  # {relpath: new text} of every modified sqlglot module of the patch (set by select), for the import probe
        self._probe = None  # None: not run; False: unavailable (static widening); else {"mask": bits}
        self.doc_only = False  # every difference of the current file is the text of a string statement (a docstring)

    def attr(self):
        if self._attr is None:
            self._attr = attrrule.Attr(self.m.dir)
        return self._attr

    def val(self):
        if self._val is None:
            self._val = valrule.Val(self.m.dir)
        return self._val

    def note(self, s: str):
        self.notes.append(s)

    # ---- widening -------------------------------------------------------------------------
    def wide(self, path: str) -> int:
        """Every test that imports the module (R9); unknown module means the whole suite."""
        m = self.m.imports.get(path)
        sr = self.m.srcreaders.get(path, 0)
        if m is None:
            if path in self.by_file:
                return self.by_file[path] | sr
            return self.all
        return m | sr | self.m.files.get(path, 0)

    def imptime(self, path: str, cls: str, soft=None) -> int:
        """Tests that import a module whose body, while it imports, ran code the edit can reach (maps/impexec.bin).

        The coverage maps cannot see these runs: the map build preimports every module in the parent process, and a test
        that imports a module lazily (Dialect.get_or_raise) is not credited with the classes and names the import used. An edit
        of class- or module-level data (a keyword table, a parser or generator table) can change what such an import does, or make it
        raise, and then every test that imports that module fails. cls: the class holding the edited statement ("" at module level).
        soft: (first, last) line of the lambda the edit sits in: only that lambda's code can run. Otherwise a class-level edit is
        reached by the methods of the class (or of a subclass) that ran, a module-level one by the functions of the file that ran.
        The module being edited is left out: a change of what its own body does is the business of the class and name maps."""
        files, classes, ranges, state, base, _pinfo = self.m.impexec
        if state == "absent" or self.doc_only:
            return 0
        if state == "bad":
            return self.all
        mods = set()
        if soft:
            for lo, hi, ms in ranges.get(path, ()):
                if lo <= soft[1] and soft[0] <= hi:
                    mods.update(ms)
        elif cls:
            mods.update(classes.get(cls, ()))
        else:
            mods.update(files.get(path, ()))
        mods.discard(path)
        out = 0
        if path in base:  # `import sqlglot` loads this module: test_lazy_load (a subprocess) runs it for real
            for t in self.tests:
                if t["id"].endswith("test_dialect.py::TestDialect::test_lazy_load"):
                    out |= 1 << t["number"]
        for f in mods:
            out |= self.m.imports.get(f, self.all) | self.m.srcreaders.get(f, 0)
        if out:  # the static answer is an over-approximation: ask the probe what the patched tree really does at import
            pm = self.probe_mask()
            if pm is not None:
                return pm
        return out

    def probe_mask(self):
        """Exact answer to "does this patch make an import raise?" (selector/impprobe.py in a child, two hash seeds): the tests that import
        each module that now raises, plus test_lazy_load when `import sqlglot` loads something else. 0 when nothing changes.
        None when the probe cannot run or fails (callers keep the static widening)."""
        if self._probe is None:
            try:
                self._probe = self._run_probe() or False
            except Exception:
                self._probe = False
        return self._probe["mask"] if self._probe else None

    def _child(self, patched, seed):
        import subprocess
        import sys as _sys

        env = {"PYTHONHASHSEED": str(seed), "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"}
        job = json.dumps({"src": str(self.src), "patched": patched}).encode()
        r = subprocess.run([_sys.executable, "-S", os.path.join(os.path.dirname(os.path.abspath(__file__)), "impprobe.py")],
                           input=job, capture_output=True, env=env, timeout=float(os.environ.get("BR_PROBE_S", "60")))
        return json.loads(r.stdout.decode().strip().splitlines()[-1])

    def _run_probe(self):
        info = self.m.impexec[5]
        if not self.patched or info is None or os.environ.get("BR_NO_PROBE") == "1":
            return None
        failed = {}
        first = None
        for seed in range(int(os.environ.get("BR_PROBE_SEEDS", "2"))):
            res = self._child(self.patched, seed)
            first = first or res
            for n, fs in res["failed"]:
                failed.setdefault(n, set()).update(fs)
        base_ok = set(first["base"]) == set(info["base"]) and first["nclasses"] == info["nclasses"]
        if set(failed) <= set(info["failed"]) and base_ok:
            return {"mask": 0}
        pri = self._child({}, 0)  # confirm against this machine's pristine tree
        pf = {n for n, _ in pri["failed"]}
        new = {n: fs for n, fs in failed.items() if n not in pf}
        out = 0
        for n, fs in new.items():
            cands = set(fs)
            rel = n.replace(".", "/")
            for c in (rel + ".py", rel + "/__init__.py"):
                if c in self.m.imports or c in self.by_file:
                    cands.add(c)
            for c in cands:
                out |= self.wide(c)
        if set(first["base"]) != set(pri["base"]) or first["nclasses"] != pri["nclasses"]:
            for t in self.tests:
                if t["id"].endswith("test_dialect.py::TestDialect::test_lazy_load"):
                    out |= 1 << t["number"]
        return {"mask": out}

    def udef(self, path: str, d0: int, d1: int) -> int:
        lm = self.m.lines.get(path)
        if not lm:
            return 0
        sets = self.m.sets
        u = 0
        for ln in range(d0, d1 + 1):
            sid = lm.get(ln)
            if sid:
                u |= sets[sid]
        return u


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


def _stmts_near(ctx: Ctx, path: str, ln: int):
    """The statement holding ln, else the statements just before and after it."""
    s = ctx.m.stmt(path, ln)
    if s is not None:
        return [s]
    rec = ctx.m.stmts.get(path)
    if rec is None:
        return []
    starts = rec[0]
    i = bisect.bisect_right(starts, ln) - 1
    out = []
    for j in (i, i + 1):
        if 0 <= j < len(starts):
            s = ctx.m.stmt(path, starts[j])
            if s is not None:
                out.append(s)
    return out


def _tokens(texts):
    return {t for t in pyscope.names_in(texts) if t not in STOP and not t.isdigit()}


def _mentions(ctx: Ctx, names, cls: str) -> int:
    """Tests that depend on class- or module-level statements that mention a changed name.

    Inside a class the attribute is reached as Class.name from other statements, so only lines
    that qualify the name with the class's own name count (every class defines its own
    arg_types; an unqualified match would select the world)."""
    files, idx, qual = ctx.m.mentions
    simple = cls.rsplit(".", 1)[-1] if cls else ""
    out = 0
    for n in names:
        codes = qual.get((simple, n), ()) if cls else idx.get(n, ())
        for code in codes:
            f, ln = files[code // 100000], code % 100000
            s = ctx.m.stmt(f, ln)
            if s is None or (cls and s[2] == "function"):
                continue
            if s[5]:
                out |= ctx.m.names_class.get(s[5], 0) or ctx.m.files.get(f, 0)
            else:
                out |= ctx.m.files.get(f, 0)
    return out


_ENUM = re.compile(r"\b(Enum|AutoName|IntEnum|StrEnum|Flag|IntFlag)\b")


def _doc_only(old_tree, diffs) -> bool:
    """True when every difference is the text of a string constant that is a whole statement (a docstring, or an attribute
    docstring that is evaluated and thrown away). The only reader of a first-statement docstring is `__doc__`."""
    strs = {id(n.value) for n in ast.walk(old_tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
    return bool(diffs) and all(id(n) in strs for n, _ in diffs)


def _scope_stmts(node, path=()):
    """Every statement outside function bodies, with the path ((attr, index) steps from the module) that reaches it. A def is a leaf."""
    for fld in ("body", "orelse", "finalbody"):
        sub = getattr(node, fld, None)
        if not (isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt)):
            continue
        for i, st in enumerate(sub):
            here = path + ((fld, i),)
            yield st, here
            if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from _scope_stmts(st, here)
    for hi, h in enumerate(getattr(node, "handlers", ()) or ()):
        yield from _scope_stmts(h, path + (("handlers", hi),))


def _counterpart(tree, path):
    """The statement at `path` (see _scope_stmts) in another parse, or None when the shapes differ."""
    node = tree
    for fld, i in path:
        try:
            node = getattr(node, fld)[i]
        except (AttributeError, IndexError, TypeError):
            return None
    return node


_PLAIN = (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Assign, ast.AnnAssign, ast.Starred, ast.UnaryOp, ast.USub, ast.UAdd)


def _blank(node, plain=True):
    """Dump of `node` with bool and None constants blanked everywhere, and numbers blanked only where every ancestor is plain data
    (a dict, list, tuple or set display, an assignment). A number that is a call argument, a subscript, an operand of arithmetic or a
    condition can make the import raise (datetime.date(1900, 0, 1))."""
    if isinstance(node, ast.Constant):
        v = node.value
        if v is None or isinstance(v, bool) or (plain and isinstance(v, (int, float, complex))):
            return "C"
        return repr(v)
    if isinstance(node, ast.AST):
        here = plain and isinstance(node, _PLAIN + (ast.Module, ast.ClassDef, ast.Load, ast.Store, ast.keyword)) and not isinstance(node, ast.keyword)
        parts = []
        for f in node._fields:
            parts.append(f + "=" + _blank(getattr(node, f, None), here))
        return type(node).__name__ + "(" + ",".join(parts) + ")"
    if isinstance(node, list):
        return "[" + ",".join(_blank(x, plain) for x in node) + "]"
    return repr(node)


def _same_but_numbers(a, b) -> bool:
    """The two statements agree except for bool and None constants anywhere, and number constants in plain data."""
    return _blank(a) == _blank(b)


def _own_import_risk(old_tree, new_tree, old_text: str, diffs, ranges) -> bool:
    """True when an edit outside function bodies can change whether the edited module imports at all: the statement that holds it
    is not a plain assignment of a literal that cannot raise (a call such as exp.maybe_parse(...) at class level parses at import,
    `1 / 0` or a name that does not exist raises). Edits inside lambda bodies and docstrings do not run at import. A def or a decorator
    is the business of the import-time line sets (_func_level, the d0 rule). Any doubt is True."""
    stmts = list(_scope_stmts(old_tree))
    for node, deferred in diffs:
        if deferred is not None:
            continue
        lo = getattr(node, "lineno", None)
        if lo is None:  # a node without a position (a comprehension clause, an argument list): the edited lines decide
            if not ranges:
                return True
            lo, hi = min(a for a, _ in ranges), max(b for _, b in ranges)
        else:
            hi = getattr(node, "end_lineno", lo) or lo
        best = None
        for st, p in stmts:
            a = min([st.lineno] + [d.lineno for d in getattr(st, "decorator_list", ())])
            b = st.end_lineno or st.lineno
            if a <= hi and lo <= b and (best is None or b - a <= best[3] - best[2]):
                best = (st, p, a, b)
        if best is None:
            return True
        st = best[0]
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
            continue
        other = _counterpart(new_tree, best[1])
        if other is not None and type(other) is type(st) and _same_but_numbers(st, other):
            continue  # only bool, number or None constants differ: an argument of a call cannot make a plain constant raise
        if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None:
            if not attrrule.simple_value(st.value, old_text):
                return True
            if other is None or type(other) is not type(st) or other.value is None:
                return True
            if not attrrule.simple_value(other.value, old_text) or not attrrule.literal_ok(other.value):
                return True
            continue
        return True
    return False


def _is_enum(old_lines, start: int, cls: str) -> bool:
    """True when the class holding the statement at `start` has an enum base."""
    simple = cls.rsplit(".", 1)[-1]
    head = re.compile(r"^(\s*)class\s+" + re.escape(simple) + r"\b(.*)")
    i = start - 2
    while i >= 0:
        m = head.match(old_lines[i])
        if m:
            return bool(_ENUM.search(m.group(2)))
        i -= 1
    return False


# The parts of an eagerly evaluated literal or expression: every sub-expression runs when the statement runs.
# Not here: Lambda, a comprehension or generator (run later or per item), IfExp, BoolOp and Compare (short circuit),
# NamedExpr, Await, Yield: a line of those may legitimately have no record because it was never reached.
_EAGER = (ast.Constant, ast.Name, ast.Attribute, ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Starred, ast.UnaryOp,
          ast.BinOp, ast.JoinedStr, ast.FormattedValue, ast.Call, ast.keyword, ast.Subscript, ast.Slice,
          ast.operator, ast.unaryop, ast.expr_context)


def _eager_stmt(tree, start: int, end: int) -> bool:
    """P1 proof: lines start..end are exactly one simple statement (Assign, AnnAssign or Expr) and everything in it
    is evaluated when it runs, so a line of it without a coverage record is an interior line of a statement that
    ran, not code that was skipped."""
    hits = []
    stack = [tree]
    while stack:
        node = stack.pop()
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.stmt):
                if ch.lineno > end or ch.end_lineno < start:
                    continue
                if ch.lineno == start and ch.end_lineno == end:
                    hits.append(ch)
            stack.append(ch)
    if len(hits) != 1 or not isinstance(hits[0], (ast.Assign, ast.AnnAssign, ast.Expr)):
        return False
    return all(n is hits[0] or isinstance(n, _EAGER) for n in ast.walk(hits[0]))


def _import_only(ctx: Ctx, path: str, start: int, end: int, il, lm) -> bool:
    """L2 shortcut: the statement on start..end only ran at import (who ran it lazily is not who depends on it).
    True when every line was marked import-time, as before; or, new (P1), when every line with a record is
    import-time, at least one is, and the AST shows the lines without a record are interior lines of one eagerly
    evaluated statement (a 264-line set literal has one recorded line). Anything else keeps today's rule."""
    span = range(start, end + 1)
    if all(l in il for l in span):
        return True
    if not any(l in il for l in span) or any(l in lm and l not in il for l in span):
        return False
    return ctx.old_tree is not None and _eager_stmt(ctx.old_tree, start, end)


def _class_level(ctx: Ctx, path: str, s, ln: int, texts, old_lines, removed_texts=(), added_texts=(), soft=False) -> int:
    return _class_level_core(ctx, path, s, ln, texts, old_lines, removed_texts, added_texts, soft) | ctx.imptime(path, s[5], soft)


def _class_level_core(ctx: Ctx, path: str, s, ln: int, texts, old_lines, removed_texts=(), added_texts=(), soft=False) -> int:
    m = ctx.m
    start, end, kind, d0, d1, cls = s
    il = m.importtime.get(path, ())
    un = m.unstable.get(path, ())
    sets = m.sets
    lm = m.lines.get(path, {})
    if soft:
        # the edit sits inside a lambda body: it runs only when the lambda is called, and the tests
        # that called it are the ones that ran a line of the lambda in a test context (unstable
        # lines are unions over fork and non-fork runs, so complete)
        # A line that only the sequential coverage pass credited (maps/seqcredit.bin) gives the first importer, not a caller: skipped.
        kx = m.seqcredit.get(path, ())
        u = 0
        for l2 in range(soft[0], soft[1] + 1):
            sid = lm.get(l2)
            if sid and l2 not in kx:
                u |= sets[sid]
        if u:
            return u
    # A continuation line of a multi-line class/module statement that never ran at import
    # (a lambda body inside a table) is credited exactly by coverage.
    sid = lm.get(ln)
    if start < end and sid and ln not in il and ln not in un:
        return sets[sid]
    nr = ctx.narrow
    if nr is not None and nr[0] == start and _import_only(ctx, path, start, end, il, lm):
        out = 0  # L2: a statement that only runs at import: who ran it lazily is not who depends on it
    else:
        out = m.stmt_mask(path, ln)
    if d0:  # def header or decorator: callers run the body, so the def's coverage decides
        out |= ctx.udef(path, d0, d1)
        if any(l in il for l in range(end + 1, d1 + 1)):  # body lines only: the header always runs at import
            # a line of the body ran at import time: something calls the function while a module or
            # class is being built, so a changed decorator or signature changes what every importer
            # gets (or fails to import). Those lines' test sets say who ran them lazily, not who
            # depends on them: widen to every test that imports the module.
            out |= ctx.wide(path)
        name = pyscope.def_name(old_lines[d0 - 1]) if d0 - 1 < len(old_lines) else ""
        if name:
            out |= m.names_attr.get(name, 0)
    first = old_lines[start - 1] if start - 1 < len(old_lines) else ""
    if first.lstrip().startswith("class ") and ln == start:  # bases or keywords of the class change
        return out | (m.names_class.get(cls, 0) if cls else 0) | m.files.get(path, 0) * (0 if cls else 1)
    # names whose value flows to other class or module statements at import time
    diff = set(_tokens(removed_texts)) ^ set(_tokens(added_texts))
    if ln == start and _TARGET.match(first):
        diff.add(_TARGET.match(first).group(1))
    out |= _mentions(ctx, diff, cls)
    # B: tests that ran a line naming the assigned attribute or a changed name or key. Not an
    # upper bound: class attributes are often read once at class creation (metaclass), so a
    # class-level change selects every test that uses the class or a subclass.
    names = _tokens(texts)
    t = _TARGET.match(first)
    if t:
        names.add(t.group(1))
    b = 0
    for n in names:
        b |= m.names_attr.get(n, 0)
    a = m.names_class.get(cls, 0) if cls else 0
    if cls and _is_enum(old_lines, start, cls):
        # members are read as Class.NAME or by value from lines tests run; the class is never instantiated
        for n in (t.group(1) if t else "", cls.rsplit(".", 1)[-1]):
            out |= m.names_attr.get(n, 0)
    nr = ctx.narrow
    if nr is not None and nr[0] == start:
        # L2: only the tests that read this attribute (recorded); a test with no recording stays only if today's rule keeps it
        out |= nr[1] | (nr[2] & (a or (b | m.files.get(path, 0))))
        ctx.note(f"L2 {path}:{start} attribute read map")
    elif a:
        out |= a
    else:
        out |= b | m.files.get(path, 0)
    if out == 0 and not (nr is not None and nr[0] == start):
        out = ctx.wide(path)
    return out


def _func_level(ctx: Ctx, path: str, s) -> int:
    m = ctx.m
    start, end, kind, d0, d1, cls = s
    il = m.importtime.get(path, ())
    un = m.unstable.get(path, ())
    out = m.stmt_mask(path, start)
    lines = range(start, end + 1)
    if out == 0 or any(l in un for l in lines):
        out |= ctx.udef(path, d0, d1)
    if any(l in il for l in lines):
        out |= ctx.wide(path)
    return out


def _py_lines(ctx: Ctx, path: str, change, old_lines, is_test_file: bool, soft=lambda ln: None) -> int:
    m = ctx.m
    out = 0
    if m.stmts.get(path) is None:
        # no statement table: never executed by a test; tests that import or read it decide
        return ctx.wide(path) if path in m.imports else m.srcreaders.get(path, 0)
    for removed, added, p in _blocks(change):
        touched = [ln for ln, _ in removed]
        if not removed or len(added) > len(removed):
            touched += [p, p + 1]
        texts = [t for _, t in removed] + added
        for ln in touched:
            for s in _stmts_near(ctx, path, ln):
                kind = s[2]
                if kind == "function":
                    out |= _func_level(ctx, path, s)
                elif is_test_file:
                    out |= ctx.by_file.get(path, 0)
                else:
                    out |= _class_level(ctx, path, s, ln, texts, old_lines, [t for _, t in removed], added, soft(ln))
        if not _stmts_near_any(ctx, path, touched):
            out |= ctx.wide(path)
    return out


def _stmts_near_any(ctx, path, touched):
    return any(_stmts_near(ctx, path, ln) for ln in touched)


def _reads(ctx: Ctx, path: str) -> int:
    m = ctx.m
    out = m.reads.get(path, 0)
    parts = path.split("/")
    for i in range(1, len(parts)):  # directories listed or walked by a test
        out |= m.reads.get("/".join(parts[:i]), 0)
    return out


def _comment_or_blank(text: str) -> bool:
    # the loader splits on more than \n (\r, \x0b, \x0c, \x1c-\x1e, \x85, U+2028, U+2029)
    if len(text.splitlines()) > 1 or (text and text.splitlines() == []):
        return False
    return text == "" or text.startswith("--")


def _data_file(ctx: Ctx, path: str, change) -> tuple[int, str]:
    """R1 and R2: a non-Python file."""
    m = ctx.m
    if path.endswith(".sql") and change is not None and change.hunks and change.status == "modified":
        texts = [t for h in change.hunks for op, t in h.lines if op in "+-"]
        if all(_comment_or_blank(t) for t in texts):
            return 0, f"R2 {path}: only comment or blank lines (dropped by _filter_comments)"
    out = _reads(ctx, path)
    if path == TPCDS_FIXTURE and change is not None and change.status == "modified" and change.path_old == path:
        # tests/test_executor.py reads this file only in setUpClass (`cls.tpcds_sqls = list(load_sql_fixture_pairs(...))`)
        # and then uses the SQL of each pair; the third part (the optimized text) is bound to `_` and never looked at, and
        # test_optimizer's test_tpcds is the only other reader. When the (meta, sql) list the helper builds from the patched
        # text equals the pristine one (and building it does not raise), no test of that file can see the edit.
        try:
            import tpcds_scope

            if tpcds_scope._fixture_same(change, ctx.src):
                return out & ~ctx.by_file.get("tests/test_executor.py", 0), f"R2 {path}: the (meta, sql) pairs test_executor.py reads are unchanged"
        except Exception:
            pass
    if path.startswith(FIXTURE_ROOT + "/") and path not in m.reads:
        for k, v in m.reads.items():  # unknown fixture: everything that reads fixtures
            if k.startswith(FIXTURE_ROOT) and not k.endswith(".gz"):
                out |= v
        return out, f"R2 {path}: not in reads, all fixture readers"
    return out, f"{'R2' if path.startswith('tests/') else 'R1'} {path}"


# Where a def's signature can be inspected, a default value could in principle be read. grep of the pinned
# sqlglot/ and tests/ finds no read of __defaults__, __kwdefaults__ or Parameter.default; the only inspect calls are
#   sqlglot/optimizer/optimizer.py:111  inspect.getfullargspec(rule).args           every rule handed to optimize()
#   sqlglot/executor/env.py:101         inspect.signature(func).parameters          defs under @null_if_any(...)
# and both use parameter names only. tests/test_typing.py compares __code__ of the annotators in sqlglot/typing (a nested
# def's default sits in the enclosing function's co_consts); those files are source readers (R7b), selected anyway.
# A def that those calls can reach keeps the wider rule, so a later read of defaults cannot be missed: every def under
# sqlglot/optimizer/ (any module-level def may be passed as a rule) and every def in executor/env.py.
_SIGNATURE_READ = ("sqlglot/optimizer/", "sqlglot/executor/env.py")


def _nested_defs(tree) -> set:
    """ids of the defs that sit inside another def (their defaults are constants of the enclosing function's code)."""
    out = set()
    stack = [(tree, False)]
    while stack:
        node, inside = stack.pop()
        for ch in ast.iter_child_nodes(node):
            func = isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if func and inside and not isinstance(ch, ast.Lambda):
                out.add(id(ch))
            stack.append((ch, inside or func))
    return out


def _never_ran(ctx: Ctx, path: str, fn, nested: set) -> bool:
    """P3: True when it is proven that no test could have run the body of `fn`, so no call reads its defaults.

    The proof is the absence of every kind of record on the body lines (a test line, an import-time line, an
    unstable line: any of them means something ran or may run it), in a file that has line data at all, for a
    body made of statements coverage always records (a docstring or `...` leaves no line event), in a def that is
    not nested in another def and that nothing inspects (see _SIGNATURE_READ). Only sqlglot/ source: a test
    file's defs are run by the suite itself."""
    if not path.startswith("sqlglot/") or path.startswith(_SIGNATURE_READ) or id(fn) in nested:
        return False
    m = ctx.m
    lm = m.lines.get(path)
    if not lm:
        return False
    lines = range(fn.body[0].lineno, fn.end_lineno + 1)
    il = m.importtime.get(path, ())
    un = m.unstable.get(path, ())
    if any(ln in lm or ln in il or ln in un for ln in lines):
        return False
    return any(not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)) for st in fn.body)


def _defaults_only(ctx: Ctx, path: str, diffs, old_tree, new_tree, ranges):
    """Tests that depend on an edit that only changes literal default values of defs, or None when the
    edit is anything else (or the evidence is thin). A literal default is read only when a call leaves
    the parameter out, and the call then runs the body: the tests are those that ran a line of a
    changed def's body. The header lines (decorators, signature) run at import for every importer and
    say nothing about who calls. Nothing in sqlglot or its tests reads defaults by introspection
    (inspect.getfullargspec(rule).args and inspect.signature(func).parameters read names only).
    A body line that ran at import widens to every importer, as for any def. A def whose body no test ran
    selects nothing when _never_ran proves that (no record of any kind for its body lines, a body coverage
    would have recorded, nothing that can read a default); any other def whose body no test ran keeps the
    older, wider rule (None)."""
    if not diffs or not all(d is None and isinstance(n, (ast.Constant, ast.UnaryOp, ast.arguments)) for n, d in diffs):
        return None
    # cheap screen: every difference must sit inside the argument list of a def the edit touched
    under = set()
    stack = [old_tree]
    while stack:
        node = stack.pop()
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lo = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                if any(a <= ch.end_lineno and lo <= b for a, b in ranges):
                    under.update(id(x) for x in ast.walk(ch.args))
                    stack.append(ch)
            elif isinstance(ch, (ast.ClassDef, ast.If, ast.Try, ast.With, ast.For, ast.While)):
                stack.append(ch)
    if not all(id(n) in under for n, _ in diffs):
        return None
    fns = pyscope.default_only(old_tree, new_tree)
    if not fns:
        return None
    il = ctx.m.importtime.get(path, ())
    out = 0
    nested = None
    for fn in fns:
        lo, hi = fn.body[0].lineno, fn.end_lineno
        u = ctx.udef(path, lo, hi)
        if not u:
            if nested is None:
                nested = _nested_defs(old_tree)
            if not _never_ran(ctx, path, fn, nested):
                return None
            ctx.note(f"P3 {path}:{lo}: no test ran the body of {fn.name}, its literal defaults are never read")
            continue
        out |= u
        if any(ln in il for ln in range(lo, hi + 1)):
            out |= ctx.wide(path)
    return out


def _one(ctx: Ctx, change) -> tuple[int, str]:
    m = ctx.m
    path = change.path_new
    old = change.path_old
    status = change.status
    is_py = path.endswith(".py") and old.endswith(".py")
    if status == "binary":
        return _data_file(ctx, path, None)
    if status == "added" and change.path_old != change.path_new:
        old = path
    if status == "added":
        if is_py and (path.startswith("sqlglot/") or os.path.basename(path) in ("conftest.py", "helpers.py")):
            return ctx.all, f"R9 {path}: new module"
        return _data_file(ctx, path, None)
    if status in ("deleted", "renamed") or old != path:
        if old.endswith(".py") or path.endswith(".py"):
            return ctx.wide(old) | ctx.wide(path), f"R9 {old}: {status}"
        return _reads(ctx, old) | _reads(ctx, path), f"R1 {old}: {status}"
    if not is_py:
        return _data_file(ctx, path, change)
    sr = m.srcreaders.get(path, 0)
    old_text = diffparse.read_text(ctx.src / path)
    new_text = diffparse.apply(old_text, change)  # PatchError means R9 in the caller
    if pyscope.cookie(old_text) != pyscope.cookie(new_text):
        return ctx.wide(path) | sr, f"R3 {path}: encoding declaration changed"
    try:
        new_tree = pyscope.parse(new_text, path)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ctx.wide(path) | sr, f"R3 {path}: does not parse"
    old_tree = pyscope.parse(old_text, path)
    ctx.old_tree = old_tree
    ranges = []
    for removed, added, p in _blocks(change):
        ranges += [(ln, ln) for ln, _ in removed]
        if not removed or len(added) > len(removed):
            ranges.append((p, p + 1))
    diffs = pyscope.ast_diff(old_tree, new_tree, ranges)
    if not diffs:
        if any("\r" in t for h in change.hunks for _, t in h.lines):
            return ctx.wide(path) | sr, f"R9 {path}: carriage return in a changed hunk"
        return sr, f"R4 {path}: ast unchanged" + (" (source readers)" if sr else "")
    if not pyscope.compiles(new_tree, path):
        return ctx.wide(path) | sr, f"R3 {path}: does not compile"
    got = _defaults_only(ctx, path, diffs, old_tree, new_tree, ranges)
    if got is not None:
        return got | sr, f"R6/R7 {path}: literal default values only"
    if path.startswith("sqlglot/"):
        try:
            ents = valrule.entries(old_tree, new_tree, diffs)
            if ents:
                masks = [ctx.val().readers(path, ln, key, tc, e.tokens, ctx.by_file.get('tests/test_time.py', 0)) for e in ents for ln, key, tc in [e]]
                if all(x is not None for x in masks):
                    got = 0
                    for x in masks:
                        got |= x
                    return got | sr, f"V {path}: constants in {len(ents)} dict entr{'y' if len(ents) == 1 else 'ies'} read by recorded tests"
            sents = valrule.set_entries(old_tree, new_tree, diffs)
            if sents:
                masks = [ctx.val().set_readers(path, ln, words) for ln, words in sents]
                if all(x is not None for x in masks):
                    got = 0
                    for x in masks:
                        got |= x
                    return got | sr, f"V {path}: string constants in {len(sents)} set(s) looked up by recorded tests"
        except Exception:
            pass
    ctx.narrow = None
    ctx.doc_only = _doc_only(old_tree, diffs)
    if ctx.single and path.startswith("sqlglot/"):
        try:
            f = attrrule.find(old_tree, new_tree, old_text, new_text)
            if f:
                got = ctx.attr().readers(path, f[1], f[2])
                if got is not None:
                    ctx.narrow = (f[0], got[0], got[1])
        except Exception:
            ctx.narrow = None
    hard, soft_r = [], []
    for node, deferred in diffs:
        lo = getattr(node, "lineno", None)
        rng = (lo, getattr(node, "end_lineno", lo) or lo) if lo is not None else (0, 1 << 30)
        if deferred is None:
            hard.append(rng)
        else:
            soft_r.append((rng, (deferred.lineno, deferred.end_lineno or deferred.lineno)))

    def soft(ln):
        """The line range of the lambda the edit sits in, when every edit on this line does."""
        if any(a <= ln <= b for a, b in hard):
            return None
        spans = [lam for (a, b), lam in soft_r if a <= ln <= b]
        if not spans:
            return None
        return (min(a for a, _ in spans), max(b for _, b in spans))

    is_test = path.startswith("tests/") and os.path.basename(path).startswith("test_")
    out = _py_lines(ctx, path, change, old_text.split("\n"), is_test, soft) | sr
    if path.startswith("sqlglot/") and _own_import_risk(old_tree, new_tree, old_text, diffs, ranges):
        pm = ctx.probe_mask()
        out |= ctx.wide(path) if pm is None else pm  # the edit can make this very module raise while it imports: every test that imports it, lazily or not
    # A change of what a function binds or resolves (a name made local, a yield, a global, a closure) acts on every
    # line of that function, not only on the line edited: every test that ran any of its lines.
    try:
        spans = pyscope.function_hazards(old_text, new_text, old_tree, new_tree, path, str(ctx.src / path))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return out | ctx.wide(path), f"R3 {path}: scope table"
    for lo, hi in spans:
        out |= ctx.udef(path, lo, hi)
    return out, f"R6/R7 {path}" + (f"; R8 scope change in {len(spans)} function(s)" if spans else "")


def _weak(ctx: Ctx, changes) -> int:
    """R10: bits of tests a single operator mutation in a function body provably cannot affect."""
    summaries = ctx.m.weakmut
    if not summaries or len(changes) != 1 or getattr(ctx.m, "weakmut_rec", None) != weakmut.RECORDER_VERSION:
        return 0  # no profile, or one written by another recorder version
    ch = changes[0]
    path = ch.path_new
    if ch.status != "modified" or ch.path_old != path or not path.startswith("sqlglot/") or not path.endswith(".py"):
        return 0
    try:
        old_text = diffparse.read_text(ctx.src / path)
        new_text = diffparse.apply(old_text, ch)
        if pyscope.cookie(old_text) != pyscope.cookie(new_text):
            return 0
        got, note = weakmut.drops(path, ch, old_text, new_text, summaries, ctx.m.srcreaders.get(path, 0))
    except Exception:  # any doubt keeps the tests
        return 0
    if note:
        ctx.note(note)
    bits = 0
    for t in got:
        bits |= 1 << t
    return bits


def select(changes, maps, tests, src_root=None):
    ctx = Ctx(maps, tests, src_root or DEFAULT_SRC)
    ctx.single = len(changes) == 1
    try:
        pt = {}
        for ch in changes:
            if ch.status == "modified" and ch.path_new == ch.path_old and ch.path_new.startswith("sqlglot/") and ch.path_new.endswith(".py"):
                pt[ch.path_new] = diffparse.apply((ctx.src / ch.path_new).read_bytes().decode("utf-8"), ch)
        ctx.patched = pt or None
    except Exception:
        ctx.patched = None
    mask = 0
    for ch in changes:
        try:
            got, why = _one(ctx, ch)
        except Exception as exc:  # any doubt widens
            path = ch.path_new or ch.path_old
            got, why = ctx.wide(path), f"R9 {path}: {type(exc).__name__}: {exc}"
        ctx.note(why)
        mask |= got
    mask &= ~_weak(ctx, changes)
    return _bits(mask & ctx.all), "; ".join(ctx.notes)
