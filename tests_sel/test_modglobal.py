"""G: constants inside a module-level pure literal that only def bodies of the same module read (selector/modglobal.py).

Such an edit used to take the module-level rule (every test that touches the file, 1587 tests for the unit tables of
sqlglot/generators/hive.py). It now gets what an edit of each reading statement gets. The unit tests need no pinned
sqlglot; the real-source tests need BR_SRC."""

import ast
import re
import sys

import pytest

from conftest import PIN, ROOT, make_patch, needs_pin

sys.path.insert(0, str(ROOT))
import modglobal  # noqa: E402
import pyscope  # noqa: E402
import rules  # noqa: E402

PATH = "sqlglot/thing.py"
SRC = '''\
UNITS = ("YEAR", "QUARTER", "MONTH")
FACTOR = {
    "SECOND": "",
    "MINUTE": " / 60",
}
LIMIT = 3


def diff(unit):
    months = unit in UNITS
    return months


def scale(unit):
    f = FACTOR.get(unit)
    return f if LIMIT > 2 else None
'''
# 1 UNITS, 2-5 FACTOR, 6 LIMIT, 10 reads UNITS, 15 reads FACTOR, 16 reads LIMIT


def find(old, new, path=PATH):
    old_tree, new_tree = pyscope.parse(old, path), pyscope.parse(new, path)
    ol, nl = old.split("\n"), new.split("\n")
    assert len(ol) == len(nl)
    ranges = [(i + 1, i + 1) for i, (a, b) in enumerate(zip(ol, nl)) if a != b]
    return modglobal.find(old_tree, new_tree, pyscope.ast_diff(old_tree, new_tree, ranges), old)


# ---- find: what qualifies ---------------------------------------------------------------------------

@pytest.mark.parametrize("old,new,want", [
    ('"QUARTER"', '"QUARTEX"', ("UNITS", [10])),            # an element of a tuple
    ('"SECOND": ""', '"SECOND2": ""', ("FACTOR", [15])),    # a dict key
    ('" / 60"', '" / 61"', ("FACTOR", [15])),               # a dict value
    ("LIMIT = 3", "LIMIT = 4", ("LIMIT", [16])),            # a bare number
])
def test_constant_edit_of_a_private_literal_is_found(old, new, want):
    assert old in SRC
    assert find(SRC, SRC.replace(old, new, 1)) == want


def test_every_read_is_reported():
    src = SRC + "\n\ndef again(unit):\n    return unit not in UNITS\n"
    assert find(src, src.replace('"YEAR"', '"YEAS"', 1)) == ("UNITS", [10, 20])


def test_annotated_assignment_and_set_and_signed_number():
    src = 'CODES: set = {"a", ("b", -1)}\n\n\ndef f(x):\n    return x in CODES\n'
    assert find(src, src.replace("-1", "-2")) == ("CODES", [5])
    assert find(src, src.replace('"a"', '"z"')) == ("CODES", [5])


def test_read_inside_a_nested_def_or_lambda_of_a_def_body_counts_as_a_read():
    src = 'UNITS = ("A", "B")\n\n\ndef f(xs):\n    g = lambda x: x in UNITS\n    return [g(x) for x in xs]\n'
    assert find(src, src.replace('"A"', '"Z"')) == ("UNITS", [5])


# ---- find: what keeps the older rule ----------------------------------------------------------------

def _none(src, old='"YEAR"', new='"YEAS"'):
    assert old in src
    assert find(src, src.replace(old, new, 1)) is None


@pytest.mark.parametrize("extra", [
    "\nALSO = UNITS\n",                                         # read while the module body runs
    "\n\nclass K:\n    U = UNITS\n",                            # read while a class body runs
    "\n\ndef h(u=UNITS):\n    return u\n",                      # read by a default value (import time)
    "\n\ndef deco(x):\n    return lambda f: f\n\n\n@deco(UNITS)\ndef h():\n    return 1\n",  # read by a decorator
    "\nF = lambda u: u in UNITS\n",                             # a module-level lambda
    "\nX = [u for u in UNITS]\n",                               # a module-level comprehension
    "\n\ndef h() -> UNITS:\n    return 1\n",                    # an annotation
])
def test_a_read_outside_def_bodies_keeps_the_older_rule(extra):
    _none(SRC + extra)


