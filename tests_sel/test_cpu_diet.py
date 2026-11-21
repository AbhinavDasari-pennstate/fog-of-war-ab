"""CPU diet: the selector's answers must not change, only the work behind them.

Every optimisation here is checked against a frozen copy of the 314df58 module (tests_sel/baseline314/): same
inputs, same outputs. The real-file cases need the pinned sqlglot checkout (BR_SRC)."""

import ast
import importlib.util
import re
from pathlib import Path

import pytest

import attrrule
import pyscope
import weakmut
from conftest import PIN, needs_pin

HERE = Path(__file__).resolve().parent


def _load_base(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_314", HERE / "baseline314" / f"{name}_314.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


attrrule_314 = _load_base("attrrule")
weakmut_314 = _load_base("weakmut")
pyscope_314 = _load_base("pyscope")

REAL_FILES = [
    "sqlglot/expressions/core.py",
    "sqlglot/dialects/mysql.py",
    "sqlglot/optimizer/qualify_columns.py",
    "sqlglot/dialects/dialect.py",
]
# (path, variants per edit pattern): one 11 000 line file with fewer variants, to keep the run short
REAL_CASES = [(p, 2) for p in REAL_FILES] + [("sqlglot/parser.py", 1)]

_PATTERNS = [
    (re.compile(r"^(\s+)([A-Z][A-Z_0-9]*) = True$"), r"\1\2 = False"),
    (re.compile(r"^(\s+)([A-Z][A-Z_0-9]*) = False$"), r"\1\2 = True"),
    (re.compile(r"^(\s+)([A-Z][A-Z_0-9]*) = (\d+)$"), r"\1\2 = 7"),
    (re.compile(r"(\S) == (\S)"), r"\1 != \2"),
    (re.compile(r" is None"), " is not None"),
    (re.compile(r" and "), " or "),
    (re.compile(r"^(\s+)def (\w+)\("), r"\1def \2_x("),
    (re.compile(r"^(\s+)(\w+) = (\w+)$"), r"\1\2 = \3 or 1"),
    (re.compile(r"\bTrue\b"), "False"),
    (re.compile(r"\bself\b"), "cls"),
]


def variants(text: str, per_pattern: int = 3):
    """(new_text, first changed line) for single-line token edits, comment and blank-line edits, a deleted line and
    an inserted statement, spread over the file."""
    lines = text.split("\n")
    out = []
    for pat, rep in _PATTERNS:
        hits = [i for i, l in enumerate(lines) if pat.search(l)]
        for i in hits[:: max(1, len(hits) // per_pattern)][:per_pattern]:
            new = lines[:]
            new[i] = pat.sub(rep, lines[i], count=1)
            out.append(("\n".join(new), i + 1))
    mid = len(lines) // 2
    for i in (5, mid, len(lines) - 5):
        new = lines[:i] + ["# an added comment"] + lines[i:]
        out.append(("\n".join(new), i + 1))
        new = lines[:i] + [""] + lines[i:]
        out.append(("\n".join(new), i + 1))
        new = lines[:i] + lines[i + 1:]
        out.append(("\n".join(new), i + 1))
        new = lines[:i] + ["_diet_marker = 1"] + lines[i:]
        out.append(("\n".join(new), i + 1))
    return out


def real(path: str) -> str:
    return (PIN / path).read_bytes().decode("utf-8")


def parses(text: str) -> bool:
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


# ---- parse cache ----------------------------------------------------------------------------

def test_parse_returns_the_same_tree_for_the_same_text_and_path():
    a = pyscope.parse("x = 1\n", "a.py")
    assert pyscope.parse("x = 1\n", "a.py") is a
    assert pyscope.parse("x = 1\n", "b.py") is not a
    assert ast.dump(a) == ast.dump(ast.parse("x = 1\n"))


def test_parse_cache_is_bounded_and_keeps_answering():
    pyscope.clear_caches()
    first = pyscope.parse("v0 = 0\n", "p.py")
    for i in range(1, 40):
        pyscope.parse(f"v{i} = {i}\n", "p.py")
    assert len(pyscope._PARSED) <= pyscope._CACHE_MAX
    again = pyscope.parse("v0 = 0\n", "p.py")  # evicted: parsed afresh, equal tree
    assert again is not first and ast.dump(again) == ast.dump(first)


def test_parse_cache_never_hides_a_syntax_error():
    for _ in range(2):
        with pytest.raises(SyntaxError):
            pyscope.parse("def f(:\n", "bad.py")


def test_weakmut_reuses_the_trees_rules_already_parsed(monkeypatch):
    old = "def f(x):\n    return x is None\n"
    new = "def f(x):\n    return x == None\n"
    pyscope.clear_caches()
    pyscope.parse(old, "w.py")
    pyscope.parse(new, "w.py")
    calls = []
    real_parse = ast.parse
    monkeypatch.setattr(ast, "parse", lambda *a, **k: calls.append(a) or real_parse(*a, **k))
    prof = weakmut.Profile(1, {})
    weakmut.drops("w.py", None, old, new, prof)
    assert calls == []


# ---- statements written exactly alike ---------------------------------------------------------

def _first_stmts(text):
    return ast.parse(text).body


def test_stmt_same_sees_identical_lines_at_identical_columns():
    old = "x = 1\ny = 2\n"
    new = "# lead\nx = 1\ny = 3\n"
    same = pyscope.stmt_same(old, new)
    a, b = _first_stmts(old), _first_stmts(new)
    assert same(a[0], b[0]) and not same(a[1], b[1]) and not same(a[0], b[1])


def test_stmt_same_includes_decorators_and_cuts_at_semicolons():
    old = "@deco\ndef f():\n    return 1\n"
    new = "@deco2\ndef f():\n    return 1\n"
    same = pyscope.stmt_same(old, new)
    assert not same(_first_stmts(old)[0], _first_stmts(new)[0])  # the decorator line differs
    old2, new2 = "a = 1; b = 2\n", "a = 1; b = 3\n"
    s2 = pyscope.stmt_same(old2, new2)
    a, b = _first_stmts(old2), _first_stmts(new2)
    assert not s2(a[0], b[0])  # the shared line differs, so neither statement on it is skipped
    assert not s2(a[1], b[1])


def test_stmt_same_gives_up_on_carriage_returns():
    assert pyscope.stmt_same("a = 1\r\n", "a = 2\r\n") is None


# ---- attrrule.find: the same answer without dumping every statement -----------------------------

CLS_OLD = '''
from x import exp

class Dialect:
    FLAG = True

class MySQL(Dialect):
    SAFE = True
    NAMES = {"a": 1}
    Generator = MyGen

    class Tokenizer(Base):
        QUOTES = ["'"]

    def f(self):
        return 1

def g(x):
    return x
'''

CLS_EDITS = [
    ("SAFE = True", "SAFE = False"),
    ('NAMES = {"a": 1}', 'NAMES = {"a": 2}'),
    ("QUOTES = [\"'\"]", "QUOTES = ['\"']"),
    ("class Tokenizer(Base):", "class Tokenizer(Other):"),
    ("class MySQL(Dialect):", "class MySQL(Dialect, Mixin):"),
    ("    SAFE = True\n", "    SAFE = True  # note\n"),
    ("    SAFE = True\n", "    SAFE = True\n    EXTRA = 1\n"),
    ("return 1", "return 2"),
    ("return x", "return not x"),
    ("FLAG = True", "FLAG = False"),
    ("SAFE = True", "SAFE = foo()"),
    ("def g(x):", "def g(x, y=1):"),
]


@pytest.mark.parametrize("a,b", CLS_EDITS)
def test_attr_find_with_new_text_matches_314_on_small_classes(a, b):
    new = CLS_OLD.replace(a, b)
    want = attrrule_314.find(ast.parse(CLS_OLD), ast.parse(new), CLS_OLD)
    assert attrrule.find(ast.parse(CLS_OLD), ast.parse(new), CLS_OLD) == want
    assert attrrule.find(ast.parse(CLS_OLD), ast.parse(new), CLS_OLD, new) == want


@needs_pin
@pytest.mark.parametrize("path,per", REAL_CASES)
def test_attr_find_with_new_text_matches_314_on_real_edits(path, per):
    old = real(path)
    ot = pyscope.parse(old, path)
    ot314 = ast.parse(old, path)
    seen = set()
    for new, line in variants(old, per_pattern=per):
        if not parses(new):
            continue
        want = attrrule_314.find(ot314, ast.parse(new, path), old)
        got = attrrule.find(ot, pyscope.parse(new, path), old, new)
        assert got == want, (path, line)
        seen.add(want is None)
    assert seen  # at least one variant was judged


# ---- weakmut: the same single mutation found without comparing every statement ------------------

def _node_key(n):
    return None if n is None else (type(n).__name__, n.lineno, n.col_offset, n.end_lineno, n.end_col_offset)


@needs_pin
@pytest.mark.parametrize("path,per", REAL_CASES)
def test_weakmut_find_mutation_matches_314_on_real_edits(path, per):
    old = real(path)
    ot = pyscope.parse(old, path)
    ot314 = ast.parse(old, path)
    hits = 0
    for new, line in variants(old, per_pattern=per + 1):
        if not parses(new):
            continue
        nt = pyscope.parse(new, path)
        want = weakmut_314.find_mutation(ot314, ast.parse(new, path))
        got = weakmut.find_mutation(ot, nt, pyscope.stmt_same(old, new))
        assert (got is None) == (want is None), (path, line)
        if want is None:
            continue
        assert _node_key(got[0]) == _node_key(want[0]) and _node_key(got[1]) == _node_key(want[1]), (path, line)
        assert weakmut.classify(ot, *got) == weakmut_314.classify(ot314, *want), (path, line)
        hits += 1
    assert hits


# ---- scope hazards and rebinds: the same spans, with less compiling -----------------------------

SCOPE_OLD = '''
G = 1

def f(a):
    rows = 1
    return rows + G

class C:
    def m(self):
        tmp = 2
        return tmp

def h():
    return G
'''

SCOPE_EDITS = [
    ("rows = 1", "rows = 2"),
    ("rows = 1", "G = 1"),
    ("rows = 1", "yield 1"),
    ("rows = 1", "tmp = 1"),
    ("tmp = 2", "tmp = 2\n        G = 3"),
    ("return G\n", "global G\n    G = 1\n    return G\n"),
    ("return tmp", "return tmp + 1"),
]


@pytest.mark.parametrize("a,b", SCOPE_EDITS)
def test_function_hazards_match_314_on_small_files(a, b):
    new = SCOPE_OLD.replace(a, b)
    want = pyscope_314.function_hazards(SCOPE_OLD, new, ast.parse(SCOPE_OLD), ast.parse(new))
    ot, nt = pyscope.parse(SCOPE_OLD), pyscope.parse(new)
    assert pyscope.function_hazards(SCOPE_OLD, new, ot, nt) == want
    pyscope.clear_caches()
    ot, nt = pyscope.parse(SCOPE_OLD), pyscope.parse(new)
    assert pyscope.compiles(nt)
    assert pyscope.function_hazards(SCOPE_OLD, new, ot, nt) == want


@needs_pin
@pytest.mark.parametrize("path,per", REAL_CASES)
def test_function_hazards_match_314_on_real_edits(path, per):
    old = real(path)
    ot = pyscope.parse(old, path)
    ot314 = ast.parse(old, path)
    for new, line in variants(old, per_pattern=per):
        if not parses(new):
            continue
        nt = pyscope.parse(new, path)
        nt314 = ast.parse(new, path)
        try:
            want = pyscope_314.function_hazards(old, new, ot314, nt314, path)
        except (SyntaxError, ValueError):
            continue
        assert pyscope.compiles(nt, path)
        assert pyscope.function_hazards(old, new, ot, nt, path, str(PIN / path)) == want, (path, line)


def test_compile_of_the_new_text_is_not_repeated(monkeypatch):
    old = "def f():\n    return 1\n"
    new = "def f():\n    return 2\n"
    pyscope.clear_caches()
    ot, nt = pyscope.parse(old, "c.py"), pyscope.parse(new, "c.py")
    calls = []
    real_compile = compile

    def counting(*a, **k):
        calls.append(a[0] if isinstance(a[0], str) else "tree")
        return real_compile(*a, **k)

    monkeypatch.setattr(pyscope, "compile", counting, raising=False)
    assert pyscope.compiles(nt, "c.py")
    pyscope.function_hazards(old, new, ot, nt, "c.py")
    assert calls.count("tree") == 1 and calls.count(new) == 0  # the old text is compiled once, the new tree once
    calls.clear()
    pyscope.function_hazards(old, new, ot, nt, "c.py")
    assert calls == []  # both tables are cached


@needs_pin
def test_scope_tables_of_the_pyc_equal_the_tables_of_a_compile():
    checked = 0
    for p in sorted((PIN / "sqlglot").rglob("*.py")):
        text = p.read_bytes().decode("utf-8")
        code = pyscope._pyc_code(str(p), text)
        if code is None:
            continue  # no usable pyc for this interpreter (they are made in the image)
        rel = str(p.relative_to(PIN)).replace("\\", "/")
        a = pyscope._scopes_of(code)
        b = pyscope._scopes_of(compile(text, rel, "exec", dont_inherit=True))
        assert a.keys() == b.keys(), rel
        for k in a:
            assert a[k][:4] == b[k][:4] and pyscope._span(a[k][4]) == pyscope._span(b[k][4]), (rel, k)
        checked += 1
    if checked == 0:
        pytest.skip("no .pyc beside the pinned checkout (the image compiles them)")


@needs_pin
def test_scope_tables_of_a_tree_compile_equal_the_tables_of_a_text_compile():
    for path in REAL_FILES:
        text = real(path)
        tree = ast.parse(text, path)
        a = pyscope._scopes_of(compile(tree, path, "exec", dont_inherit=True))
        b = pyscope._scopes_of(compile(text, path, "exec", dont_inherit=True))
        assert a.keys() == b.keys()
        for k in a:
            assert a[k][:4] == b[k][:4] and pyscope._span(a[k][4]) == pyscope._span(b[k][4]), (path, k)


def test_a_stale_pyc_is_not_used(tmp_path):
    src = tmp_path / "m.py"
    src.write_text("def f():\n    return 1\n")
    import py_compile

    py_compile.compile(str(src), doraise=True)
    text = src.read_text()
    assert pyscope._pyc_code(str(src), text) is not None
    assert pyscope._pyc_code(str(src), text + "# longer\n") is None  # a different text than the file
    src.write_text("def f():\n    return 2222\n")  # the file moved on: size differs from the .pyc header
    assert pyscope._pyc_code(str(src), src.read_text()) is None


# ---- weakmut: classify and writes_shared_state without walking the whole file --------------------

NESTED = '''
import os

G = {}


def top(a, b=lambda q: q is None or q == 1):
    @cached
    def inner(x):
        return not x

    class K:
        flag = a is None and b
        def meth(self, y):
            G[y] = 1
            return y < 3 or self.z is not None

    for i in range(3):
        v = [t for t in a if t is not None and t > 1]
    return inner(v) if a == b else None


class Outer(Base, metaclass=Meta):
    attr = 1 if x is y else 2

    @deco(arg=1 is 1)
    def method(self, p: int = 3 if q else 4) -> bool:
        local = []
        local.append(p == 1)
        return not local and self is None


async def co(z):
    return await z or not z
'''


def _nodes_with_positions(tree, every=1):
    out = [n for n in ast.walk(tree) if hasattr(n, "lineno") and hasattr(n, "end_lineno")]
    return out[::every]


@pytest.mark.parametrize("text", [NESTED])
def test_site_kind_and_enclosing_function_match_314_on_every_node_of_a_nested_file(text):
    t314 = ast.parse(text)
    sites = weakmut_314.collect(t314)
    tree = ast.parse(text)
    for a, a314 in zip(_nodes_with_positions(tree), _nodes_with_positions(t314)):
        assert weakmut.site_kind(tree, a) == sites.get(id(a314)), ast.dump(a)
        assert weakmut.writes_shared_state(tree, a) == weakmut_314.writes_shared_state(t314, a314), ast.dump(a)


@needs_pin
@pytest.mark.parametrize("path", REAL_FILES)
def test_site_kind_and_enclosing_function_match_314_on_sampled_nodes_of_real_files(path):
    text = real(path)
    t314 = ast.parse(text, path)
    sites = weakmut_314.collect(t314)
    tree = ast.parse(text, path)
    nodes, nodes314 = _nodes_with_positions(tree), _nodes_with_positions(t314)
    assert len(nodes) == len(nodes314)
    step = max(1, len(nodes) // 400)
    picks = set(range(0, len(nodes), step))
    picks.update([i for i, n in enumerate(nodes) if isinstance(n, (ast.Compare, ast.BoolOp, ast.UnaryOp))][:300])
    for i in sorted(picks):
        a, a314 = nodes[i], nodes314[i]
        want = sites.get(id(a314))
        assert weakmut.site_kind(tree, a) == want, (path, a.lineno)
        if want is not None:
            assert weakmut.writes_shared_state(tree, a) == weakmut_314.writes_shared_state(t314, a314), (path, a.lineno)


def test_a_target_that_is_not_in_the_tree_falls_back_to_the_full_walk():
    tree = ast.parse("def f(x):\n    return x is None\n")
    other = ast.parse("def f(x):\n    return x is None\n")
    stranger = other.body[0].body[0].value  # same shape and lines, a different tree
    assert weakmut.site_kind(tree, stranger) is None
    assert weakmut.writes_shared_state(tree, stranger) is True  # not inside a plain function of this tree: shared


def test_a_target_under_a_very_deep_expression_still_gets_the_answer_collect_gives():
    src = "def f(b):\n    return " + " ** ".join(["a"] * 400) + " ** (b is None)\n"
    tree = ast.parse(src)
    target = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)][0]
    assert weakmut.site_kind(tree, target) == "C" == weakmut_314.collect(tree).get(id(target))
    assert weakmut.writes_shared_state(tree, target) is weakmut_314.writes_shared_state(tree, target)
