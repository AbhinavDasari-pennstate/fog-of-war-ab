"""V: narrow an edit of constants inside the ENTRY VALUES of a dict constant to the tests that read those values
(maps/valreads.bin, recorded by tools/run_valmap.sh).

Stdlib only. `site_nodes(tree)` names the module-level and class-level `NAME = {...}` statements the recorder
wraps (a dict display with at least one entry, only distinct constant keys, no `**`); `entries(...)` says whether a
patch changes nothing but constants inside the values of such entries, each value a pure literal tree
(constants, tuples and lists of them, a sign on a number): evaluating the new value cannot run code, raise or
reach anything but the entry. `Val.readers(...)` then gives the tests that read that entry's value (or read all
values of the dict in bulk), or None when today's rule must apply (the entry is read while a module or class body
runs or outside any test, its readers differ between runs, or the dict statement was never created in a recorded run).
"""

import ast
import marshal
import os
import zlib

_SIGN = (ast.USub, ast.UAdd)


def key_label(k):
    """The name of a dict key in the map: repr of a constant, "@" + source text of a name or attribute chain, else None."""
    if isinstance(k, ast.Constant):
        try:
            return repr(k.value)
        except Exception:
            return None
    n = k
    while isinstance(n, ast.Attribute):
        n = n.value
    if isinstance(n, ast.Name) and isinstance(k, (ast.Name, ast.Attribute)):
        return "@" + ast.unparse(k)
    return None


def explicit_keys(v):
    """The (key node, value node) pairs of a dict display that are not `**spread` entries."""
    return [(k, x) for k, x in zip(v.keys, v.values) if k is not None]


def eligible(v) -> bool:
    """A dict display with at least one explicit entry whose keys are all constants or name / attribute chains with
    distinct labels. `**spread` entries are allowed: they are read (and recorded) through the dict they spread."""
    if not isinstance(v, ast.Dict):
        return False
    ex = explicit_keys(v)
    if not ex:
        return False
    labels = [key_label(k) for k, _ in ex]
    return None not in labels and len(set(labels)) == len(labels)


def site_nodes(tree):
    """[(statement, class qualname, name)] for every eligible assignment of a module body or a (nested) class body."""
    out = []

    def visit(body, stack):
        for s in body:
            if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name) and eligible(s.value):
                out.append((s, ".".join(stack), s.targets[0].id))
            elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.value is not None and eligible(s.value):
                out.append((s, ".".join(stack), s.target.id))
            elif isinstance(s, ast.ClassDef):
                visit(s.body, stack + [s.name])

    visit(tree.body, [])
    return out


def set_eligible(v) -> bool:
    """A set display of at least one string constant, no repeats."""
    if not isinstance(v, ast.Set) or not v.elts:
        return False
    if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in v.elts):
        return False
    return len({e.value for e in v.elts}) == len(v.elts)


def set_nodes(tree):
    """[(statement, class qualname, name)] for every module- or class-level `NAME = {"a", "b"}` of string constants."""
    out = []

    def visit(body, stack):
        for s in body:
            if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name) and set_eligible(s.value):
                out.append((s, ".".join(stack), s.targets[0].id))
            elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.value is not None and set_eligible(s.value):
                out.append((s, ".".join(stack), s.target.id))
            elif isinstance(s, ast.ClassDef):
                visit(s.body, stack + [s.name])

    visit(tree.body, [])
    return out


def literal_tree(n) -> bool:
    """A constant, a tuple or list of literal trees, or a sign on a number constant."""
    if isinstance(n, ast.Constant):
        return True
    if isinstance(n, ast.UnaryOp):
        return isinstance(n.op, _SIGN) and isinstance(n.operand, ast.Constant) and isinstance(n.operand.value, (int, float, complex)) \
            and not isinstance(n.operand.value, bool)
    if isinstance(n, (ast.Tuple, ast.List)):
        return all(literal_tree(e) for e in n.elts)
    return False


class _E(tuple):
    """(statement line, key repr, truth changed) with the strings of the key and of the old and new value in .tokens"""

    tokens = frozenset()


def entries(old_tree, new_tree, diffs):
    """[(statement line, key repr, truth changed)] of the entries a patch edits, or None when the patch is anything else.

    diffs: pyscope.ast_diff(old, new, ranges): the old nodes at which the trees stop agreeing. Every one must be a
    Constant (so the trees differ only in constant values: a changed shape shows up as a non-Constant parent) that
    sits inside the value of one entry of an eligible dict statement, whose whole value is a literal tree.
    "truth changed" says whether bool(old value) != bool(new value) for that entry (what `required_args` looks at).
    """
    if not diffs or not all(isinstance(n, ast.Constant) and d is None for n, d in diffs):
        return None
    olds, news = site_nodes(old_tree), site_nodes(new_tree)
    if len(olds) != len(news):
        return None
    where = {}
    vals = {}
    for (stmt, _, _), (nstmt, _, _) in zip(olds, news):
        if [None if k is None else key_label(k) for k in stmt.value.keys] != [None if k is None else key_label(k) for k in nstmt.value.keys]:
            return None
        for k, v, nv in zip(stmt.value.keys, stmt.value.values, nstmt.value.values):
            if k is None:
                continue
            if not literal_tree(v) or not literal_tree(nv):
                continue
            e = (stmt.lineno, key_label(k))
            vals[e] = (v, nv, k)
            for sub in ast.walk(v):
                where[id(sub)] = e
    out = []
    for n, _ in diffs:
        e = where.get(id(n))
        if e is None:
            return None
        if e not in out:
            out.append(e)
    res = []
    for e in out:
        v, nv, k = vals[e]
        try:
            changed = bool(ast.literal_eval(v)) != bool(ast.literal_eval(nv))
        except Exception:
            return None
        r = _E((e[0], e[1], changed))
        r.tokens = frozenset(c.value for x in (v, nv, k) for c in ast.walk(x) if isinstance(c, ast.Constant) and isinstance(c.value, str))
        res.append(r)
    return res