@pytest.mark.parametrize("extra", [
    '\nUNITS = ("X",)\n',                                       # bound twice
    "\nUNITS += ('X',)\n",
    "\n\ndef h():\n    global UNITS\n    UNITS = ()\n",
    "\n\ndef h(UNITS):\n    return UNITS\n",                    # a parameter of that name
    "\n\ndef h():\n    for UNITS in ():\n        pass\n",
    "\n\ndef h():\n    del UNITS\n",
    "\nimport os as UNITS\n",
    "\nfrom m import UNITS\n",
    "\n\ndef UNITS():\n    return 1\n",
    "\n\nclass UNITS:\n    pass\n",
    "\n\ndef h(m):\n    return m.UNITS\n",                      # an attribute of that name
    '\n__all__ = ["UNITS"]\n',                                  # the name in a string
    '\n\ndef h(m):\n    return getattr(m, "UNITS")\n',
    "\n\ndef h():\n    try:\n        pass\n    except Exception as UNITS:\n        pass\n",
    "\n\ndef h():\n    return globals()\n",                     # the namespace reached without the name
    "\n\ndef h(m):\n    return vars(m)\n",
    "\n\ndef h(m):\n    return m.__dict__\n",
    "\n\ndef h():\n    return eval('1')\n",
])
def test_another_binding_or_a_dynamic_reach_keeps_the_older_rule(extra):
    _none(SRC + extra)


def test_a_dunder_name_keeps_the_older_rule():
    # `from m import *` reads __all__ without spelling it anywhere; the same goes for __doc__ and doctest
    src = '__all__ = ["a", "b"]\na = b = 1\n\n\ndef names():\n    return __all__\n'
    assert find(src, src.replace('"b"]', '"c"]')) is None
    ok = src.replace("__all__", "EXPORTS")
    assert find(ok, ok.replace('"b"]', '"c"]')) == ("EXPORTS", [6])


@pytest.mark.parametrize("extra", [
    "\n\ndef h(f):\n    return f.__globals__\n",
    "\n\ndef h(fr):\n    return fr.f_globals\n",
    "\nimport sys\n\n\ndef h():\n    return sys._getframe()\n",
    "\nimport inspect\n\n\ndef h():\n    return inspect.currentframe()\n",
])
def test_a_frame_or_function_namespace_reach_keeps_the_older_rule(extra):
    _none(SRC + extra)


def test_a_literal_nobody_reads_keeps_the_older_rule():
    src = 'UNITS = ("YEAR",)\n\n\ndef f():\n    return 1\n'
    _none(src)


@pytest.mark.parametrize("stmt", [
    'UNITS = tuple(["YEAR"])',          # a call
    'UNITS = ("YEAR", OTHER)',          # a name
    'UNITS = ("YEAR", *REST)',          # a spread
    'UNITS = {**BASE, "k": "YEAR"}',    # a dict spread
    'UNITS = ("YEAR", f"{1}")',         # an f-string
    'UNITS = "YEAR" + "S"',             # arithmetic
    'UNITS = ["YEAR" for _ in ()]',     # a comprehension
    'UNITS = {["x"]: "YEAR"}',          # an unhashable key (would raise)
])
def test_a_value_that_is_not_a_pure_literal_keeps_the_older_rule(stmt):
    _none(stmt + "\nOTHER = 1\nREST = ()\nBASE = {}\n\n\ndef f(u):\n    return u in UNITS\n")


def test_a_statement_that_is_not_in_the_module_body_keeps_the_older_rule():
    _none('if True:\n    UNITS = ("YEAR",)\n\n\ndef f(u):\n    return u in UNITS\n')
    _none('class K:\n    UNITS = ("YEAR",)\n\n    def f(self, u):\n        return u in UNITS\n')
    _none('def f(u):\n    UNITS = ("YEAR",)\n    return u in UNITS\n')


def test_an_edit_of_two_statements_or_of_anything_else_keeps_the_older_rule():
    new = SRC.replace('"YEAR"', '"YEAS"').replace("LIMIT = 3", "LIMIT = 4")
    assert find(SRC, new) is None
    assert find(SRC, SRC.replace("LIMIT > 2", "LIMIT > 3")) is None  # a constant of a function body
    assert find(SRC, SRC.replace('("YEAR", "QUARTER", "MONTH")', '["YEAR", "QUARTER", "MONTH"]')) is None  # shape
    assert find(SRC, SRC.replace("LIMIT = 3", "LIMIT = None")) == ("LIMIT", [16])  # still a constant


