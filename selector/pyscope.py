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
