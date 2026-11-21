"""T28: a class- or module-level data edit that changes what a module does while it imports (a parse at import, a metaclass hook) fails every
test that imports that module, lazily or not. Real full-suite runs (reviewer f09 and four crafted edits of the same kind) failed tests the
coverage maps had no context for: test_time.py::test_canonical_time_format_vocabulary imports the duckdb generator only through the dialect
registry, and importing it raises when the tokenizer maps SELECT to another token."""

import json
import sys

import pytest

from conftest import KIT, PIN, ROOT, make_patch, needs_kit, needs_pin

sys.path.insert(0, str(ROOT))
pytestmark = [needs_pin, needs_kit]

LAZY = "tests/test_time.py::TestTime::test_canonical_time_format_vocabulary"

# (file, line, old text, new text): each one makes `import sqlglot.generators.duckdb` raise while `import sqlglot` still works
CASES = {
    "tokenizer_keywords_select": ("sqlglot/tokens.py", 392, "TokenType.SELECT", "TokenType.SET"),
    "tokenizer_keywords_with": ("sqlglot/tokens.py", 426, "TokenType.WITH", "TokenType.SET"),
    "parser_window_tokens": ("sqlglot/parser.py", 1806, "{TokenType.OVER}", "{TokenType.SET}"),
    "dialect_flag": ("sqlglot/dialects/dialect.py", 467, "SUPPORTS_USER_DEFINED_TYPES = True", "SUPPORTS_USER_DEFINED_TYPES = False"),
}


@pytest.fixture(scope="module")
def env():
    from maps.load import load
    import diffparse
    import rules

    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    return load(str(ROOT / "maps")), tests, diffparse, rules


def _select(env, rel, ln, old_s, new_s):
    maps, tests, diffparse, rules = env
    old = (PIN / rel).read_text("utf-8")
    lines = old.split("\n")
    assert old_s in lines[ln - 1], (rel, ln, lines[ln - 1])
    lines[ln - 1] = lines[ln - 1].replace(old_s, new_s)
    got, why = rules.select(diffparse.parse(make_patch(rel, old, "\n".join(lines))), maps, tests, src_root=PIN)
    return got, why, {t["id"]: t["number"] for t in tests}


@pytest.mark.parametrize("name", sorted(CASES))
def test_edit_that_breaks_a_lazily_imported_module_selects_its_lazy_importers(env, name):
    got, _, ids = _select(env, *CASES[name])
    assert ids[LAZY] in got


def test_docstring_only_edit_adds_no_import_time_tests(env):
    maps, tests, diffparse, rules = env
    rel = "sqlglot/dialects/dialect.py"
    old = (PIN / rel).read_text("utf-8")
    new = old.replace('"""Whether `CONCAT`\'s arguments must be strings."""', '"""Whether `CONCAT`\'s arguments must be strings (edited)."""', 1)
    assert new != old
    got, _ = rules.select(diffparse.parse(make_patch(rel, old, new)), maps, tests, src_root=PIN)
    ctx = rules.Ctx(maps, tests, PIN)
    ctx.doc_only = True
    assert ctx.imptime(rel, "Dialect") == 0
    assert len(got) < 1600


def test_imptime_skips_the_edited_module_and_reads_the_class_map(env):
    maps, tests, diffparse, rules = env
    ctx = rules.Ctx(maps, tests, PIN)
    files, classes, ranges, state, base = maps.impexec
    assert state == "ok"
    assert "sqlglot/generators/duckdb.py" in classes["Tokenizer"] or "sqlglot/generators/duckdb.py" in files["sqlglot/tokens.py"]
    with_self = ctx.imptime("sqlglot/dialects/bigquery.py", "")
    assert isinstance(with_self, int)


def test_damaged_map_widens_to_every_test(env):
    maps, tests, diffparse, rules = env

    class Damaged:
        impexec = ({}, {}, {}, "bad", frozenset())

    ctx = rules.Ctx(maps, tests, PIN)
    ctx.m = Damaged()
    assert ctx.imptime("sqlglot/tokens.py", "Tokenizer") == ctx.all


def test_edit_that_makes_the_edited_module_raise_while_it_imports_selects_its_lazy_importers(env):
    """generators/duckdb.py parses ZIPF_TEMPLATE in its class body: a broken template makes the import raise, and
    test_time.py imports that module only through the dialect registry."""
    maps, tests, diffparse, rules = env
    rel = "sqlglot/generators/duckdb.py"
    old = (PIN / rel).read_text("utf-8")
    new = old.replace("SELECT :random_expr AS r),", "SELECT :random_expr AS r FROM FROM),", 1)
    assert new != old
    got, _ = rules.select(diffparse.parse(make_patch(rel, old, new)), maps, tests, src_root=PIN)
    assert {t["id"]: t["number"] for t in tests}[LAZY] in got


def test_number_or_bool_constant_inside_a_call_in_a_class_body_does_not_widen(env):
    maps, tests, diffparse, rules = env
    rel = "sqlglot/parsers/singlestore.py"
    old = (PIN / rel).read_text("utf-8")
    new = old.replace('_show_parser("GROUPS FOR USER", target=True)', '_show_parser("GROUPS FOR USER", target=False)', 1)
    assert new != old
    got, _ = rules.select(diffparse.parse(make_patch(rel, old, new)), maps, tests, src_root=PIN)
    assert len(got) < 200


def test_string_constant_inside_a_call_in_a_class_body_widens_to_the_importers(env):
    maps, tests, diffparse, rules = env
    rel = "sqlglot/parsers/singlestore.py"
    old = (PIN / rel).read_text("utf-8")
    new = old.replace('_show_parser("GROUPS FOR USER", target=True)', '_show_parser("GROUPS FOR USER ", target=True)', 1)
    assert new != old
    got, _ = rules.select(diffparse.parse(make_patch(rel, old, new)), maps, tests, src_root=PIN)
    imp = maps.imports[rel]
    assert len(got) >= len([n for n in range(len(tests)) if (imp >> n) & 1])


def test_zero_month_in_a_date_call_at_module_scope_selects_every_importer(env):
    """Reviewer H1: parsers/tsql.py DEFAULT_START_DATE = datetime.date(1900, 1, 1) -> (1900, 0, 1) raises ValueError at import; a number
    inside a call is not a harmless constant. test_get_or_raise and the lazy test import the tsql dialect."""
    got, _, ids = _select(env, "sqlglot/parsers/tsql.py", 54, "datetime.date(1900, 1, 1)", "datetime.date(1900, 0, 1)")
    for want in (LAZY, "tests/dialects/test_dialect.py::TestDialect::test_get_or_raise", "tests/dialects/test_dialect.py::TestDialect::test_enum"):
        assert ids[want] in got, want


def test_class_edit_in_a_module_that_import_sqlglot_loads_selects_test_lazy_load(env):
    got, _, ids = _select(env, "sqlglot/tokens.py", 392, "TokenType.SELECT", "TokenType.SET")
    assert ids["tests/dialects/test_dialect.py::TestDialect::test_lazy_load"] in got
