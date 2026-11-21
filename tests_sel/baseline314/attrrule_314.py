# Frozen copy of selector/attrrule.py at 314df58, the differential oracle for tests_sel/test_cpu_diet.py. Do not edit.
"""L2: narrow a class-level attribute edit to the tests that read that attribute (maps/attrreads.bin).

Stdlib only. `find(old_tree, new_tree, old_text)` says whether a patch changes exactly one class-level
assignment with a literal or simple value; `Attr.readers(...)` then gives the tests that read it through the
class or a subclass, or None when today's rule must apply (derived at class creation, an enum, an unknown or
never read name, a value that could fail to resolve).
"""

import ast
import marshal
import os
import re
import zlib

_SIMPLE_ATOMS = (ast.Constant,)
_UNARY = (ast.USub, ast.UAdd, ast.Not)


def module_of(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def _chain(node):
    """Dotted name for Name/Attribute chains, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def simple_value(node, old_text: str) -> bool:
    """A literal, a container of literals, or names that the old file text already uses."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, _UNARY) and simple_value(node.operand, old_text)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(simple_value(e, old_text) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all((k is None or simple_value(k, old_text)) and simple_value(v, old_text)
                   for k, v in zip(node.keys, node.values))
    if isinstance(node, ast.Starred):
        return simple_value(node.value, old_text)
    if isinstance(node, (ast.Name, ast.Attribute)):
        c = _chain(node)
        if c is None:
            return False
        return re.search(r"(?<![\w.])" + re.escape(c) + r"(?![\w])", old_text) is not None
    return False


class _Opaque(ast.NodeTransformer):
    """Names, attribute chains and `**x` become hashable stand-ins so that literal_eval can judge the rest."""

    def visit_Name(self, node):
        return ast.copy_location(ast.Constant(value="n"), node)

    visit_Attribute = visit_Name

    def visit_Starred(self, node):
        return ast.copy_location(ast.Constant(value="n"), node)

    def visit_Dict(self, node):
        self.generic_visit(node)
        pairs = [(k, v) for k, v in zip(node.keys, node.values) if k is not None]
        node.keys = [k for k, _ in pairs]
        node.values = [v for _, v in pairs]
        return node


def literal_ok(node) -> bool:
    """The value builds without raising when its names resolve: a literal of hashable keys and set members."""
    import copy

    try:
        ast.literal_eval(ast.fix_missing_locations(_Opaque().visit(copy.deepcopy(ast.Expression(body=node)))))
        return True
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False


def bare_use(cls_node, own, name: str) -> bool:
    """True when the class body loads `name` as a bare name anywhere but inside its own assignment: the
    value flows into another statement (a derived table, a comprehension, a default, a decorator) at class creation."""
    skip = {id(n) for n in ast.walk(own.value)} if getattr(own, "value", None) is not None else set()
    for n in ast.walk(cls_node):
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load) and id(n) not in skip:
            return True
    return False


def find(old_tree, new_tree, old_text: str):
    """(start_line, class qualname, name) when the trees differ in exactly one class-level
    assignment whose old and new values are simple; else None."""
    found = []

    def dump(x):
        return ast.dump(x)

    def walk(ob, nb, stack, cnode):
        if len(ob) != len(nb):
            return False
        for a, b in zip(ob, nb):
            if dump(a) == dump(b):
                continue
            if isinstance(a, ast.ClassDef) and isinstance(b, ast.ClassDef) and a.name == b.name and \
                    dump(ast.Tuple(elts=a.bases + [k.value for k in a.keywords] + a.decorator_list)) == \
                    dump(ast.Tuple(elts=b.bases + [k.value for k in b.keywords] + b.decorator_list)) and \
                    [k.arg for k in a.keywords] == [k.arg for k in b.keywords]:
                if not walk(a.body, b.body, stack + [a.name], a):
                    return False
                continue
            if stack and type(a) is type(b) and isinstance(a, ast.Assign):
                if len(a.targets) != 1 or dump(a.targets[0]) != dump(b.targets[0]) or not isinstance(a.targets[0], ast.Name):
                    return False
                found.append((a, b, stack, a.targets[0].id, cnode))
                continue
            if stack and type(a) is type(b) and isinstance(a, ast.AnnAssign):
                if not isinstance(a.target, ast.Name) or dump(a.target) != dump(b.target) or \
                        dump(a.annotation) != dump(b.annotation) or a.value is None or b.value is None:
                    return False
                found.append((a, b, stack, a.target.id, cnode))
                continue
            return False
        return True

    if not walk(old_tree.body, new_tree.body, [], None) or len(found) != 1:
        return None
    a, b, stack, name, cnode = found[0]
    if name.startswith("_"):
        return None
    if not (simple_value(a.value, old_text) and simple_value(b.value, old_text)):
        return None
    if not literal_ok(b.value) or bare_use(cnode, a, name):
        return None
    return a.lineno, ".".join(stack), name


class Attr:
    def __init__(self, maps_dir: str):
        self.ok = False
        if os.environ.get("BR_NO_L2") == "1":
            return
        p = os.path.join(maps_dir, "attrreads.bin")
        if not os.path.exists(p):
            return
        try:
            with open(p, "rb") as f:
                d = marshal.loads(zlib.decompress(f.read()))
            if d.get("v") != 1:
                return
            self.names = frozenset(d["names"])
            self.sets = d["sets"]
            self.reads = d["reads"]
            self.derived = {q: frozenset(v) for q, v in d["derived"].items()}
            self.enums = frozenset(d["enums"])
            self.seen = frozenset(n for v in d["reads"].values() for n in v)
            self.unrecorded = 0
            for n in d["unrecorded"]:
                self.unrecorded |= 1 << n
            self.ok = True
        except Exception:
            self.ok = False

    def readers(self, path: str, cls: str, name: str):
        """(mask of the tests that read name through cls or a subclass, mask of tests with no recording) or None: use today's rule."""
        if not self.ok or not cls or name not in self.names:
            return None
        q = f"{module_of(path)}:{cls}"
        if q in self.enums or name in self.derived.get(q, ()):
            return None
        i = self.reads.get(q, {}).get(name)
        if not i:
            # nobody read it through this class: fine when the recorder saw the name read elsewhere (so the
            # instrument covers it); a name no test ever read gets today's rule
            return (0, self.unrecorded) if name in self.seen else None
        return self.sets[i], self.unrecorded
