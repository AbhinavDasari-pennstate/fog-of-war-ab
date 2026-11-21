"""R3: a literal default of a def that only BUILDS a closure. `date_delta_to_binary_interval_op(cast=True)` runs at import
(generators/duckdb.py and spark.py call it) so its lines are import-time lines and `_defaults_only` widened to every importer of
dialects/dialect.py (1617 tests for 05z0, which fails none). When the parameter is read only inside the body of a nested def, that
def's body never ran at import and no line of it is unstable, the new default can only reach the tests that ran the nested body.

Unit tests use a stub of the maps. The real-source tests need BR_SRC and the kit."""

import json
import sys

import pytest

from conftest import KIT, PIN, ROOT, needs_kit, needs_pin

sys.path.insert(0, str(ROOT))
import pyscope  # noqa: E402
import rules  # noqa: E402

PATH = "sqlglot/thing.py"
TESTS = [{"number": n, "id": f"t{n}", "file": "tests/t.py", "seconds": 0.1} for n in range(8)]

SRC = """\
def factory(cast: bool = True):
    def inner(x):
        y = x
        return y if cast else None
    return inner
"""
# 1 def factory, 2 def inner, 3-4 the body of inner, 5 return inner
IMPORT_LINES = {1, 2, 5}


class Stub:
    def __init__(self, lines=None, il=IMPORT_LINES, un=(), ranges=((1, 5, ["sqlglot/user.py"]),), impfn=True):
        self.lines = {PATH: dict(lines if lines is not None else {2: 2, 3: 1, 4: 1})}
        self.sets = [0, 0b1010, 0b0100]
        self.importtime = {PATH: set(il)}
        self.unstable = {PATH: set(un)}
        self.imports = {}  # the module that ran the factory is not in it: R2 keeps the wide answer where R3 does not apply
        self.srcreaders = {}
        self.files = {}
        # the function map: the factory (lines 1-5) ran under a module body
        self.impfn = ({PATH: list(ranges)}, frozenset()) if impfn else None

    def stmt_mask(self, path, line):
        return self.sets[self.lines[path].get(line, 0)]


ALL = 0xFF  # ctx.wide(PATH) with these stubs: the whole suite


def run(old, new, maps=None, path=PATH):
    maps = maps or Stub()
    if path != PATH:
        for name in ("lines", "importtime", "unstable"):
            getattr(maps, name)[path] = getattr(maps, name).pop(PATH)
    ctx = rules.Ctx(maps, TESTS, "/nonexistent")
    old_tree, new_tree = pyscope.parse(old, path), pyscope.parse(new, path)
    ol, nl = old.split("\n"), new.split("\n")
    ranges = [(i + 1, i + 1) for i, (a, b) in enumerate(zip(ol, nl)) if a != b]
    diffs = pyscope.ast_diff(old_tree, new_tree, ranges)
    got = rules._defaults_only(ctx, path, diffs, old_tree, new_tree, ranges)
    run.notes = ctx.notes
    return got


def flip(src, a="cast: bool = True", b="cast: bool = False"):
    return src.replace(a, b, 1)


def test_closure_factory_default_selects_only_the_runners_of_the_closure():
    assert run(SRC, flip(SRC)) == 0b1110  # udef of lines 2-5: line 2 (set 2) and the body (set 1)
    assert any("R3" in n for n in run.notes)


def test_a_number_or_none_default_is_a_literal_too():
    src = SRC.replace("cast: bool = True", "cast: int = 1")
    assert run(src, flip(src, "cast: int = 1", "cast: int = -2")) == 0b1110
    assert run(src, flip(src, "cast: int = 1", "cast: int = None")) == 0b1110


def test_unrun_closure_body_with_import_time_header_selects_nothing_new():
    assert run(SRC, flip(SRC), Stub(lines={2: 2})) == 0b0100


def test_direct_read_in_the_factory_body_keeps_the_wide_answer():
    src = SRC.replace("    return inner", "    z = cast\n    return inner")
    assert run(src, flip(src), Stub(il={1, 2, 5, 6})) == ALL


def test_read_through_locals_keeps_the_wide_answer():
    src = SRC.replace("    return inner", "    z = locals()\n    return inner")
    assert run(src, flip(src), Stub(il={1, 2, 5, 6})) == ALL


def test_a_lambda_reading_it_keeps_the_wide_answer():
    # the lambda's line is a line of the factory itself: its coverage cannot be told apart from the import-time run
    src = "def factory(cast: bool = True):\n    return lambda x: x if cast else None\n"
    assert run(src, flip(src), Stub(lines={2: 1}, il={2})) == ALL


