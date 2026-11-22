"""R2: an edit of a function that ran while a module imported used to widen to every importer of the edited module
(`ctx.wide(path)`: 1617 tests for Placeholder.name). The tests that can depend on such a run are the importers of the
module bodies that ran the function (maps/impfn.bin: sqlglot and test modules, collection included, three seeds), plus the
tests that ran the lines, plus what the import probe finds. Anything the map or the probe cannot answer keeps the wide answer.

Unit tests use a stub of the maps. The real-source tests need BR_SRC and the kit."""

import ast
import json
import marshal
import os
import sys
import zlib

import pytest

from conftest import KIT, PIN, ROOT, make_patch, needs_kit, needs_pin

sys.path.insert(0, str(ROOT))
import pyscope  # noqa: E402
import rules  # noqa: E402

@pytest.fixture(autouse=True)
def _r2_on(monkeypatch):
    """These tests are about what R2 does when it is on; the shipped default is off (rules.R2_ENABLED)."""
    monkeypatch.setattr(rules, "R2_ENABLED", True)


PATH = "sqlglot/thing.py"
M1, M2, M3 = "sqlglot/generators/a.py", "sqlglot/generators/b.py", "sqlglot/generators/c.py"
LAZY = "tests/dialects/test_dialect.py::TestDialect::test_lazy_load"
TESTS = [{"number": n, "id": f"t{n}", "file": "tests/t.py", "seconds": 0.1} for n in range(14)] + [
    {"number": 14, "id": LAZY, "file": "tests/dialects/test_dialect.py", "seconds": 0.1}
]
WIDE = 0xFFF  # the importers of PATH
S = (7, 7, "function", 5, 9, "")  # the statement on line 7, in the function on lines 5-9


class Stub:
    def __init__(self, ranges=None, base=(), il=(7,), un=(), imports=None):
        self.importtime = {PATH: set(il)}
        self.unstable = {PATH: set(un)}
        self.lines = {PATH: {7: 1}}
        self.sets = [0, 0b1]
        self.imports = imports if imports is not None else {PATH: WIDE, M1: 0b1100, M2: 0b110000, M3: 1 << 9}
        self.srcreaders = {}
        self.files = {}
        self.impfn = None if ranges is None else (ranges, frozenset(base))

    def stmt_mask(self, path, line):
        return self.sets[self.lines[path].get(line, 0)]


TEXT = "\n".join(["# 1", "# 2", "# 3", "# 4", "def f(x):", "    a = x", '    return a or "?"', "    # 8", "    # 9", ""])  # f: lines 5-7


def run(maps, probe=0, s=S, text=TEXT):
    ctx = rules.Ctx(maps, TESTS, "/nonexistent")
    ctx.old_tree = pyscope.parse(text, PATH) if text is not None else None
    ctx._probe = False if probe is None else {"mask": probe}  # False: the probe could not run
    return rules._func_level(ctx, PATH, s)


def test_import_time_function_selects_the_importers_of_the_bodies_that_ran_it():
    assert run(Stub({PATH: [(5, 9, [M1])]})) == 0b1 | 0b1100


def test_no_map_keeps_the_wide_answer():
    assert run(Stub(None)) == WIDE


def test_unavailable_probe_keeps_the_wide_answer():
    assert run(Stub({PATH: [(5, 9, [M1])]}), probe=None) == WIDE


def test_the_probe_verdict_is_added():
    assert run(Stub({PATH: [(5, 9, [M1])]}), probe=1 << 12) == 0b1 | 0b1100 | 1 << 12


def test_function_without_a_recorded_range_keeps_the_wide_answer():
    assert run(Stub({PATH: [(20, 30, [M1])]})) == WIDE
    assert run(Stub({"sqlglot/other.py": [(5, 9, [M1])]})) == WIDE


def test_a_call_made_by_collection_itself_keeps_the_wide_answer():
    assert run(Stub({PATH: [(5, 9, [M1, "<nomod>"])]})) == WIDE


def test_a_module_the_import_map_does_not_know_keeps_the_wide_answer():
    # a test module, or anything not in imports.bin: who imports it is not known
    assert run(Stub({PATH: [(5, 9, [M1, "tests/helpers.py"])]})) == WIDE