def set_entries(old_tree, new_tree, diffs):
    """[(statement line, [repr of every word the edit removes or adds])] when the patch only changes string constants that
    are elements of eligible set displays (same size, so no word is added or dropped other than by the rename), else None."""
    if not diffs or not all(isinstance(n, ast.Constant) and d is None for n, d in diffs):
        return None
    olds, news = set_nodes(old_tree), set_nodes(new_tree)
    if len(olds) != len(news):
        return None
    where = {}
    for (stmt, _, _), (nstmt, _, _) in zip(olds, news):
        if len(stmt.value.elts) != len(nstmt.value.elts):
            return None
        for e, ne in zip(stmt.value.elts, nstmt.value.elts):
            where[id(e)] = (stmt.lineno, e, ne)
    out = {}
    for n, _ in diffs:
        w = where.get(id(n))
        if w is None:
            return None
        ln, e, ne = w
        words = out.setdefault(ln, [])
        for x in (e.value, ne.value):
            r = repr(x)
            if r not in words:
                words.append(r)
    return sorted(out.items())


class Val:
    def __init__(self, maps_dir: str):
        self.ok = False
        if os.environ.get("BR_NO_VAL") == "1":
            return
        p = os.path.join(maps_dir, "valreads.bin")
        if not os.path.exists(p):
            return
        try:
            with open(p, "rb") as f:
                d = marshal.loads(zlib.decompress(f.read()))
            if d.get("v") != 1 or d.get("runs", 0) < 2:  # one recording cannot tell a hash-order effect from a read
                return
            self.sets = d["sets"]
            self.reads = d["reads"]
            self.derived = {s: frozenset(v) for s, v in d["derived"].items()}
            self.sites = frozenset(d["sites"])
            self.req = frozenset(d.get("req", ()))
            self.req2 = frozenset(d.get("req2", ()))
            self.strict = frozenset(d.get("strict", ()))
            # format strings seen by sqlglot.time.format_time, with the tests that passed each: [(string, mask)]
            self.ft = [(None if k == "*" else ast.literal_eval(k), self.sets[i]) for k, i in d["reads"].get("FT", {}).items()]
            self.ft_derived = bool(d["derived"].get("FT"))
            self.unrecorded = 0
            for n in d["unrecorded"]:
                self.unrecorded |= 1 << n
            self.ok = True
        except Exception:
            self.ok = False

    def set_readers(self, path: str, line: int, words):
        """Mask of the tests that looked up one of `words` in the set statement at path:line, or read the set as a whole
        (plus unrecorded tests); None when the set is unknown or was read as a whole while a module or class body ran."""
        if not self.ok:
            return None
        site = f"{path}:{line}"
        if site not in self.sites:
            return None
        der = self.derived.get(site, ())
        if "*" in der or any(w in der for w in words):
            return None
        r = self.reads.get(site, {})
        mask = self.sets[r.get("*", 0)] | self.unrecorded
        for w in words:
            mask |= self.sets[r.get(w, 0)]
        return mask

    def readers(self, path: str, line: int, key: str, truth_changed: bool = False, tokens=(), extra: int = 0):
        """Mask of the tests that read the value of `key` in the dict statement at path:line (plus every unrecorded
        test), or None: today's rule applies.

        A site in `req` is an `arg_types` dict whose values were also read, at class creation, by the one comprehension
        that builds `required_args` (a value matters there only through bool(value)). Its other consumer is
        Expression.error_messages, which reports key k only for an instance that has no value for k. So when the
        truth of the value changes, the tests that validated such an instance (the recorded "M:" entries) are added;
        when it does not, `required_args` is the same set and nothing is added."""
        if not self.ok:
            return None
        site = f"{path}:{line}"
        if site not in self.sites:
            return None
        der = self.derived.get(site, ())
        if "*" in der or key in der:
            return None
        r = self.reads.get(site, {})
        mask = self.sets[r.get(key, 0)] | self.sets[r.get("*", 0)] | self.unrecorded
        if site in self.req2:
            # TIME_MAPPING / FORMAT_MAPPING of a dialect: the metaclass turns the values into the inverse mapping and tries; the
            # only consumers of those are format_time (every call's format string is recorded: a changed key or value, in either
            # direction, can only matter to a call whose string contains it, since format_time matches whole keys in the trie
            # walk) and tests/test_time.py (`extra`). The strict and lax formats are added because _with_strict_time_inverse
            # inserts them depending on which of them the inverse mapping holds.
            if self.ft_derived or not tokens:
                return None
            toks = set(tokens) | self.strict
            for text, m in self.ft:
                if text is None or any(t in text for t in toks):
                    mask |= m
            mask |= extra
        if site in self.req:
            mk = "M:" + key
            if truth_changed:
                if mk in der:
                    return None
                mask |= self.sets[r.get(mk, 0)]
        return mask
