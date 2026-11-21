"""AST rewrite for the attribute-read recorder (map build only, laptop)."""
import ast


def _call(name, args, node):
    return ast.copy_location(ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=args, keywords=[]), node)


def transform(tree, names):
    class R(ast.NodeTransformer):
        def visit_Attribute(self, node):
            node = self.generic_visit(node)
            if not isinstance(node.ctx, ast.Load):
                return node
            if node.attr in names:
                return _call("_ar", [node.value, ast.copy_location(ast.Constant(value=node.attr), node)], node)
            if node.attr == "__dict__":
                return _call("_ard", [node.value], node)
            return node

        def visit_Call(self, node):
            node = self.generic_visit(node)
            f = node.func
            if isinstance(f, ast.Name) and not node.keywords and not any(isinstance(a, ast.Starred) for a in node.args):
                if f.id == "getattr" and len(node.args) in (2, 3):
                    node.func = ast.copy_location(ast.Name(id="_arg", ctx=ast.Load()), f)
                elif f.id == "hasattr" and len(node.args) == 2:
                    node.func = ast.copy_location(ast.Name(id="_arh", ctx=ast.Load()), f)
                elif f.id == "vars" and len(node.args) == 1:
                    node.func = ast.copy_location(ast.Name(id="_arv", ctx=ast.Load()), f)
            return node

    new = R().visit(tree)
    ast.fix_missing_locations(new)
    return new


def compile_instrumented(data, path, names):
    tree = ast.parse(data, path)
    return compile(transform(tree, names), path, "exec", dont_inherit=True)
