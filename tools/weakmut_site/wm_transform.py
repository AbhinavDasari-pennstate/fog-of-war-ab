"""AST rewrite for the weak-mutation map build (laptop only)."""
import ast
import copy

import weakmut as W


def _call(name, args, node):
    c = ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=args, keywords=[])
    return ast.copy_location(c, node)


def _const(v, node):
    return ast.copy_location(ast.Constant(value=v), node)


def transform(tree, rel):
    sites = W.collect(tree)
    bools = W.bool_nodes(tree)
    empty = ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[])

    class R(ast.NodeTransformer):
        def visit_Compare(self, node):
            kind = sites.get(id(node))
            key = W.site_key(rel, node, "C") if kind == "C" else None
            nid = id(node)
            node = self.generic_visit(node)
            if kind != "C":
                return node
            return _call("_wm_c", [_const(key, node), _const(W.op_index(node.ops[0]), node), node.left, node.comparators[0]], node)

        def visit_UnaryOp(self, node):
            kind = sites.get(id(node))
            key = W.site_key(rel, node, "N") if kind == "N" else None
            node = self.generic_visit(node)
            if kind != "N":
                return node
            return _call("_wm_n", [_const(key, node), node.operand], node)

        def visit_BoolOp(self, node):
            kind = sites.get(id(node))
            key = W.site_key(rel, node, "B") if kind == "B" else None
            bctx = id(node) in bools
            pures = [W.pure(v) for v in node.values]
            node = self.generic_visit(node)
            if kind != "B":
                return node
            ths = []
            for v in node.values:
                lam = ast.copy_location(ast.Lambda(args=copy.deepcopy(empty), body=v), v)
                ths.append(lam)
            args = [_const(key, node), _const(isinstance(node.op, ast.And), node), _const(bctx, node),
                    ast.copy_location(ast.Tuple(elts=[_const(p, node) for p in pures], ctx=ast.Load()), node)] + ths
            return _call("_wm_b", args, node)

    new = R().visit(tree)
    ast.fix_missing_locations(new)
    return new


def compile_instrumented(data, path, rel):
    tree = ast.parse(data, path)
    return compile(transform(tree, rel), path, "exec", dont_inherit=True)
