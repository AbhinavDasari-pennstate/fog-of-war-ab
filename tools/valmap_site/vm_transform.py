"""AST rewrite for the value-read recorder (map build only, laptop).

A module-level or class-level `NAME = {...}` (or `NAME: T = {...}`) whose value is a dict display with at least one
entry, only distinct constant keys and no `**` becomes `NAME = _vw({...}, "<relpath>:<line of the statement>")`.
valrule.site_nodes() in the selector enumerates the same statements, so a site is named the same way on both sides.
"""
import ast
import importlib.util
import os

# one definition of an eligible statement, shared with the selector (loaded by path: nothing is added to sys.path)
_p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "selector", "valrule.py")
_spec = importlib.util.spec_from_file_location("_valrule_shared", _p)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
site_nodes = _m.site_nodes
_label = _m.key_label
set_nodes = _m.set_nodes


def transform(tree, relpath):
    for stmt, _, name in site_nodes(tree):
        site = f"{relpath}:{stmt.lineno}"
        args = [stmt.value, ast.Constant(value=site)]
        ex = [k for k in stmt.value.keys if k is not None]
        spread = len(ex) != len(stmt.value.keys)
        tm = name in ("TIME_MAPPING", "FORMAT_MAPPING") and relpath.startswith("sqlglot/dialects/")
        if spread or tm or any(not isinstance(k, ast.Constant) for k in ex):
            # keys written as names or attribute chains: evaluate them once more (a name lookup has no effect) so the
            # recorder can label the runtime key objects with their source text
            args.append(ast.Tuple(elts=[ast.copy_location(ast.parse(ast.unparse(k), mode="eval").body, k) for k in ex], ctx=ast.Load()))
            args.append(ast.Tuple(elts=[ast.Constant(value=_label(k)) for k in ex], ctx=ast.Load()))
            args.append(ast.Constant(value=spread))
            args.append(ast.Constant(value=tm))
        call = ast.Call(func=ast.Name(id="_vw", ctx=ast.Load()), args=args, keywords=[])
        stmt.value = ast.copy_location(call, stmt.value)
    ast.fix_missing_locations(tree)
    return tree


def _call(name, args, node):
    return ast.copy_location(ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=args, keywords=[]), node)


def rewrite_set_loads(tree, names):
    """Every read of a name in `names` becomes a recorded whole-set read, except `x in NAME` / `x not in NAME` which
    becomes a recorded lookup of the word x. getattr, vars and __dict__ are routed through the recorder too."""

    def is_set_ref(n):
        return (isinstance(n, ast.Attribute) and n.attr in names and isinstance(n.ctx, ast.Load)) or \
            (isinstance(n, ast.Name) and n.id in names and isinstance(n.ctx, ast.Load))

    class R(ast.NodeTransformer):
        def visit_Compare(self, node):
            if len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)) and is_set_ref(node.comparators[0]):
                right = node.comparators[0]
                if isinstance(right, ast.Attribute):
                    right = ast.copy_location(ast.Attribute(value=self.visit(right.value), attr=right.attr, ctx=ast.Load()), right)
                left = self.visit(node.left)
                call = _call("_mem", [left, right], node)
                if isinstance(node.ops[0], ast.NotIn):
                    return ast.copy_location(ast.UnaryOp(op=ast.Not(), operand=call), node)
                return call
            return self.generic_visit(node)

        def visit_Attribute(self, node):
            node = self.generic_visit(node)
            if isinstance(node.ctx, ast.Load):
                if node.attr in names:
                    return _call("_bulk", [node], node)
                if node.attr == "__dict__":
                    return _call("_sd", [node.value], node)
            return node

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load) and node.id in names:
                return _call("_bulk", [node], node)
            return node

        def visit_Call(self, node):
            node = self.generic_visit(node)
            f = node.func
            if isinstance(f, ast.Name) and not node.keywords and not any(isinstance(a, ast.Starred) for a in node.args):
                if f.id == "getattr" and len(node.args) in (2, 3):
                    node.func = ast.copy_location(ast.Name(id="_gets", ctx=ast.Load()), f)
                elif f.id == "vars" and len(node.args) == 1:
                    node.func = ast.copy_location(ast.Name(id="_sv", ctx=ast.Load()), f)
            return node

    return R().visit(tree)


def wrap_sets(tree, relpath):
    for stmt, _, _ in set_nodes(tree):
        stmt.value = _call("_sw", [stmt.value, ast.Constant(value=f"{relpath}:{stmt.lineno}")], stmt.value)


def compile_instrumented(data, path, relpath, setnames=frozenset()):
    tree = ast.parse(data, path)
    if relpath.startswith("sqlglot/"):
        tree = transform(tree, relpath)
        wrap_sets(tree, relpath)
    if setnames:
        tree = rewrite_set_loads(tree, setnames)
    ast.fix_missing_locations(tree)
    return compile(tree, path, "exec", dont_inherit=True)