def test_a_default_argument_of_the_nested_def_is_read_at_import():
    src = SRC.replace("def inner(x):", "def inner(x, c=cast):")
    assert run(src, flip(src)) == ALL


def test_nested_def_that_ran_under_a_module_body_keeps_the_wide_answer():
    # the function map disagrees with importtime.bin: the nested def ran at import (a module body called the closure)
    assert run(SRC, flip(SRC), Stub(ranges=((1, 5, ["sqlglot/user.py"]), (2, 4, ["sqlglot/user.py"])))) == ALL


def test_without_the_function_map_the_wide_answer_stays():
    assert run(SRC, flip(SRC), Stub(impfn=False)) == ALL


def test_a_body_edit_of_the_closure_is_not_r3_and_still_selects_its_runners():
    # blindness: flipping the condition inside the nested body is an ordinary function-level edit
    ctx = rules.Ctx(Stub(), TESTS, "/nonexistent")
    got = rules._func_level(ctx, PATH, (4, 4, "function", 2, 4, ""))
    assert got == 0b1010  # the tests that ran line 4, nothing from import time


def test_nested_body_line_that_ran_at_import_keeps_the_wide_answer():
    assert run(SRC, flip(SRC), Stub(il={1, 2, 3, 5})) == ALL


def test_unstable_nested_body_line_keeps_the_wide_answer():
    assert run(SRC, flip(SRC), Stub(un={4})) == ALL


def test_one_line_nested_def_keeps_the_wide_answer():
    src = "def factory(cast: bool = True):\n    def inner(x): return x if cast else None\n    return inner\n"
    assert run(src, flip(src), Stub(lines={2: 1}, il={1, 2, 3})) == ALL


def test_decorated_factory_keeps_the_wide_answer():
    src = "@cache\n" + SRC
    assert run(src, flip(src), Stub(lines={3: 2, 4: 1, 5: 1}, il={2, 3, 6})) == ALL


def test_nested_factory_keeps_the_wide_answer():
    src = "def outer():\n" + "".join("    " + l + "\n" for l in SRC.splitlines()) + "    return factory\n"
    assert run(src, flip(src), Stub(lines={3: 2, 4: 1, 5: 1}, il={1, 2, 3, 6, 7})) == ALL


@pytest.mark.parametrize("default", ["make()", '-"x"'])
def test_a_default_that_is_not_a_literal_is_not_a_defaults_only_edit(default):
    # a call runs, and `-"x"` raises, when the def runs at import: the edit never reaches R3
    assert run(SRC, flip(SRC, "cast: bool = True", f"cast: bool = {default}")) is None


@pytest.mark.parametrize("path", ["sqlglot/optimizer/rule.py", "sqlglot/executor/env.py"])
def test_defs_whose_signature_is_inspected_keep_the_wide_answer(path):
    assert run(SRC, flip(SRC), path=path) == ALL


def test_a_second_changed_def_that_is_not_a_closure_factory_keeps_the_wide_answer():
    src = SRC + "\ndef other(copy: bool = True):\n    return copy\n"
    new = src.replace("copy: bool = True", "copy: bool = False").replace("cast: bool = True", "cast: bool = False")
    assert run(src, new, Stub(lines={2: 2, 3: 1, 4: 1, 8: 1}, il=IMPORT_LINES | {7, 8})) == ALL


def test_a_changed_parameter_that_is_unused_is_harmless():
    src = SRC.replace("def factory(cast: bool = True)", "def factory(cast: bool = True, other: int = 1)")
    new = flip(src, "other: int = 1", "other: int = 2")
    assert run(src, new) == 0b1110


# ---- real source: 05z0 (dialects/dialect.py `cast: bool = True` of date_delta_to_binary_interval_op) --------

def _real(patch):
    from maps.load import load
    import diffparse

    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    maps = load(str(ROOT / "maps"))
    text = (ROOT / "tests_sel" / "fixtures" / "import_deps" / patch).read_text("utf-8")
    got, why = rules.select(diffparse.parse(text), maps, tests, src_root=PIN)
    return {tests[n]["id"] for n in got}, why


@needs_pin
@needs_kit
def test_05z0_selects_the_runners_of_the_closure_not_the_importers_of_dialect_py():
    got, why = _real("05z0.patch")
    assert "R3" in why
    assert "tests/test_parser.py::TestParser::test_max_nodes" not in got
    assert "tests/test_generator.py::TestGenerator::test_generator_memory" not in got
    assert 0 < len(got) < 80, len(got)
