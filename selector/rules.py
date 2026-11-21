"""Static test selection: rules R0 to R9 from plan/plan.md ("Selector rules").

    select(changes, maps, tests, src_root=None) -> (set of test numbers, reason)

changes: diffparse.parse() output. maps: maps.load.Maps. tests: the "tests" list of
tests.json. src_root: the pinned sqlglot checkout (old file text). Any doubt widens (R9).
Stdlib only, never imports sqlglot.
"""

import ast
import bisect
import os
import re
from pathlib import Path

import diffparse
import attrrule
import pyscope
import weakmut

DEFAULT_SRC = os.environ.get("BR_SRC", "/app/sqlglot-src")
FIXTURE_ROOT = "tests/fixtures"
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


class Ctx:
    def __init__(self, maps, tests, src_root):
        self.m = maps
        self.src = Path(src_root)
        self.tests = tests
        self.all = 0
        self.by_file: dict[str, int] = {}
        for t in tests:
            self.all |= 1 << t["number"]
            self.by_file[t["file"]] = self.by_file.get(t["file"], 0) | (1 << t["number"])
        self.notes: list[str] = []
        self.single = False  # the patch changes one file
        self.narrow = None  # (start line, mask) of a class-level assignment narrowed by maps/attrreads.bin
        self._attr = None

    def attr(self):
        if self._attr is None:
            self._attr = attrrule.Attr(self.m.dir)
        return self._attr

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


def _class_level(ctx: Ctx, path: str, s, ln: int, texts, old_lines, removed_texts=(), added_texts=(), soft=False) -> int:
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
    if nr is not None and nr[0] == start and all(l in il for l in range(start, end + 1)):
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
    if path.startswith(FIXTURE_ROOT + "/") and path not in m.reads:
        for k, v in m.reads.items():  # unknown fixture: everything that reads fixtures
            if k.startswith(FIXTURE_ROOT) and not k.endswith(".gz"):
                out |= v
        return out, f"R2 {path}: not in reads, all fixture readers"
    return out, f"{'R2' if path.startswith('tests/') else 'R1'} {path}"


def _defaults_only(ctx: Ctx, path: str, diffs, old_tree, new_tree, ranges):
    """Tests that depend on an edit that only changes literal default values of defs, or None when the
    edit is anything else (or the evidence is thin). A literal default is read only when a call leaves
    the parameter out, and the call then runs the body: the tests are those that ran a line of a
    changed def's body. The header lines (decorators, signature) run at import for every importer and
    say nothing about who calls. Nothing in sqlglot or its tests reads defaults by introspection
    (inspect.getfullargspec(rule).args and inspect.signature(func).parameters read names only).
    A body line that ran at import widens to every importer, as for any def; a def whose body no test ran
    keeps the older, wider rule."""
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
    for fn in fns:
        lo, hi = fn.body[0].lineno, fn.end_lineno
        u = ctx.udef(path, lo, hi)
        if not u:
            return None
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
    old_text = (ctx.src / path).read_bytes().decode("utf-8")
    new_text = diffparse.apply(old_text, change)  # PatchError means R9 in the caller
    if pyscope.cookie(old_text) != pyscope.cookie(new_text):
        return ctx.wide(path) | sr, f"R3 {path}: encoding declaration changed"
    try:
        new_tree = pyscope.parse(new_text, path)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ctx.wide(path) | sr, f"R3 {path}: does not parse"
    old_tree = pyscope.parse(old_text, path)
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
    ctx.narrow = None
    if ctx.single and path.startswith("sqlglot/"):
        try:
            f = attrrule.find(old_tree, new_tree, old_text)
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
    # A change of what a function binds or resolves (a name made local, a yield, a global, a closure) acts on every
    # line of that function, not only on the line edited: every test that ran any of its lines.
    try:
        spans = pyscope.function_hazards(old_text, new_text, old_tree, new_tree, path)
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
        old_text = (ctx.src / path).read_bytes().decode("utf-8")
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