# ---- reached_elsewhere ------------------------------------------------------------------------------

def _tree(tmp_path, files):
    for rel, text in {"sqlglot/thing.py": SRC, **files}.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_no_other_file_spells_the_name(tmp_path):
    root = _tree(tmp_path, {"sqlglot/other.py": "X = 1\n", "tests/test_x.py": "def test():\n    assert 1\n"})
    assert modglobal.reached_elsewhere(root, PATH, "UNITS") is False


def test_a_file_with_its_own_binding_of_the_same_name_is_harmless(tmp_path):
    root = _tree(tmp_path, {"sqlglot/tsql.py": 'UNITS = {"a": 1}\n\n\ndef f(u):\n    return UNITS.get(u)\n'})
    assert modglobal.reached_elsewhere(root, PATH, "UNITS") is False


@pytest.mark.parametrize("text", [
    "from sqlglot.thing import UNITS\n",
    "from sqlglot.thing import UNITS as U\n",
    "from sqlglot import thing\n\n\ndef f(u):\n    return u in thing.UNITS\n",
    'import sqlglot.thing as t\nX = getattr(t, "UNITS")\n',
    "def f(u):\n    return u in UNITS\n",                               # spelled, not bound here: whose is it?
    'from sqlglot.other import *\nUNITS = ("x",)\n',                    # a star import can rebind it
    'UNITS = ("x",)\n\n\ndef f(m):\n    return m.UNITS\n',              # own binding, but also an attribute read
    'UNITS = ("x",)\n\n\ndef f(m):\n    return getattr(m, "UNITS")\n',  # own binding, but also the name in a string
    'UNITS = ("x",)\n\n\ndef f():\n    global UNITS\n',                 # own binding, but also a global statement
    'UNITS = ("x",)\nfrom sqlglot.thing import UNITS\n',                # own binding, then the import wins
    "# UNITS\n",                                                        # only a comment: no own binding, no proof
])
def test_a_file_that_may_reach_the_name_keeps_the_older_rule(tmp_path, text):
    root = _tree(tmp_path, {"tests/test_x.py": text})
    assert modglobal.reached_elsewhere(root, PATH, "UNITS") is True


def test_a_longer_word_is_not_the_name(tmp_path):
    root = _tree(tmp_path, {"sqlglot/other.py": "from sqlglot.x import TIME_UNITS, UNITS_2\n"})
    assert modglobal.reached_elsewhere(root, PATH, "UNITS") is False


def test_doubt_keeps_the_older_rule(tmp_path):
    root = _tree(tmp_path, {"sqlglot/broken.py": "UNITS = (\n"})          # does not parse
    assert modglobal.reached_elsewhere(root, PATH, "UNITS") is True
    assert modglobal.reached_elsewhere(tmp_path / "nowhere", PATH, "UNITS") is True  # the edited file was never seen
    root2 = _tree(tmp_path / "b", {"sqlglot/ext.pyx": "UNITS = 1\n"})     # a non-Python source spells it (even as its own)
    assert modglobal.reached_elsewhere(root2, PATH, "UNITS") is True


# ---- rules._private_global on a stub of the maps ----------------------------------------------------

TESTS = [{"number": n, "id": f"t{n}", "file": "tests/t.py", "seconds": 0.1} for n in range(8)]


class Stub:
    """Just the tables _private_global and _func_level read. Statements: (start, end, kind, def start, def end, class)."""

    def __init__(self, lines=None, il=(), un=(), stmts=None):
        self.lines = {PATH: dict(lines if lines is not None else {10: 1, 15: 2, 16: 2})}
        self.sets = [0, 0b0011, 0b1100]
        self.importtime = {PATH: set(il)}
        self.unstable = {PATH: set(un)}
        self.imports = {PATH: 0b11111111}
        self.srcreaders = {}
        self.files = {}
        self._stmts = stmts if stmts is not None else {
            10: (10, 10, "function", 9, 11, ""), 15: (15, 15, "function", 14, 16, ""), 16: (16, 16, "function", 14, 16, "")}
        self.stmts = {PATH: True}

    def stmt(self, file, line):
        return self._stmts.get(line)

    def stmt_mask(self, file, line):
        s = self.stmt(file, line)
        u = 0
        for ln in range(s[0], s[1] + 1):
            u |= self.sets[self.lines[file].get(ln, 0)]
        return u