def test_nested_function_ranges_inside_the_function_are_included():
    # a closure the function built ran under another module body: that module saw the edited function's state too
    got = run(Stub({PATH: [(5, 9, [M1]), (8, 8, [M2]), (20, 30, [M3])]}))
    assert got == 0b1 | 0b1100 | 0b110000


def test_a_function_ranged_in_a_base_module_adds_test_lazy_load():
    # `import sqlglot` loads PATH: test_lazy_load runs it in a subprocess, no cold import mask lists that
    assert run(Stub({PATH: [(5, 9, [M1])]}, base=[PATH])) == 0b1 | 0b1100 | 1 << 14
    assert run(Stub({PATH: [(5, 9, [M1])]}, base=[M1])) == 0b1 | 0b1100 | 1 << 14
    assert run(Stub({PATH: [(5, 9, [M1])]}, base=["sqlglot/else.py"])) == 0b1 | 0b1100


def test_importers_that_cover_the_wide_answer_skip_the_probe():
    # nothing is gained over `wide`, so the probe's CPU is not spent
    ctx = rules.Ctx(Stub({PATH: [(5, 9, [M1])]}, imports={PATH: WIDE, M1: WIDE}), TESTS, "/nonexistent")
    ctx.old_tree = pyscope.parse(TEXT, PATH)
    ctx.probe_mask = lambda: pytest.fail("the probe was asked")
    assert rules._func_level(ctx, PATH, S) == WIDE


def test_a_cached_function_keeps_the_wide_answer():
    text = "# 1\n# 2\n# 3\n@functools.lru_cache(maxsize=None)\ndef f(x):\n    a = x\n    return a or '?'\n"
    assert run(Stub({PATH: [(4, 7, [M1])]}), text=text) == WIDE
    assert run(Stub({PATH: [(4, 7, [M1])]}), text=text.replace("@functools.lru_cache(maxsize=None)", "@staticmethod")) == 0b1 | 0b1100


def test_without_the_parsed_old_file_the_wide_answer_stays():
    assert run(Stub({PATH: [(5, 9, [M1])]}), text=None) == WIDE


def test_a_line_that_did_not_run_at_import_is_untouched():
    assert run(Stub({PATH: [(5, 9, [M1])]}, il=())) == 0b1


# ---- the loader --------------------------------------------------------------------------------------

def _write(d, **over):
    doc = {"v": 1, "py": sys.version, "seeds": [0, 1, 2], "collect": True, "base": ["sqlglot/__init__.py"],
           "range": {PATH: [(5, 9, [M1])]}}
    doc.update(over)
    (d / "impfn.bin").write_bytes(zlib.compress(marshal.dumps(doc)))


def _maps(d):
    from maps.load import Maps

    m = Maps.__new__(Maps)
    m.dir = str(d)
    return m


def test_loader_accepts_a_map_this_interpreter_recorded(tmp_path):
    _write(tmp_path)
    got = _maps(tmp_path).impfn
    assert got is not None and got[0][PATH] == [(5, 9, [M1])] and "sqlglot/__init__.py" in got[1]


@pytest.mark.parametrize("over", [
    {"py": "3.12.0 (main) [GCC]"},  # recorded by another interpreter: the answers need not hold here
    {"collect": False},  # without the collection pass a test module's call at import is invisible
    {"seeds": [0, 1]},  # fewer than three hash seeds
    {"v": 2},
])
def test_loader_refuses_an_unfit_map(tmp_path, over):
    _write(tmp_path, **over)
    assert _maps(tmp_path).impfn is None


def test_loader_without_a_file_gives_none(tmp_path):
    assert _maps(tmp_path).impfn is None


# ---- real source: the commits that carried the cost ------------------------------------------------------

def _real(patch):
    from maps.load import load
    import diffparse

    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    maps = load(str(ROOT / "maps"))
    assert maps.impfn is not None, "maps/impfn.bin missing or not recorded by this interpreter"
    text = (ROOT / "tests_sel" / "fixtures" / "import_deps" / patch).read_text("utf-8")
    got, why = rules.select(diffparse.parse(text), maps, tests, src_root=PIN)
    return {tests[n]["id"] for n in got}, why


MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"
MEMORY = "tests/test_generator.py::TestGenerator::test_generator_memory"


