"""G: an edit of constants inside a module-level literal that only function bodies of its own module read.

    DIFF_MONTH_SWITCH = ("YEAR", "QUARTER", "MONTH")        # sqlglot/generators/hive.py
    def _date_diff_sql(self, expression):
        months_between = unit in DIFF_MONTH_SWITCH           # the only read

Binding a pure literal (constants, tuples, lists, sets and dicts of them, a sign on a number) cannot run code or raise,
so importing the module does the same thing before and after; the only thing that changes is the value the name is
bound to. A test can see that value only by running code that reads the name. `find` proves, from the old and new
trees of the edited file, that the edit is nothing but such constants in one statement `NAME = <literal>` of the module
body, that NAME has no other binding in the file, and that every read of it is a plain `NAME` inside a def body (a read
at module or class level, in a decorator or in a default value runs at import and keeps the wider rule).
`reached_elsewhere` proves that no other file of the checkout can reach the name: a file that spells NAME must bind
its own module-level NAME and neither import that name, nor read an attribute called NAME, nor hold it in a string, nor
star-import anything. The caller then treats the edit as an edit of every statement that reads NAME (rules._func_level:
the tests that ran the statement, the whole def when a line is unstable, every importer when a line ran at import).

Not seen: a reader that walks a module's namespace without spelling the name (vars(module), module.__dict__,
inspect.getmembers). In the pinned checkout nothing does that to a sqlglot module's data: see tests_sel/test_modglobal.py
(test_pin_has_no_namespace_walkers). Stdlib only, never imports sqlglot.
"""

import ast
import os
import re

_SIGN = (ast.USub, ast.UAdd)
# the module's own namespace reached without spelling a global's name
_DYNAMIC = re.compile(r"(?<![A-Za-z0-9_])(globals|locals|vars|eval|exec|__dict__|__import__|import_module|modules|getmembers)(?![A-Za-z0-9_])")


def _number(n) -> bool:
    return isinstance(n, ast.Constant) and isinstance(n.value, (int, float, complex)) and not isinstance(n.value, bool)


def _hashable(n) -> bool:
    """A constant, a signed number, or a tuple of those: a dict key or set element that cannot raise."""
    if isinstance(n, ast.Constant):
        return True
    if isinstance(n, ast.UnaryOp):
        return isinstance(n.op, _SIGN) and _number(n.operand)
    if isinstance(n, ast.Tuple):
        return all(_hashable(e) for e in n.elts)
    return False


def pure(n) -> bool:
    """A literal whose evaluation runs no code and cannot raise: constants, signed numbers, and tuple, list, set and
    dict displays of them (no names, calls, spreads, comprehensions or f-strings; hashable keys and set elements)."""
    if isinstance(n, ast.Constant):
        return True
    if isinstance(n, ast.UnaryOp):
        return isinstance(n.op, _SIGN) and _number(n.operand)
    if isinstance(n, (ast.Tuple, ast.List)):
        return all(pure(e) for e in n.elts)
    if isinstance(n, ast.Set):
        return all(_hashable(e) for e in n.elts)
    if isinstance(n, ast.Dict):
        return all(k is not None and _hashable(k) for k in n.keys) and all(pure(v) for v in n.values)
    return False


def _shape(n) -> str:
    """The literal with every constant blanked: two literals with the same shape differ only in constant values."""
    if isinstance(n, ast.Constant):
        return "C"
    if isinstance(n, ast.UnaryOp):
        return type(n.op).__name__ + "(C)"
    if isinstance(n, ast.Dict):
        return "D{" + ",".join(_shape(k) + ":" + _shape(v) for k, v in zip(n.keys, n.values)) + "}"
    return type(n).__name__ + "[" + ",".join(_shape(e) for e in n.elts) + "]"


def _target(st):
    """(name node, value node) of `NAME = value` / `NAME: T = value`, else None."""
    if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
        return st.targets[0], st.value
    if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None and st.simple:
        return st.target, st.value
    return None


def _word(name: str):
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")