def run(old, new, maps=None, tmp_path=None, single=True, files=None):
    maps = maps or Stub()
    root = _tree(tmp_path, files or {})
    ctx = rules.Ctx(maps, TESTS, str(root))
    ctx.single = single
    old_tree, new_tree = pyscope.parse(old, PATH), pyscope.parse(new, PATH)
    ol, nl = old.split("\n"), new.split("\n")
    ranges = [(i + 1, i + 1) for i, (a, b) in enumerate(zip(ol, nl)) if a != b]
    return rules._private_global(ctx, PATH, pyscope.ast_diff(old_tree, new_tree, ranges), old_tree, new_tree, old)


NEW_UNITS = SRC.replace('"YEAR"', '"YEAS"', 1)
NEW_FACTOR = SRC.replace('"SECOND": ""', '"SECOND2": ""', 1)


def test_selects_the_tests_that_ran_the_reading_statement(tmp_path):
    assert run(SRC, NEW_UNITS, tmp_path=tmp_path) == 0b0011
    assert run(SRC, NEW_FACTOR, tmp_path=tmp_path) == 0b1100


def test_a_read_that_ran_at_import_selects_every_importer(tmp_path):
    assert run(SRC, NEW_UNITS, Stub(il={10}), tmp_path=tmp_path) == 0b11111111


def test_an_unstable_read_selects_everyone_who_ran_the_def(tmp_path):
    maps = Stub(lines={10: 1, 11: 2, 15: 2, 16: 2}, un={10})
    assert run(SRC, NEW_UNITS, maps, tmp_path=tmp_path) == 0b1111


def test_a_read_no_test_ran_keeps_the_older_rule(tmp_path):
    assert run(SRC, NEW_UNITS, Stub(lines={15: 2, 16: 2}), tmp_path=tmp_path) is None


def test_a_patch_of_several_files_keeps_the_older_rule(tmp_path):
    assert run(SRC, NEW_UNITS, tmp_path=tmp_path, single=False) is None


def test_a_file_without_line_data_or_statement_table_keeps_the_older_rule(tmp_path):
    assert run(SRC, NEW_UNITS, Stub(lines={}), tmp_path=tmp_path) is None
    maps = Stub()
    maps.stmts = {}
    assert run(SRC, NEW_UNITS, maps, tmp_path=tmp_path) is None


def test_a_read_the_statement_table_does_not_place_in_a_function_keeps_the_older_rule(tmp_path):
    assert run(SRC, NEW_UNITS, Stub(stmts={10: (10, 10, "module", 0, 0, "")}), tmp_path=tmp_path) is None
    assert run(SRC, NEW_UNITS, Stub(stmts={}), tmp_path=tmp_path) is None


def test_another_file_that_reaches_the_name_keeps_the_older_rule(tmp_path):
    files = {"sqlglot/spark.py": "from sqlglot.thing import UNITS\n"}
    assert run(SRC, NEW_UNITS, tmp_path=tmp_path, files=files) is None


# ---- real source ------------------------------------------------------------------------------------

HIVE = "sqlglot/generators/hive.py"


def _select(rel, old, new):
    from maps.load import load
    import diffparse
    import json

    text = (PIN / rel).read_text(encoding="utf-8")
    assert text.count(old) == 1, (rel, old)
    tests = json.loads((ROOT.parent / "kits" / "rwx" / "x" / "sample" / "tests.json").read_text())["tests"] \
        if (ROOT.parent / "kits" / "rwx" / "x" / "sample" / "tests.json").is_file() else None
    if tests is None:
        pytest.skip("kit tests.json not found")
    changes = diffparse.parse(make_patch(rel, text, text.replace(old, new)))
    got, why = rules.select(changes, load(), tests, str(PIN))
    return {tests[n]["id"].split("::", 1)[1] for n in got}, why