@needs_pin
@needs_kit
def test_placeholder_name_edit_no_longer_selects_the_world():
    got, why = _real("5xj4.patch")
    failing = set(json.loads((ROOT / "tests_sel" / "fixtures" / "import_deps" / "5xj4_failing.json").read_text("utf-8")))
    assert failing <= got, sorted(failing - got)[:5]  # the labelled failures are all kept
    assert MAX_NODES not in got
    assert len(got) < 1300, len(got)
    assert "R2" in why


@needs_pin
@needs_kit
def test_parse_one_edit_still_keeps_test_max_nodes():
    # blindness: parse_one is in test_max_nodes's own footprint, and it fails there
    got, _why = _real("8s3g.patch")
    failing = set(json.loads((ROOT / "tests_sel" / "fixtures" / "import_deps" / "8s3g_failing.json").read_text("utf-8")))
    assert MAX_NODES in got
    assert failing <= got, sorted(failing - got)[:5]


@needs_pin
@needs_kit
def test_noise_a_comment_only_edit_of_the_same_function_selects_as_before():
    # the same line as 5xj4, the text changed but not the code: R4 (ast unchanged), no R2, nothing selected
    from maps.load import load
    import diffparse

    rel = "sqlglot/expressions/core.py"
    old = (PIN / rel).read_text("utf-8")
    new = old.replace('        return self.text("this") or "?"\n', '        return self.text("this") or "?"  # same\n', 1)
    assert new != old
    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    got, why = rules.select(diffparse.parse(make_patch(rel, old, new)), load(str(ROOT / "maps")), tests, src_root=PIN)
    assert "R2" not in why and "ast unchanged" in why
    assert len(got) == 0, len(got)


# ---- H1: a def that writes state the importers do not own keeps the wide answer ---------------------------------

UNSAFE = {
    "memo_in_module_globals": "def __getattr__(name):\n    attr = look(name)\n    globals()[name] = attr\n    return attr\n",
    "h1_mutant": "def __getattr__(name):\n    attr = look(name)\n    globals()[name] = attr if name != 'BigQuery' else None\n    return attr\n",
    "global_statement": "def f():\n    global X\n    X = 1\n",
    "instance_attribute_store": "def f(self):\n    self.x = 1\n",
    "augmented_instance_attribute": "def f(self):\n    self._index += 1\n",
    "class_registry_store": "def f(cls, k, v):\n    cls.REG[k] = v\n",
    "append_to_attribute": "def f(self, e):\n    self.errors.append(e)\n",
    "append_to_alias_of_non_local": "def f(self):\n    d = self.table\n    d.append(1)\n",
    "setitem_on_alias_of_non_local": "def f(self, k):\n    d = self.table\n    d[k] = 1\n",
    "store_on_loop_variable": "def f(items):\n    for x in items:\n        x.y = 1\n",
    "sys_modules": "def f(m):\n    sys.modules['a'] = m\n",
    "dunder_dict": "def f(o):\n    return o.__dict__\n",
    "setattr_call": "def f(o):\n    setattr(o, 'a', 1)\n",
    "delete_attribute": "def f(self):\n    del self.x\n",
    "mutating_call_on_a_call_result": "def f():\n    get().append(1)\n",
    "in_place_add_on_an_alias": "def f(a):\n    n = a\n    n += [1]\n",
    "cache_decorator": "@functools.lru_cache(maxsize=None)\ndef f(x):\n    return x\n",
    "global_in_a_nested_def": "def f(x):\n    def g():\n        global Y\n        Y = 1\n    return g\n",
    "lambda_that_sets_an_attribute": "f = lambda self: self.__setattr__('a', 1)\n",
    "import_module": "def f(n):\n    return importlib.import_module(n)\n",
}
SAFE = {
    "pure_property": "def name(self):\n    return self.text('this') or '?'\n",
    "builds_and_returns_a_list": "def f(xs):\n    out = []\n    for x in xs:\n        out.append(x)\n    return out\n",
    "builds_and_returns_a_dict": "def f(k, v):\n    d = {}\n    d[k] = v\n    return d\n",
    "local_counter": "def f():\n    n = 0\n    n += 1\n    return n\n",
    "copy_then_store": "def f(a):\n    b = a.copy()\n    b['k'] = 1\n    return b\n",
    "comprehension": "def f(xs):\n    return [x for x in xs]\n",
}