def find(old_tree, new_tree, diffs, old_text: str):
    """(NAME, sorted lines of every read of NAME) when the patch only changes constants inside the value of one
    module-body statement `NAME = <pure literal>` whose name is bound nowhere else in the file and read only by plain
    `NAME` loads inside def bodies; else None.

    diffs: pyscope.ast_diff(old, new, ranges), the old nodes at which the trees stop agreeing (everything outside them
    has the same text in both files)."""
    if not diffs or not all(d is None and isinstance(n, ast.Constant) for n, d in diffs):
        return None
    if len(old_tree.body) != len(new_tree.body):
        return None
    want = {id(n) for n, _ in diffs}
    hit = None
    for i, st in enumerate(old_tree.body):
        t = _target(st)
        if t is None:
            continue
        if want <= {id(n) for n in ast.walk(t[1])}:
            hit = (i, st, t)
            break
    if hit is None:
        return None
    i, st, (tnode, value) = hit
    nst = new_tree.body[i]
    nt = _target(nst)
    if type(nst) is not type(st) or nt is None or nt[0].id != tnode.id:
        return None
    if not pure(value) or not pure(nt[1]) or _shape(value) != _shape(nt[1]):
        return None
    try:
        ast.literal_eval(value)
        ast.literal_eval(nt[1])
    except Exception:
        return None
    name = tnode.id
    if _DYNAMIC.search(old_text):
        return None
    word = _word(name)
    inbody = set()
    for node in ast.walk(old_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for s in node.body:
                for n in ast.walk(s):
                    inbody.add(id(n))
    reads = set()
    for node in ast.walk(old_tree):
        if isinstance(node, ast.Name):
            if node.id != name:
                continue
            if isinstance(node.ctx, ast.Load):
                if id(node) not in inbody:
                    return None  # read while the module or a class body runs, or in a decorator, default or annotation
                reads.add(node.lineno)
            elif node is not tnode:
                return None  # bound or deleted a second time (any scope)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            if name in node.names:
                return None
        elif isinstance(node, ast.arg):
            if node.arg == name:
                return None
        elif isinstance(node, ast.alias):
            if name in (node.name, node.asname, node.name.split(".")[0]):
                return None
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return None
        elif isinstance(node, ast.Attribute):
            if node.attr == name:
                return None
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, (str, bytes)) and word.search(node.value if isinstance(node.value, str) else node.value.decode("latin-1")):
                return None  # the name inside a string (getattr by name, __all__)
        elif getattr(node, "name", None) == name or getattr(node, "rest", None) == name:
            return None  # except ... as NAME, a match capture
    if not reads:
        return None  # nothing in the file reads it: whoever does is out of sight
    return name, sorted(reads)


def reached_elsewhere(src_root, path: str, name: str) -> bool:
    """True unless it is proven that no other .py file of the checkout can reach the module-level `name` of `path`.

    A file that never spells the name cannot read it by name. A file that spells it is harmless only when the name is
    its own: it binds NAME in its module body, does not import a NAME, reads no attribute called NAME, has no string
    holding the word and star-imports nothing (so no chain of star imports can hand it ours). Any doubt is True."""
    word = _word(name)
    needle = name.encode()
    root = str(src_root)
    own = os.path.normpath(os.path.join(root, path))
    seen_own = False
    try:
        for dp, dns, fns in os.walk(root):
            for fn in fns:
                if not fn.endswith((".py", ".pyi", ".pyx", ".pxd")):
                    continue
                full = os.path.join(dp, fn)
                if os.path.normpath(full) == own:
                    seen_own = True
                    continue
                with open(full, "rb") as f:
                    data = f.read()
                if needle not in data:
                    continue
                text = data.decode("utf-8")
                if not word.search(text):
                    continue
                if not fn.endswith(".py"):
                    return True
                tree = ast.parse(text)
                bound = False
                for st in tree.body:
                    t = _target(st)
                    if t is not None and t[0].id == name:
                        bound = True
                if not bound:
                    return True
                for node in ast.walk(tree):
                    if isinstance(node, ast.alias):
                        if node.name == "*" or name in (node.name, node.asname, node.name.split(".")[0]):
                            return True
                    elif isinstance(node, ast.Attribute):
                        if node.attr == name:
                            return True
                    elif isinstance(node, ast.Constant):
                        v = node.value
                        if isinstance(v, bytes):
                            v = v.decode("latin-1")
                        if isinstance(v, str) and word.search(v):
                            return True
                    elif isinstance(node, (ast.Global, ast.Nonlocal)):
                        if name in node.names:
                            return True
    except Exception:
        return True
    return not seen_own