@needs_pin
@pytest.mark.parametrize("old,new", [
    ('    "MILLISECOND": " * 1000",', '    "MILLISECONDS": " * 1000",'),     # a key of TIME_DIFF_FACTOR
    ('DIFF_MONTH_SWITCH = ("YEAR", "QUARTER", "MONTH")', 'DIFF_MONTH_SWITCH = ("YEAR", "QUARTEX", "MONTH")'),
])
def test_hive_unit_tables_select_the_tests_that_ran_date_diff(old, new):
    got, why = _select(HIVE, old, new)
    assert " G " in " " + why or why.startswith("G "), why
    assert "TestHive::test_time" in got
    assert len(got) < 20, sorted(got)


@needs_pin
def test_hive_key_edit_selects_what_an_edit_of_the_reading_lines_selects():
    key, _ = _select(HIVE, '    "MILLISECOND": " * 1000",', '    "MILLISECONDS": " * 1000",')
    line, _ = _select(HIVE, "    factor = TIME_DIFF_FACTOR.get(unit)", "    factor = TIME_DIFF_FACTOR.get(unit, None)")
    assert key == line


@needs_pin
def test_a_constant_that_another_module_imports_is_not_narrowed():
    # sqlglot/generators/spark2.py imports HIVE_DATE_FORMAT: the module-level rule stays
    _, why = _select(HIVE, "HIVE_DATE_FORMAT = \"'yyyy-MM-dd'\"", "HIVE_DATE_FORMAT = \"'yyyy-MM-da'\"")
    assert not why.startswith("G "), why


# Namespace walkers: code that reads a module's attributes without spelling their names. Each one in the pinned
# sqlglot/ and tests/ was read; all of them keep only classes or functions, so plain data bound at module level is
# never looked at: Dialect._try_load (isinstance(attr, type) and issubclass), helper.subclasses (inspect.isclass),
# tests/test_docs.py (doctest collects docstrings of functions and classes). A new walker fails this test.
_WALK = re.compile(r"getmembers\(|(?<![A-Za-z0-9_.])vars\(|(?<![A-Za-z0-9_.])dir\((?!cls\)|Gen\))|walk_packages|DocTestSuite")
_KNOWN = {
    ("sqlglot/dialects/dialect.py", "for attr_name in dir(module):"),
    ("sqlglot/helper.py", "for _, obj in inspect.getmembers("),
    ("sqlglot/helper.py", "**{k: v.copy() if hasattr(v, \"copy\") else copy(v) for k, v in vars(obj).items()},"),
    ("tests/test_docs.py", "for info in pkgutil.walk_packages(sqlglot.__path__, prefix=\"sqlglot.\"):"),
    ("tests/test_docs.py", "tests.addTests(doctest.DocTestSuite(module))"),
}