@pytest.mark.parametrize("name", sorted(UNSAFE))
def test_def_that_may_write_non_local_state_is_unsafe(name):
    src = UNSAFE[name]
    assert rules._unsafe_def(ast.parse(src), 1, len(src.splitlines())) is True


@pytest.mark.parametrize("name", sorted(SAFE))
def test_def_that_leaves_non_local_state_alone_is_safe(name):
    src = SAFE[name]
    assert rules._unsafe_def(ast.parse(src), 1, len(src.splitlines())) is False


def test_import_time_memoiser_keeps_the_wide_answer():
    text = "# 1\n# 2\n# 3\n# 4\ndef f(name):\n    attr = look(name)\n    globals()[name] = attr\n    return attr\n"
    assert run(Stub({PATH: [(5, 8, [M1])]}, il=(7,)), s=(7, 7, "function", 5, 8, ""), text=text) == WIDE


def test_memoiser_that_is_not_the_edited_def_but_encloses_it_keeps_the_wide_answer():
    text = ("# 1\n# 2\ndef outer(name):\n    def f(x):\n        return x or '?'\n    globals()[name] = f\n    return f\n")
    assert run(Stub({PATH: [(3, 7, [M1])]}, il=(5,)), s=(5, 5, "function", 4, 5, ""), text=text) == WIDE


@needs_pin
def test_h1_memoising_getattr_edit_keeps_test_jinja():
    """The reviewer's mutant: `globals()[name] = attr` of sqlglot/dialects/__init__.py:__getattr__ stores the dialect class on the module. A
    test that reads sqlglot.dialects.BigQuery later finds it there; the edit makes it None and test_jinja fails with an AttributeError, yet that
    test imports none of the module bodies that ran __getattr__ at import. R2 must not narrow it. BR_TEST_MAPS: a maps copy whose impfn.bin
    carries this interpreter's sys.version (host runs); without a usable impfn the test cannot tell and is skipped."""
    from maps.load import load
    import diffparse

    maps = load(os.environ.get("BR_TEST_MAPS") or str(ROOT / "maps"))
    if maps.impfn is None:
        pytest.skip("no impfn map recorded by this interpreter")
    tests = [{"number": n, "id": i, "file": i.split("::")[0], "seconds": 0.1} for n, i in enumerate(maps.ids) if i is not None]
    rel = "sqlglot/dialects/__init__.py"
    old = (PIN / rel).read_text("utf-8")
    new = old.replace("        globals()[name] = attr\n", "        globals()[name] = attr if name != 'BigQuery' else None\n", 1)
    assert new != old
    got, why = rules.select(diffparse.parse(make_patch(rel, old, new)), maps, tests, src_root=PIN)
    ids = {tests[n]["id"] for n in got}
    assert "tests/test_tokens.py::TestTokens::test_jinja" in ids
    assert "R2" not in why


# ---- L1: the build and the loader refuse a map that cannot be trusted ---------------------------------------------

def test_loader_refuses_three_equal_seeds(tmp_path):
    _write(tmp_path, seeds=[0, 0, 0])
    assert _maps(tmp_path).impfn is None


def _runs(*rows):
    return [{"seed": s, "rc": rc, "ncollected": n} for s, rc, n in rows]


def test_build_check_accepts_three_clean_seeds():
    import impfn

    assert impfn.check_runs(_runs((0, 0, 1621), (1, 0, 1621), (2, 0, 1621)), True) == []


@pytest.mark.parametrize("rows,needle", [
    (((0, 0, 1621), (1, 2, 1621), (2, 0, 1621)), "collection exited 2"),  # an import error hid a test module
    (((0, 0, 1621), (1, 0, 1620), (2, 0, 1621)), "different numbers"),
    (((0, 0, 0), (1, 0, 0), (2, 0, 0)), "collected no test"),
    (((0, 0, 1621), (1, 0, 1621)), "fewer than three"),
    (((0, 0, 1621), (0, 0, 1621), (0, 0, 1621)), "fewer than three"),
])
def test_build_check_refuses_an_untrustworthy_run(rows, needle):
    import impfn

    assert any(needle in p for p in impfn.check_runs(_runs(*rows), True))


def test_r2_is_off_by_default(monkeypatch):
    monkeypatch.setattr(rules, "R2_ENABLED", False)
    assert run(Stub({PATH: [(5, 9, [M1])]})) == WIDE
