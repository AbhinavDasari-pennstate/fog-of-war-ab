"""Python-source helpers for the selector: AST equality, encoding cookies, changed names.

Stdlib only. Nothing here imports sqlglot.
"""

import ast
import marshal
import os
import re

CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_STR = re.compile(r"\"([^\"\\\n]*)\"|'([^'\\\n]*)'")
_DEF = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z_0-9]*)")
_CLASS = re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z_0-9]*)")


def cookie(text: str) -> list:
    """PEP 263 encoding declaration on each of the first two lines (None when absent)."""
    out = []
    for line in text.split("\n")[:2]:
        m = CODING.match(line)
        out.append(m.group(1) if m else None)
    return out


# ---- caches: the same file text is parsed and compiled by several rules of one run -------------------
#
# No selector code changes a tree it gets from parse(): default_only puts its stand-ins back before it
# returns, and tpcds_scope, which blanks function bodies, parses on its own. A few entries only: a tree of a
# big sqlglot file is tens of megabytes.

_CACHE_MAX = 6
_PARSED: dict = {}  # (text, path) -> tree
_TREE_CODE: dict = {}  # id(tree) -> (tree, code object compile() made of it)
_SCOPE_TABLES: dict = {}  # (text, path) -> scope table


def _put(cache: dict, key, value):
    if len(cache) >= _CACHE_MAX:
        cache.pop(next(iter(cache)))
    cache[key] = value
    return value


def clear_caches():
    _PARSED.clear()
    _TREE_CODE.clear()
    _SCOPE_TABLES.clear()


def parse(text: str, path: str = "<src>"):
    key = (text, path)
    tree = _PARSED.get(key)
    if tree is None:
        tree = _put(_PARSED, key, ast.parse(text, filename=path))
    return tree


def dump(tree) -> str:
    return ast.dump(tree)


def compiles(tree, path: str = "<src>") -> bool:
    """True when the parsed tree also survives the compiler (catches 'return' outside a function)."""
    try:
        code = compile(tree, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    _put(_TREE_CODE, id(tree), (tree, code))
    return True


def stmt_same(old_text: str, new_text: str):
    """same(a, b) -> True only when statement a of the old file and statement b of the new file are written exactly
    alike (the same lines, from its first decorator to its last line, at the same columns). Such statements parse to
    equal trees, so a walk that compares two trees can skip them. None when a carriage return makes line numbers
    ambiguous: the caller then compares everything."""
    if "\r" in old_text or "\r" in new_text:
        return None
    ol = old_text.split("\n")
    nl = new_text.split("\n")

    def first(n):
        lo = n.lineno
        for d in getattr(n, "decorator_list", ()):
            if d.lineno < lo:
                lo = d.lineno
        return lo

    def same(a, b):
        if type(a) is not type(b) or a.col_offset != b.col_offset or a.end_col_offset != b.end_col_offset:
            return False
        la, lb = first(a), first(b)
        return a.end_lineno - la == b.end_lineno - lb and ol[la - 1:a.end_lineno] == nl[lb - 1:b.end_lineno]

    return same


def names_in(lines) -> set:
    """Identifiers and string-literal contents (whole and per word) found in source lines."""
    out: set = set()
    for line in lines:
        out.update(_IDENT.findall(line))
        for m in _STR.finditer(line):
            s = m.group(1) if m.group(1) is not None else m.group(2)
            if s:
                out.add(s)
                out.update(_IDENT.findall(s))
    return out


def def_name(line: str) -> str:
    m = _DEF.match(line)
    return m.group(1) if m else ""


def class_name(line: str) -> str:
    m = _CLASS.match(line)
    return m.group(1) if m else ""


def is_code_line(text: str) -> bool:
    s = text.strip()
    return bool(s) and not s.startswith("#")


_LINENO = ("lineno", "end_lineno")


def ast_diff(old, new, ranges):
    """Differences between two parsed files, looking only where the edit touched.

    ranges: (lo, hi) old line ranges the patch changed. A subtree of the old tree that does not
    overlap any range has identical text in both files, so it is skipped. Returns a list of
    (old_node, lambda_node_or_None) where old_node is the old node at which the trees stop agreeing
    and the second item is the enclosing Lambda node when the difference lies inside a lambda body
    (it runs only when the lambda is called). An empty list means the ASTs agree (comment, blank-line and layout edits).
    """
    out = []

    def touched(node):
        lo = getattr(node, "lineno", None)
        if lo is None:
            return True
        hi = getattr(node, "end_lineno", lo) or lo
        for d in getattr(node, "decorator_list", ()):  # decorators sit above the def line
            lo = min(lo, d.lineno)
        for a, b in ranges:
            if a <= hi and lo <= b:
                return True
        return False

    def walk(a, b, deferred, parent):
        if isinstance(a, ast.AST):
            if type(a) is not type(b):
                out.append((parent, deferred))
                return
            if not touched(a):
                return
            for field in a._fields:
                va = getattr(a, field, None)
                vb = getattr(b, field, None)
                inner = deferred or (a if isinstance(a, ast.Lambda) and field == "body" else None)
                if isinstance(va, list):
                    if not isinstance(vb, list) or len(va) != len(vb):
                        out.append((a, deferred))
                        continue
                    for x, y in zip(va, vb):
                        walk(x, y, inner, a)
                else:
                    walk(va, vb, inner, a)
        elif isinstance(a, list):
            if not isinstance(b, list) or len(a) != len(b):
                out.append((parent, deferred))
                return
            for x, y in zip(a, b):
                walk(x, y, deferred, parent)
        else:
            if type(a) is not type(b) or a != b:
                out.append((parent, deferred))

    walk(old, new, None, old)
    return out


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp,
           ast.DictComp, ast.GeneratorExp)


