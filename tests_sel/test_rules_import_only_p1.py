"""P1: a multi-line literal or expression assigned at class level ran as ONE statement at import. Its interior lines
carry no coverage record (a 264-line set literal has one recorded line), so the L2 import-only shortcut
(`all(l in il ...)`) used to fail and credit the first importers (the TPC-DS giant among them). A line with no
record now counts as part of the statement when the AST shows that the span is one simple statement whose
parts all run when it runs.

Unit tests use a stub of the maps; the real-source test needs BR_SRC and the kit."""

import json
import sys

import pytest

from conftest import KIT, PIN, ROOT, make_patch, needs_kit, needs_pin

sys.path.insert(0, str(ROOT))
import pyscope  # noqa: E402
import rules  # noqa: E402

PATH = "sqlglot/thing.py"
TESTS = [{"number": n, "id": f"t{n}", "file": "tests/t.py", "seconds": 0.1} for n in range(4)]


class Stub:
    def __init__(self, lines, il):
        self.lines = {PATH: dict(lines)}
        self.importtime = {PATH: set(il)}


def check(src, start, end, lines, il, tree=True):
    ctx = rules.Ctx(Stub(lines, il), TESTS, "/nonexistent")
    ctx.old_tree = pyscope.parse(src, PATH) if tree else None
    return rules._import_only(ctx, PATH, start, end, set(il), dict(lines))


SET = 'class G:\n    KW = {\n        "a",\n        "b",\n        "c",\n    }\n    OTHER = 1\n'
# line 1 class, 2 `KW = {` (the one recorded line), 3-5 elements, 6 `}`, 7 OTHER


def test_interior_lines_without_a_record_count_as_import_time():
    assert check(SET, 2, 6, {2: 1}, {2}) is True


def test_every_line_import_time_is_still_true_without_a_tree():
    assert check(SET, 2, 6, {2: 1, 3: 1, 4: 1, 5: 1, 6: 1}, {2, 3, 4, 5, 6}, tree=False) is True


def test_no_line_of_the_span_at_import_is_false():
    assert check(SET, 2, 6, {}, set()) is False  # nothing proves the statement ran at import


def test_a_line_a_test_ran_is_false():
    assert check(SET, 2, 6, {2: 1, 4: 2}, {2}) is False


def test_without_a_tree_the_new_rule_does_not_apply():
    assert check(SET, 2, 6, {2: 1}, {2}, tree=False) is False


@pytest.mark.parametrize("src", [
    # annotated assignment, a call, an f-string, arithmetic on literals, a star unpack: all evaluated when the statement runs
    'class G:\n    KW: set = {\n        "a",\n        "b",\n    }\n',
    'class G:\n    KW = frozenset(\n        ("a", "b", "c"),\n    )\n',
    'class G:\n    KW = {\n        "a": 1,\n        **OTHER,\n        "b": -2,\n    }\n',
    'class G:\n    KW = [\n        f"a{X}",\n        "b" + "c",\n    ]\n',
])
def test_eager_statements_are_accepted(src):
    end = len(src.rstrip("\n").split("\n"))
    assert check(src, 2, end, {2: 1}, {2}) is True


@pytest.mark.parametrize("src", [
    'class G:\n    KW = {\n        "a": lambda x: x,\n        "b": 1,\n    }\n',        # a lambda body runs only when called
    'class G:\n    KW = {\n        "a",\n        "b" if X else "c",\n    }\n',          # a branch that is not taken has no record
    'class G:\n    KW = (\n        A or\n        {"b"}\n    )\n',                        # short circuit
    'class G:\n    KW = [\n        x\n        for x in Y\n    ]\n',                      # comprehension
    'class G:\n    KW = (\n        x\n        for x in Y\n    )\n',                      # generator, runs later
    'class G:\n    KW = (\n        1 < X <\n        3\n    )\n',                        # chained comparison short circuits
    'class G:\n    KW += {\n        "a",\n        "b",\n    }\n',                       # augmented assignment
    'class G:\n    for x in (\n        "a",\n        "b"):\n        pass\n',            # not a simple statement
])
def test_conditional_or_deferred_parts_are_refused(src):
    end = len(src.rstrip("\n").split("\n"))
    assert check(src, 2, end, {2: 1}, {2}) is False


def test_span_that_is_not_exactly_one_statement_is_refused():
    src = "class G:\n    A = {\n        1,\n    }\n    B = {\n        2,\n    }\n"
    assert check(src, 2, 7, {2: 1, 5: 1}, {2, 5}) is False  # two statements in one span


# ---- real source: commit lyov of the const board (a RESERVED_KEYWORDS entry of MySQLGenerator) -------------

TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


@needs_pin
@needs_kit
def test_reserved_keyword_entry_does_not_credit_the_first_importers():
    from maps.load import load
    import diffparse

    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    maps = load(str(ROOT / "maps"))
    rel = "sqlglot/generators/mysql.py"
    old = (PIN / rel).read_text("utf-8")
    lines = old.split("\n")
    assert lines[600].strip() == '"varchar",', lines[600]
    lines[600] = lines[600].replace("varchar", "zarchar")
    got, why = rules.select(diffparse.parse(make_patch(rel, old, "\n".join(lines))), maps, tests, src_root=PIN)
    ids = {t["id"]: t["number"] for t in tests}
    assert "L2" in why or why.startswith("V "), why  # T29: a word lookup map now answers this edit before L2 does
    assert ids[TPCDS] not in got, "the first importer of mysql.py (the TPC-DS giant) is still credited"
    assert len(got) < 190
