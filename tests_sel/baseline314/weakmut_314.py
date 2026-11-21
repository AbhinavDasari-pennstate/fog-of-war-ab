# Frozen copy of selector/weakmut.py at 314df58, the differential oracle for tests_sel/test_cpu_diet.py. Do not edit.
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
  B  BoolOp (and/or)                         only the evaluated bit: a swap skips or adds operands (side effects,
                                             value context), so it is dropped only if the site never ran
  N  UnaryOp Not                             only the evaluated bit (removing `not` always differs)
  bit 10 = the site was reached at least once in the recorded runs (recorded BEFORE its operands run)
  bit 12 = "unknown": the recorded runs disagreed or an operand came from hash()/id(): never dropped
Only code that runs when a function or lambda body runs counts (not defaults, decorators, annotations
of the def, class bodies, module level).
"""

import ast

RECORDER_VERSION = 3
OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn)
EVAL_BIT = 1 << 10
ALT_BIT = 1 << 11
UNKNOWN_BIT = 1 << 12


def op_index(op) -> int:
    for i, t in enumerate(OPS):
        if type(op) is t:
            return i
    return -1


def site_key(rel: str, node, kind: str) -> str:
    return f"{rel}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}:{kind}"


def key_tuple(node, kind: str):
    return (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, kind)


def tainted(node) -> bool:
    """An operand may come from hash() or id(): its order alternatives depend on the process, never model them."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("hash", "id"):
            return True
        if isinstance(n, ast.Attribute) and n.attr in ("__hash__", "__eq__", "_hash"):
            return True
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
            elif isinstance(n, ast.BoolOp):
                sites[id(n)] = "B"
            elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
                sites[id(n)] = "N"
        for c in ast.iter_child_nodes(n):
            go(c, fn)

    go(tree, False)
    return sites


def bool_nodes(tree) -> set:
    """ids of expressions whose value is only used for its truth value (kept for tooling; the rule no longer
    relies on it). Operands of a BoolOp are truth-only only when the BoolOp itself is: `(a and b) or c` used
    as a value hands the inner BoolOp's value to the caller."""
    out = set()
    for n in ast.walk(tree):  # parents are visited before children
        if isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            out.add(id(n.test))
        elif isinstance(n, ast.comprehension):
            out.update(id(i) for i in n.ifs)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            out.add(id(n.operand))
        elif isinstance(n, ast.BoolOp) and id(n) in out:
            out.update(id(v) for v in n.values)
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
    if mask & UNKNOWN_BIT:
        return False
    if not mask & EVAL_BIT:
        return True  # the site never ran in any recorded run
    if kind == "C":
        return not mask & (1 << j)
    return False  # B and N: a swap or a removed `not` can differ on any evaluation


_STORE_METHODS = frozenset({"setdefault", "update", "append", "add", "pop", "popitem", "clear", "extend", "insert", "remove", "discard"})


def _root(node):
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def writes_shared_state(tree, target) -> bool:
    """True unless the function holding `target` provably writes only to its own locals and arguments' objects.

    A function that stores into cls, a class name, a module level name or a global, mutates a container with such a root,
    or is cached, may leave the changed value behind for later tests (Dialect._classes is filled this way). Anything not
    inside a plain function (a lambda, a class body) counts as shared."""
    parents = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[id(c)] = n
    fn = parents.get(id(target))
    while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        fn = parents.get(id(fn))
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return True
    module_names = set()
    for n in tree.body:
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            module_names.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                for e in ast.walk(t):
                    if isinstance(e, ast.Name):
                        module_names.add(e.id)
    for d in fn.decorator_list:
        text = ast.dump(d)
        if "cache" in text.lower() or "cached" in text.lower():
            return True

    def shared(root):
        return root is not None and (root == "cls" or root in module_names or root[:1].isupper())

    for n in ast.walk(fn):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            return True
        targets = []
        if isinstance(n, ast.Assign):
            targets = n.targets
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            targets = [n.target]
        elif isinstance(n, ast.Delete):
            targets = n.targets
        for t in targets:
            for e in ast.walk(t):
                if isinstance(e, (ast.Attribute, ast.Subscript)) and isinstance(e.ctx, (ast.Store, ast.Del)) and shared(_root(e)):
                    return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _STORE_METHODS and shared(_root(n.func.value)):
            return True
    return False


class Profile:
    """Compact weak-mutation profile of many tests (maps/weakmut.bin, format 3).

    recorded: bitmask of the tests with a complete recording (a test that failed, was skipped or timed out
    while recording is not in it, so it is never dropped). sites: path -> {(line, col, end_line, end_col, kind):
    [(mask, tests bitmask), ...]}, one group per distinct merged summary mask; a recorded test that appears in
    no group of a site never ran there."""

    def __init__(self, recorded: int, sites: dict):
        self.recorded = recorded
        self.sites = sites

    def __bool__(self):
        return bool(self.recorded)

    def groups(self, path, key):
        return self.sites.get(path, {}).get(key, ())

    def droppable_tests(self, path, key, kind, j, propagate=True) -> int:
        """Recorded tests the mutation cannot affect. A test that evaluates the site with a different value keeps itself and
        every test numbered after it (the suite runs in test number order in one process): the changed value may have been
        written into process wide state (a registry, a cache) that a later test only reads."""
        keep = 0
        for mask, bits in self.groups(path, key):
            if not droppable(kind, j, mask):
                keep |= bits
        if keep and propagate:
            first = (keep & -keep).bit_length() - 1
            keep |= ~((1 << first) - 1)
        return self.recorded & ~keep


def _bits_of(mask: int):
    n = 0
    while mask:
        low = mask & -mask
        n = low.bit_length() - 1
        yield n
        mask ^= low


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
    if isinstance(summaries, Profile):
        propagate = writes_shared_state(old_tree, a)
        out = set(_bits_of(summaries.droppable_tests(path, key, kind, j, propagate) & ~srcreaders))
        return out, f"weakmut {path}:{a.lineno} {kind}{'' if j is None else ' -> ' + OPS[j].__name__}: dropped {len(out)} tests"
    for t, per in summaries.items():
        if (srcreaders >> t) & 1:
            continue
        mask = per.get(path, {}).get(key, 0)
        if droppable(kind, j, mask):
            out.add(t)
    return out, f"weakmut {path}:{a.lineno} {kind}{'' if j is None else ' -> ' + OPS[j].__name__}: dropped {sorted(out)}"
