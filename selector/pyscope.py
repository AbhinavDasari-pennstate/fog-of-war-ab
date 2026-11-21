"""Python-source helpers for the selector: AST equality, encoding cookies, changed names.

Stdlib only. Nothing here imports sqlglot.
"""

import ast
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


def parse(text: str, path: str = "<src>"):
    return ast.parse(text, filename=path)


def dump(tree) -> str:
    return ast.dump(tree)


def compiles(tree, path: str = "<src>") -> bool:
    """True when the parsed tree also survives the compiler (catches 'return' outside a function)."""
    try:
        compile(tree, path, "exec", dont_inherit=True)
        return True
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False


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


def rebound_spans(old_tree, new_tree) -> list:
    """Old-file line spans (lo, hi) of functions whose binding set changed."""
    new = {k: bind_sig(n) for k, n in units(new_tree)}
    out = []
    for k, n in units(old_tree):
        if k in new and new[k] != bind_sig(n):
            out.append((min([n.lineno] + [d.lineno for d in n.decorator_list]), n.end_lineno))
    return out


# ---- scope hazards: an edit that changes how a whole function resolves names ----------------------

_GEN_FLAGS = 0x20 | 0x80 | 0x200 | 0x100  # generator, coroutine, async generator, iterable coroutine


def _code_scopes(text: str, path: str):
    """key -> (locals, names, frees, flags, first line, last line) for every code object of the module."""
    import types

    out = {}

    def walk(co, prefix):
        seen: dict = {}
        for c in co.co_consts:
            if isinstance(c, types.CodeType):
                n = seen.get(c.co_name, 0)
                seen[c.co_name] = n + 1
                key = f"{prefix}/{c.co_name}#{n}"
                lines = [ln for _, _, ln in c.co_lines() if ln is not None]
                out[key] = (
                    frozenset(c.co_varnames) | frozenset(c.co_cellvars), frozenset(c.co_names), frozenset(c.co_freevars),
                    c.co_flags & _GEN_FLAGS, min(lines + [c.co_firstlineno]), max(lines + [c.co_firstlineno]),
                )
                walk(c, key)

    walk(compile(text, path, "exec", dont_inherit=True), "")
    return out


def scope_hazards(old_text: str, new_text: str, path: str = "<src>") -> list:
    """Old-file line ranges of every function scope that the edit changes as a whole.

    An edit to one statement can break every call of its function without that statement running: it can make the
    function a generator or coroutine, change which of its names are local (a name that was read from the module now
    binds in the function, or the reverse), or change what it takes from an enclosing scope. Returns [] when the
    scope tables match; raises when either text does not compile (the caller already widens then)."""
    a = _code_scopes(old_text, path)
    b = _code_scopes(new_text, path)
    out = []
    for key, (loc_a, nam_a, fre_a, fl_a, s, e) in a.items():
        rec = b.get(key)
        if rec is None:
            continue  # the scope itself was added or removed: the statement rules see that
        loc_b, nam_b, fre_b, fl_b, _, _ = rec
        added, removed = loc_b - loc_a, loc_a - loc_b
        bad = fl_a != fl_b or fre_a != fre_b or bool(added & (nam_a | fre_a)) or bool(removed & (nam_b | fre_b))
        if bad:
            out.append((s, e))
    return out


def function_hazards(old_text: str, new_text: str, old_tree, new_tree, path: str = "<src>") -> list:
    """One answer for "an edit that acts on a whole function": old-file line spans of every function whose binding set
    changed (rebound_spans: a made-local name, global, yield, await) or whose compiled scope table changed
    (scope_hazards: locals read as globals, closure, generator and coroutine flags). The union of both, so it is
    at least as strict as either."""
    spans = set(rebound_spans(old_tree, new_tree))
    spans.update(scope_hazards(old_text, new_text, path))
    return sorted(spans)
