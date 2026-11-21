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


def transform(tree, relpath):
    for stmt, _, _ in site_nodes(tree):
        site = f"{relpath}:{stmt.lineno}"
        args = [stmt.value, ast.Constant(value=site)]
        ex = [k for k in stmt.value.keys if k is not None]
        spread = len(ex) != len(stmt.value.keys)
        if spread or any(not isinstance(k, ast.Constant) for k in ex):
            # keys written as names or attribute chains: evaluate them once more (a name lookup has no effect) so the
            # recorder can label the runtime key objects with their source text
            args.append(ast.Tuple(elts=[ast.copy_location(ast.parse(ast.unparse(k), mode="eval").body, k) for k in ex], ctx=ast.Load()))
            args.append(ast.Tuple(elts=[ast.Constant(value=_label(k)) for k in ex], ctx=ast.Load()))
            args.append(ast.Constant(value=spread))
        call = ast.Call(func=ast.Name(id="_vw", ctx=ast.Load()), args=args, keywords=[])
        stmt.value = ast.copy_location(call, stmt.value)
    ast.fix_missing_locations(tree)
    return tree


def compile_instrumented(data, path, relpath):
    tree = ast.parse(data, path)
    if relpath.startswith("sqlglot/"):
        tree = transform(tree, relpath)
    return compile(tree, path, "exec", dont_inherit=True)
