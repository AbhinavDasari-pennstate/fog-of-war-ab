"""R2: an edit of a function that ran while a module imported used to widen to every importer of the edited module
(`ctx.wide(path)`: 1617 tests for Placeholder.name). The tests that can depend on such a run are the importers of the
module bodies that ran the function (maps/impfn.bin: sqlglot and test modules, collection included, three seeds), plus the
tests that ran the lines, plus what the import probe finds. Anything the map or the probe cannot answer keeps the wide answer.

Unit tests use a stub of the maps. The real-source tests need BR_SRC and the kit."""

import json
import marshal
import sys
import zlib

import pytest

from conftest import KIT, PIN, ROOT, make_patch, needs_kit, needs_pin

sys.path.insert(0, str(ROOT))
import rules  # noqa: E402

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


def run(maps, probe=0, s=S):
    ctx = rules.Ctx(maps, TESTS, "/nonexistent")
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
    ctx.probe_mask = lambda: pytest.fail("the probe was asked")
    assert rules._func_level(ctx, PATH, S) == WIDE


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
    new = old.replace('        return self.text("this") or "?"
', '        return self.text("this") or "?"  # same
', 1)
    assert new != old
    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    got, why = rules.select(diffparse.parse(make_patch(rel, old, new)), load(str(ROOT / "maps")), tests, src_root=PIN)
    assert "R2" not in why and "ast unchanged" in why
    assert len(got) == 0, len(got)
