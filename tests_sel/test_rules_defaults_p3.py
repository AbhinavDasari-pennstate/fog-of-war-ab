"""P3: a literal-default-only edit of a def whose body no test ever ran selects nothing (it used to return None
and fall back to the class rule: every test that touches the class, 1541 tests for Expr.and_).

Unit tests use a stub of the maps, so they need no pinned sqlglot. The real-source tests need BR_SRC and the kit."""

import json
import sys

import pytest

from conftest import KIT, PIN, ROOT, make_patch, needs_kit, needs_pin

sys.path.insert(0, str(ROOT))
import pyscope  # noqa: E402
import rules  # noqa: E402

PATH = "sqlglot/thing.py"
SRC = '''\
class Base:
    def and_(self, x, copy: bool = True):
        """Doc."""
        raise NotImplementedError

    def other(self, x, copy: bool = True):
        return x
'''
# line 2 def and_ (header), 3 docstring, 4 raise, 6 def other, 7 return x
TESTS = [{"number": n, "id": f"t{n}", "file": "tests/t.py", "seconds": 0.1} for n in range(8)]


class Stub:
    """Just the tables _defaults_only reads."""

    def __init__(self, lines=None, il=(), un=()):
        self.lines = {PATH: dict(lines if lines is not None else {7: 1})}
        self.sets = [0, 0b1010]
        self.importtime = {PATH: set(il)}
        self.unstable = {PATH: set(un)}
        self.imports = {}
        self.srcreaders = {}
        self.files = {}


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
    return rules._defaults_only(ctx, path, diffs, old_tree, new_tree, ranges)


def flip(src, which="and_"):
    out, hit = [], False
    for line in src.split("\n"):
        if not hit and f"def {which}(" in line:
            line, hit = line.replace("copy: bool = True", "copy: bool = False"), True
        out.append(line)
    return "\n".join(out)


def test_unrun_traceable_body_selects_nothing():
    got = run(SRC, flip(SRC))
    assert got == 0  # not None: None means "fall back to the class rule"


def test_body_that_a_test_ran_still_selects_its_callers():
    maps = Stub(lines={4: 1, 7: 1})
    assert run(SRC, flip(SRC), maps) == 0b1010


def test_body_line_credited_to_import_only_keeps_the_fallback():
    # set id 0 = the line ran at import and no test context owns it
    assert run(SRC, flip(SRC), Stub(lines={4: 0, 7: 1}, il={4})) is None


def test_body_line_marked_import_time_keeps_the_fallback():
    assert run(SRC, flip(SRC), Stub(il={4})) is None


def test_unstable_body_line_keeps_the_fallback():
    assert run(SRC, flip(SRC), Stub(un={4})) is None


def test_file_without_coverage_data_keeps_the_fallback():
    assert run(SRC, flip(SRC), Stub(lines={})) is None


@pytest.mark.parametrize("body", ['        """Doc."""', "        ..."])
def test_docstring_or_ellipsis_only_body_keeps_the_fallback(body):
    # coverage may not record a line event for these, so an empty record proves nothing
    src = "\n".join([
        "class Base:",
        "    def and_(self, x, copy: bool = True):",
        body,
        "",
        "    def other(self, x, copy: bool = True):",
        "        return x",
        "",
    ])
    assert run(src, flip(src), Stub(lines={6: 1})) is None


def test_nested_def_keeps_the_fallback():
    src = "def outer(x):\n    def inner(y, copy: bool = True):\n        return y\n    return inner(x)\n"
    new = src.replace("copy: bool = True", "copy: bool = False")
    assert run(src, new, Stub(lines={4: 1})) is None


@pytest.mark.parametrize("path", ["sqlglot/optimizer/rule.py", "sqlglot/executor/env.py"])
def test_defs_whose_signature_is_inspected_keep_the_fallback(path):
    # optimizer.optimize reads inspect.getfullargspec(rule).args; executor/env.py null_if_any reads inspect.signature(func)
    assert run(SRC, flip(SRC), path=path) is None


def test_tests_directory_keeps_the_fallback():
    assert run(SRC, flip(SRC), path="tests/helper.py") is None


def test_one_unrun_and_one_run_def_select_only_the_runner():
    new = SRC.replace("copy: bool = True", "copy: bool = False")  # both defs change
    assert run(SRC, new, Stub(lines={7: 1})) == 0b1010


def test_one_unrun_def_next_to_one_that_cannot_be_judged_keeps_the_fallback():
    src = SRC + "\n    def third(self, copy: bool = True):\n        ...\n"
    new = src.replace("copy: bool = True", "copy: bool = False")
    assert run(src, new) is None


# ---- real source: Expr.and_ (commit 1vng of the const board) ----------------------------------------

def _select(rel, edit):
    from maps.load import load
    import diffparse

    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    maps = load(str(ROOT / "maps"))
    old = (PIN / rel).read_text("utf-8")
    lines = old.split("\n")
    edit(lines)
    new = "\n".join(lines)
    assert new != old
    got, why = rules.select(diffparse.parse(make_patch(rel, old, new)), maps, tests, src_root=PIN)
    return got, why, {t["id"]: t["number"] for t in tests}


@pytest.mark.parametrize("ln,old_s,new_s", [
    (551, "copy: bool = True,", "copy: bool = False,"),  # Expr.and_ (1vng): the body is `raise NotImplementedError`, no test calls it
    (581, "copy: bool = True,", "copy: bool = False,"),  # Expr.or_
])
@needs_pin
@needs_kit
def test_abstract_expr_method_default_selects_no_test(ln, old_s, new_s):
    def edit(lines):
        assert old_s in lines[ln - 1], lines[ln - 1]
        lines[ln - 1] = lines[ln - 1].replace(old_s, new_s, 1)

    got, why, _ids = _select("sqlglot/expressions/core.py", edit)
    assert "literal default values only" in why
    assert len(got) == 0, f"{len(got)} tests kept"


@needs_pin
@needs_kit
def test_optimizer_overload_stub_default_keeps_the_class_rule():
    # normalize_identifiers is a rule handed to optimize(), so inspect.getfullargspec may read it
    def edit(lines):
        i = next(i for i, l in enumerate(lines) if "store_original_column_identifiers: bool = False" in l)
        lines[i] = lines[i].replace("bool = False", "bool = True", 1)

    _got, why, _ids = _select("sqlglot/optimizer/normalize_identifiers.py", edit)
    assert "literal default values only" not in why