def bind_sig(node):
    """What each scope of a function binds: (names, (the same for every nested scope in order)).
    Names are stores and deletes, imports, def and class names, handler and match names, parameters,
    global and nonlocal declarations, and yield or await. Making a name local anywhere in a scope
    changes every read of it there (UnboundLocalError); a yield turns the function into a generator.
    Neither is local to the line that holds it. Scopes are kept apart: a name that moves from the
    outer function into a nested one changes both signatures."""
    names = set()
    kids = []

    def record(n):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(type(n).__name__ + ":" + x for x in n.names)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await)):
            names.add("<" + type(n).__name__ + ">")
        elif type(n).__name__.startswith("Match"):
            for attr in ("name", "rest"):
                v = getattr(n, attr, None)
                if isinstance(v, str):
                    names.add(v)

    def visit(n):
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, _SCOPES):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(ch.name)
                kids.append(bind_sig(ch))
            else:
                record(ch)
                visit(ch)

    visit(node)
    return (frozenset(names), tuple(kids))


def units(tree):
    """Outermost function defs in source order, keyed (qualified name, ordinal)."""
    out, counts = [], {}

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, _FUNCS):
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


# ---- default-value-only edits ------------------------------------------------------------------

def _literal(n) -> bool:
    """A constant default: 1, "x", None, -1 (a literal evaluates at def time with no side effect)."""
    if isinstance(n, ast.Constant):
        return True
    return (isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd))
            and isinstance(n.operand, ast.Constant) and isinstance(n.operand.value, (int, float)))


def _slots(fn):
    """(list, index) of every literal default of a def (positional and keyword-only)."""
    out = []
    for lst in (fn.args.defaults, fn.args.kw_defaults):
        for i, d in enumerate(lst):
            if d is not None and _literal(d):
                out.append((lst, i))
    return out


def _dumps(nodes):
    return [None if n is None else ast.dump(n) for n in nodes]


def default_only(old_tree, new_tree):
    """The old def nodes whose literal default values changed, when that is the ONLY difference between
    the two files (every other node, in order, is identical, including the number and place of defaults).
    None when the trees differ in anything else, or no default value changed. The trees are left as found.

    Why it matters: a literal default is evaluated when the def runs and read only when a call leaves the
    parameter out, so a test depends on the edit only if it calls the function (runs a line of its body)."""
    fo = [n for n in ast.walk(old_tree) if isinstance(n, _FUNCS)]
    fn = [n for n in ast.walk(new_tree) if isinstance(n, _FUNCS)]
    if len(fo) != len(fn):
        return None
    changed = []
    for a, b in zip(fo, fn):
        if _dumps(a.args.defaults + a.args.kw_defaults) != _dumps(b.args.defaults + b.args.kw_defaults):
            changed.append(a)
    if not changed:
        return None
    saved = []
    marker = ast.Constant(value=0)
    try:
        for tree_fns in (fo, fn):
            for f in tree_fns:
                for lst, i in _slots(f):
                    saved.append((lst, i, lst[i]))
                    lst[i] = marker
        same = ast.dump(old_tree) == ast.dump(new_tree)
    finally:
        for lst, i, d in reversed(saved):
            lst[i] = d
    return changed if same else None


def rebound_spans(old_tree, new_tree, same=None) -> list:
    """Old-file line spans (lo, hi) of functions whose binding set changed. `same` (stmt_same) lets two functions that
    are written exactly alike skip the comparison: equal trees bind equal names."""
    new = dict(units(new_tree))
    out = []
    for k, n in units(old_tree):
        m = new.get(k)
        if m is None or (same is not None and same(n, m)):
            continue
        if bind_sig(m) != bind_sig(n):
            out.append((min([n.lineno] + [d.lineno for d in n.decorator_list]), n.end_lineno))
    return out


