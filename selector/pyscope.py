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


def bind_sig(node) -> frozenset:
    """Everything a function (and what it contains) can bind, plus yield and await. Making a name
    local anywhere in a function changes every read of it there (UnboundLocalError); a yield turns
    the function into a generator. Neither is local to the line that holds it."""
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(type(n).__name__ + ":" + x for x in n.names)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await)):
            names.add("<" + type(n).__name__ + ">")
        else:
            for attr in ("name", "rest"):
                v = getattr(n, attr, None)
                if isinstance(v, str) and type(n).__name__.startswith("Match"):
                    names.add(v)
    return frozenset(names)


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


def rebound_spans(old_tree, new_tree) -> list:
    """Old-file line spans (lo, hi) of functions whose binding set changed."""
    new = {k: bind_sig(n) for k, n in units(new_tree)}
    out = []
    for k, n in units(old_tree):
        if k in new and new[k] != bind_sig(n):
            out.append((min([n.lineno] + [d.lineno for d in n.decorator_list]), n.end_lineno))
    return out
