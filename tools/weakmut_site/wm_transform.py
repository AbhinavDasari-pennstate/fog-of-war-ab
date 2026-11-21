"""AST rewrite for the weak-mutation map build (laptop only)."""
import ast

import weakmut as W


def _call(name, args, node):
    c = ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=args, keywords=[])
    return ast.copy_location(c, node)


def _const(v, node):
    return ast.copy_location(ast.Constant(value=v), node)


def _marked(key, node, expr):
    """`(_wm_m(key) or expr)`: the marker runs first and returns False, so the value is exactly expr."""
    marker = _call("_wm_m", [_const(key, node)], node)
    return ast.copy_location(ast.BoolOp(op=ast.Or(), values=[marker, expr]), node)


def transform(tree, rel):
    sites = W.collect(tree)

    class R(ast.NodeTransformer):
        def visit_Compare(self, node):
            kind = sites.get(id(node))
            key = W.site_key(rel, node, "C") if kind == "C" else None
            taint = W.tainted(node) if kind == "C" else False
            node = self.generic_visit(node)
            if kind != "C":
                return node
            call = _call("_wm_c", [_const(key, node), _const(W.op_index(node.ops[0]), node), _const(taint, node),
                                   node.left, node.comparators[0]], node)
            return _marked(key, node, call)

        def visit_UnaryOp(self, node):
            kind = sites.get(id(node))
            key = W.site_key(rel, node, "N") if kind == "N" else None
            node = self.generic_visit(node)
            return _marked(key, node, node) if kind == "N" else node

        def visit_BoolOp(self, node):
            kind = sites.get(id(node))
            key = W.site_key(rel, node, "B") if kind == "B" else None
            node = self.generic_visit(node)
            return _marked(key, node, node) if kind == "B" else node

    new = R().visit(tree)
    ast.fix_missing_locations(new)
    return new


def compile_instrumented(data, path, rel):
    tree = ast.parse(data, path)
    return compile(transform(tree, rel), path, "exec", dont_inherit=True)