# ---- scope hazards: an edit that changes how a whole function resolves names ----------------------

_GEN_FLAGS = 0x20 | 0x80 | 0x200 | 0x100  # generator, coroutine, async generator, iterable coroutine


def _scopes_of(code) -> dict:
    """key -> (locals, names, frees, flags, code object) for every code object nested in the module code `code`."""
    import types

    out = {}

    def walk(co, prefix):
        seen: dict = {}
        for c in co.co_consts:
            if isinstance(c, types.CodeType):
                n = seen.get(c.co_name, 0)
                seen[c.co_name] = n + 1
                key = f"{prefix}/{c.co_name}#{n}"
                out[key] = (
                    frozenset(c.co_varnames) | frozenset(c.co_cellvars), frozenset(c.co_names), frozenset(c.co_freevars),
                    c.co_flags & _GEN_FLAGS, c,
                )
                walk(c, key)

    walk(code, "")
    return out


def _span(c) -> tuple:
    """(first line, last line) of a code object: only the functions the edit breaks need it."""
    lines = [ln for _, _, ln in c.co_lines() if ln is not None]
    return min(lines + [c.co_firstlineno]), max(lines + [c.co_firstlineno])


def _pyc_code(file: str, text: str):
    """The module code object from the file's .pyc, when that .pyc is the compiled form of exactly this text (header
    flags, source mtime and size agree with the file, and the text is as long as the file); else None."""
    try:
        import _frozen_importlib_external as ext

        with open(ext.cache_from_source(file), "rb") as fh:
            data = fh.read()
        st = os.stat(file)
        if (len(data) < 16 or data[:4] != ext.MAGIC_NUMBER or data[4:8] != b"\0\0\0\0"
                or int.from_bytes(data[8:12], "little") != int(st.st_mtime) & 0xFFFFFFFF
                or int.from_bytes(data[12:16], "little") != st.st_size & 0xFFFFFFFF
                or st.st_size != len(text.encode("utf-8"))):
            return None
        return marshal.loads(memoryview(data)[16:])
    except Exception:
        return None


def _code_scopes(text: str, path: str, tree=None, file=None) -> dict:
    """The scope table of the module `text` (cached). Its code object comes, cheapest first, from the compile that
    compiles() already did for `tree`, from the file's .pyc, or from compiling the text."""
    key = (text, path)
    got = _SCOPE_TABLES.get(key)
    if got is None:
        code = None
        if tree is not None:
            hit = _TREE_CODE.get(id(tree))
            if hit is not None and hit[0] is tree:
                code = hit[1]
        if code is None and file is not None:
            code = _pyc_code(file, text)
        if code is None:
            code = compile(text, path, "exec", dont_inherit=True)
        got = _put(_SCOPE_TABLES, key, _scopes_of(code))
    return got


def scope_hazards(old_text: str, new_text: str, path: str = "<src>", old_file=None, new_tree=None) -> list:
    """Old-file line ranges of every function scope that the edit changes as a whole.

    An edit to one statement can break every call of its function without that statement running: it can make the
    function a generator or coroutine, change which of its names are local (a name that was read from the module now
    binds in the function, or the reverse), or change what it takes from an enclosing scope. Returns [] when the
    scope tables match; raises when either text does not compile (the caller already widens then).
    old_file: path of the unmodified file (its .pyc spares compiling it); new_tree: the parsed new text (its compile is reused)."""
    a = _code_scopes(old_text, path, file=old_file)
    b = _code_scopes(new_text, path, tree=new_tree)
    out = []
    for key, (loc_a, nam_a, fre_a, fl_a, code_a) in a.items():
        rec = b.get(key)
        if rec is None:
            continue  # the scope itself was added or removed: the statement rules see that
        loc_b, nam_b, fre_b, fl_b, _ = rec
        added, removed = loc_b - loc_a, loc_a - loc_b
        bad = fl_a != fl_b or fre_a != fre_b or bool(added & (nam_a | fre_a)) or bool(removed & (nam_b | fre_b))
        if bad:
            out.append(_span(code_a))
    return out


def function_hazards(old_text: str, new_text: str, old_tree, new_tree, path: str = "<src>", old_file=None) -> list:
    """One answer for "an edit that acts on a whole function": old-file line spans of every function whose binding set
    changed (rebound_spans: a made-local name, global, yield, await) or whose compiled scope table changed
    (scope_hazards: locals read as globals, closure, generator and coroutine flags). The union of both, so it is
    at least as strict as either."""
    spans = set(rebound_spans(old_tree, new_tree, stmt_same(old_text, new_text)))
    spans.update(scope_hazards(old_text, new_text, path, old_file=old_file, new_tree=new_tree))
    return sorted(spans)
