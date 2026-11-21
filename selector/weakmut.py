"""Weak-mutation test dropping ("infection condition"), stdlib only.

A single operator mutant inside a function body changes a program only when the mutated expression
evaluates to a different value. Map build time (tools/weakmut_build.py) runs a few giant tests with
an instrumented sqlglot that records, per expression site, which alternative operators would have
produced a different (or raising) result on any evaluation the test performed. At run time a giant
test is dropped when the patch is exactly one recognised mutation at a site where the mutated
operator agrees with the original on every recorded evaluation (or the site never ran at all).
Anything else (other shapes, several edits, class or module level, unknown site) keeps the test.

Sites (shared by the instrumenter and the selector, so both agree):
  C  Compare with exactly one operator       summary bit j = replacing the operator by OPS[j] differs or raises
  B  BoolOp (and/or)                         bit 11 = swapping and/or differs (bool ctx: truth value, else identity)
  N  UnaryOp Not                             only the evaluated bit (removing `not` always differs)
  bit 10 = the site was evaluated at least once in the recorded run
Only code that runs when a function or lambda body runs counts (not defaults, decorators, annotations
of the def, class bodies, module level).
"""

import ast

OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn)
EVAL_BIT = 1 << 10
ALT_BIT = 1 << 11
_FORBID = (ast.Yield, ast.YieldFrom, ast.Await, ast.NamedExpr)


def op_index(op) -> int:
    for i, t in enumerate(OPS):
        if type(op) is t:
            return i
    return -1


def site_key(rel: str, node, kind: str) -> str:
    return f"{rel}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}:{kind}"


def key_tuple(node, kind: str):
    return (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, kind)


def _thunk_ok(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, _FORBID):
            return False
        if isinstance(n, ast.Name) and n.id in ("super", "__class__"):
            return False
    return True


def pure(node) -> bool:
    """Side-effect free to evaluate speculatively (names, constants, attributes, subscripts, comparisons)."""
    if isinstance(node, (ast.Name, ast.Constant)):
        return True
    if isinstance(node, ast.Attribute):
        return pure(node.value)
    if isinstance(node, ast.Subscript):
        return pure(node.value) and pure(node.slice)
    if isinstance(node, ast.Compare):
        return pure(node.left) and all(pure(c) for c in node.comparators)
    if isinstance(node, ast.BoolOp):
        return all(pure(v) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub)):
        return pure(node.operand)
    if isinstance(node, ast.Tuple):
        return all(pure(e) for e in node.elts)
    return False


def collect(tree) -> dict:
    """id(node) -> kind for every instrumentable site (only inside function or lambda bodies)."""
    sites = {}

    def go(n, fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                go(d, fn)
            go(n.args, fn)
            if n.returns is not None:
                go(n.returns, fn)
            for s in n.body:
                go(s, True)
            return
        if isinstance(n, ast.Lambda):
            go(n.args, fn)
            go(n.body, True)
            return
        if isinstance(n, ast.ClassDef):
            for d in n.decorator_list:
                go(d, fn)
            for b in n.bases:
                go(b, fn)
            for k in n.keywords:
                go(k, fn)
            for s in n.body:
                go(s, False)
            return
        if fn:
            if isinstance(n, ast.Compare) and len(n.ops) == 1 and op_index(n.ops[0]) >= 0:
                sites[id(n)] = "C"
            elif isinstance(n, ast.BoolOp) and all(_thunk_ok(v) for v in n.values):
                sites[id(n)] = "B"
            elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
                sites[id(n)] = "N"
        for c in ast.iter_child_nodes(n):
            go(c, fn)

    go(tree, False)
    return sites


def bool_nodes(tree) -> set:
    """ids of expressions whose value is only used for its truth value."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            out.add(id(n.test))
        elif isinstance(n, ast.comprehension):
            out.update(id(i) for i in n.ifs)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            out.add(id(n.operand))
        elif isinstance(n, ast.BoolOp):
            out.update(id(v) for v in n.values[:-1])
            if id(n) in out:
                out.add(id(n.values[-1]))
    return out


# ---- run time: recognise the mutation -------------------------------------------------------

def _diff(a, b, out, pa, pb):
    if type(a) is not type(b):
        if isinstance(a, (ast.cmpop, ast.boolop, ast.operator, ast.unaryop)):
            out.append((pa, pb))
        else:
            out.append((a, b) if isinstance(a, ast.AST) and isinstance(b, ast.AST) else (pa, pb))
        return
    if isinstance(a, ast.AST):
        for f in a._fields:
            _diff(getattr(a, f, None), getattr(b, f, None), out, a, b)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append((pa, pb))
            return
        for x, y in zip(a, b):
            _diff(x, y, out, pa, pb)
    elif a != b:
        out.append((pa, pb))


def find_mutation(old_tree, new_tree):
    """(old_node, new_node) when the two trees differ in exactly one place, else None."""
    out = []
    _diff(old_tree, new_tree, out, old_tree, new_tree)
    if len(out) != 1:
        return None
    a, b = out[0]
    if not isinstance(a, ast.AST) or not isinstance(b, ast.AST) or not hasattr(a, "lineno"):
        return None
    return a, b


def classify(old_tree, a, b):
    """('C', mutated_op_index) | ('B', None) | ('N', None) | None for the mutation a -> b."""
    sites = collect(old_tree)
    kind = sites.get(id(a))
    if kind is None:
        return None
    if kind == "C":
        if not (isinstance(b, ast.Compare) and len(b.ops) == 1):
            return None
        if ast.dump(a.left) != ast.dump(b.left) or ast.dump(a.comparators[0]) != ast.dump(b.comparators[0]):
            return None
        j = op_index(b.ops[0])
        if j < 0 or j == op_index(a.ops[0]):
            return None
        return "C", j
    if kind == "B":
        if isinstance(b, ast.BoolOp) and type(a.op) is not type(b.op) and ast.dump(ast.Tuple(a.values, ast.Load())) == ast.dump(ast.Tuple(b.values, ast.Load())):
            return "B", None
        return None
    if kind == "N":
        if ast.dump(b) == ast.dump(a.operand):
            return "N", None
        return None
    return None


def droppable(kind, j, mask: int) -> bool:
    """True when the mutated expression cannot behave differently given the recorded summary."""
    if not mask & EVAL_BIT:
        return True  # the site never ran in the giant's recorded run
    if kind == "C":
        return not mask & (1 << j)
    if kind == "B":
        return not mask & ALT_BIT
    return False  # N: removing `not` always differs once evaluated


def drops(path, patch_changes, old_text, new_text, summaries, srcreaders=0):
    """Giant test numbers (keys of summaries) the single mutation provably cannot affect.

    summaries: {test number: {path: {(line, col, end_line, end_col, kind): mask}}}. Returns (set, note).
    """
    if not summaries:
        return set(), ""
    old_tree = ast.parse(old_text, path)
    new_tree = ast.parse(new_text, path)
    m = find_mutation(old_tree, new_tree)
    if m is None:
        return set(), "weakmut: not a single-site change"
    a, b = m
    cl = classify(old_tree, a, b)
    if cl is None:
        return set(), "weakmut: shape not covered"
    kind, j = cl
    key = key_tuple(a, kind)
    out = set()
    for t, per in summaries.items():
        if (srcreaders >> t) & 1:
            continue
        mask = per.get(path, {}).get(key, 0)
        if droppable(kind, j, mask):
            out.add(t)
    return out, f"weakmut {path}:{a.lineno} {kind}{'' if j is None else ' -> ' + OPS[j].__name__}: dropped {sorted(out)}"