@needs_pin
def test_pin_has_no_namespace_walkers():
    seen = set()
    for p in sorted(PIN.rglob("*.py")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if _WALK.search(line):
                seen.add((p.relative_to(PIN).as_posix(), line.strip()))
    assert seen == _KNOWN, sorted(seen ^ _KNOWN)


# Attribute access by a computed name on something an import statement bound (a module, or a name taken from one).
# These are all there are in the whole checkout, and none can hand out plain data bound at module level:
#   dialects/__init__.py   module __getattr__: `name` is a key of its own table of dialect names (spelled there)
#   dialects/dialect.py    the dir(module) walk of _try_load: keeps subclasses of Dialect only
#   optimizer/__init__.py  module __getattr__: `attr` comes from its own table of rule names (spelled there)
#   serde.py               getattr(module, class_name)(): the result is called, a tuple or a dict would raise
_DYN_KNOWN = {
    ("sqlglot/dialects/__init__.py", "getattr", "module", "name"),
    ("sqlglot/dialects/dialect.py", "getattr", "module", "attr_name"),
    ("sqlglot/optimizer/__init__.py", "getattr", "importlib.import_module(module_name)", "attr"),
    ("sqlglot/serde.py", "getattr", "module", "class_name"),
}


@needs_pin
def test_pin_dynamic_attribute_access_on_imported_names_is_known():
    seen = set()
    for p in sorted(PIN.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        bound = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                bound.update(a.asname or a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                bound.update(a.asname or a.name for a in n.names)
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "setattr", "delattr", "hasattr")):
                continue
            if len(n.args) < 2 or isinstance(n.args[1], ast.Constant):
                continue  # a literal name is a string constant: reached_elsewhere and find look at those
            base = n.args[0]
            text = ast.unparse(base)
            while isinstance(base, (ast.Attribute, ast.Subscript, ast.Call)):
                base = base.func if isinstance(base, ast.Call) else base.value
            if (isinstance(base, ast.Name) and base.id in bound) or "module" in text or "import" in text:
                seen.add((p.relative_to(PIN).as_posix(), n.func.id, text, ast.unparse(n.args[1])))
    assert seen == _DYN_KNOWN, sorted(seen ^ _DYN_KNOWN)


def _eligible():
    """(file, NAME) of every module-body pure literal of sqlglot/ that find() accepts for an edit of one of its constants."""
    out = []
    for p in sorted((PIN / "sqlglot").rglob("*.py")):
        rel = p.relative_to(PIN).as_posix()
        text = p.read_text(encoding="utf-8")
        tree = pyscope.parse(text, rel)
        for st in tree.body:
            t = modglobal._target(st)
            if t is None or not modglobal.pure(t[1]):
                continue
            consts = [c for c in ast.walk(t[1]) if isinstance(c, ast.Constant)]
            if consts and modglobal.find(tree, tree, [(consts[0], None)], text):
                out.append((rel, t[0].id))
    return out


def _index():
    """Per .py file of the whole checkout: every identifier the parser sees (normalized, whatever the spelling), every
    string constant (escapes resolved and adjacent literals joined), the names its module body binds, star imports."""
    idx = {}
    for p in sorted(PIN.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        ids, strs, attrs, imported, glob = set(), [], set(), set(), set()
        star = False
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                ids.add(n.id)
            elif isinstance(n, ast.Attribute):
                ids.add(n.attr)
                attrs.add(n.attr)
            elif isinstance(n, ast.alias):
                star |= n.name == "*"
                for x in (n.asname, n.name, *n.name.split(".")):
                    if x:
                        ids.add(x)
                        imported.add(x)
            elif isinstance(n, ast.arg):
                ids.add(n.arg)
            elif isinstance(n, ast.keyword):
                if n.arg:
                    ids.add(n.arg)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                ids.update(n.names)
                glob.update(n.names)
            elif isinstance(n, ast.Constant):
                v = n.value
                if isinstance(v, bytes):
                    v = v.decode("latin-1")
                if isinstance(v, str):
                    strs.append(v)
            for f in ("name", "rest"):
                x = getattr(n, f, None)
                if isinstance(x, str):
                    ids.add(x)
        bound = {t[0].id for t in map(modglobal._target, tree.body) if t is not None}
        idx[p.relative_to(PIN).as_posix()] = (ids, strs, attrs, imported, glob, bound, star)
    return idx


@needs_pin
def test_the_byte_scan_agrees_with_a_full_parse_for_every_name_the_rule_can_fire_on():
    """reached_elsewhere skips a file whose bytes do not hold the name. A name can also be spelled through an escape in
    a string, through adjacent string literals ("UN" "ITS") or with a compatibility character the parser normalizes.
    Every other file of a patch G accepts is the pinned file, and the names G can fire on are fixed by the pinned tree,
    so all of them are checked here once, exhaustively: wherever a full parse of the whole checkout finds a file that
    may reach the name, the byte scan must have said so too."""
    names = _eligible()
    assert ("sqlglot/generators/hive.py", "TIME_DIFF_FACTOR") in names
    assert ("sqlglot/generators/hive.py", "DIFF_MONTH_SWITCH") in names
    assert ("sqlglot/generators/hive.py", "HIVE_DATE_FORMAT") in names  # find() alone accepts it; spark2.py imports it
    assert modglobal.reached_elsewhere(PIN, "sqlglot/generators/hive.py", "HIVE_DATE_FORMAT") is True
    assert modglobal.reached_elsewhere(PIN, "sqlglot/generators/hive.py", "DATE_DELTA_INTERVAL") is False  # parsers/tsql.py has its own
    idx = _index()
    for rel, name in names:
        word = modglobal._word(name)
        full = False
        for other, (ids, strs, attrs, imported, glob, bound, star) in idx.items():
            if other == rel:
                continue
            in_str = any(word.search(x) for x in strs)
            if name not in ids and not in_str:
                continue
            if name not in bound or star or name in imported or name in attrs or name in glob or in_str:
                full = True
        cheap = modglobal.reached_elsewhere(PIN, rel, name)
        assert cheap or not full, (rel, name)
