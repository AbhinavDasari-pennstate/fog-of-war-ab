"""Regression probes from the T3 review: real full-suite runs showed these edits break exactly
these tests, in places where line coverage alone is wrong (import-time class body, decorator,
default argument). Selection must contain every one."""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from conftest import KIT, PIN, ROOT, make_patch, needs_kit, needs_pin

sys.path.insert(0, str(ROOT))
pytestmark = [needs_pin, needs_kit]


# (name, file, line number, old text, new text, tests that really fail, as node ids)
CASES = {
    "mysql_quotes_class_body": (
        "sqlglot/dialects/mysql.py", 88, "QUOTES = [\"'\", '\"']", "QUOTES = [\"'\"]",
        ["tests/dialects/test_mysql.py::TestMySQL::test_string_literals",
         "tests/dialects/test_mysql.py::TestMySQL::test_ddl",
         "tests/dialects/test_mysql.py::TestMySQL::test_escape",
         "tests/dialects/test_starrocks.py::TestStarrocks::test_ddl",
         "tests/dialects/test_starrocks.py::TestStarrocks::test_map_literal",
         "tests/dialects/test_starrocks.py::TestStarrocks::test_unnest"],
    ),
    "executor_decorator": (
        "sqlglot/executor/python.py", 336, "@staticmethod", "@classmethod",
        [f"tests/test_executor.py::TestExecutor::{n}" for n in (
            "test_distinct_order_by_column_from_different_table", "test_correlated_exists_over_scalar_aggregate",
            "test_execute_callable", "test_execute_subqueries", "test_execute_tables", "test_execute_tpcds",
            "test_outer_joins_preserve_unmatched_rows", "test_set_operations", "test_subqueries",
            "test_in_any_subquery_without_a_from")],
    ),
    "default_argument": (
        "sqlglot/optimizer/qualify_columns.py", 35, "expand_alias_refs: bool = True,",
        "expand_alias_refs: bool = False,",
        [f"tests/test_optimizer.py::TestOptimizer::{n}" for n in (
            "test_pushdown_projection", "test_qualify_columns__invalid", "test_expand_alias_refs")],
    ),
    # T18: real full-suite failures of stress-3-0011 and stress-3-0049. DataType.from_str and
    # Expr.__init_subclass__ run while modules import, so a changed decorator breaks every importer.
    "decorator_on_method_called_at_import": (
        "sqlglot/expressions/datatypes.py", 386, "@classmethod", "@staticmethod",
        ["tests/dialects/test_databricks.py::TestDatabricks::test_add_date",
         "tests/dialects/test_dialect.py::TestDialect::test_regr_r2",
         "tests/dialects/test_doris.py::TestDoris::test_analyze",
         "tests/dialects/test_oracle.py::TestOracle::test_join_marker",
         "tests/dialects/test_tsql.py::TestTSQL::test_types_date",
         "tests/test_optimizer.py::TestOptimizer::test_nested_type_annotation"],
    ),
    "decorator_on_init_subclass": (
        "sqlglot/expressions/core.py", 104, "@classmethod", "@staticmethod",
        ["tests/dialects/test_databricks.py::TestDatabricks::test_add_date",
         "tests/dialects/test_dialect.py::TestDialect::test_regr_r2",
         "tests/dialects/test_doris.py::TestDoris::test_analyze",
         "tests/dialects/test_oracle.py::TestOracle::test_join_marker",
         "tests/test_expressions.py::TestExprs::test_literal_number",
         "tests/test_transforms.py::TestTransforms::test_eliminate_join_marks"],
    ),
    "executor_body_line": (
        "sqlglot/executor/python.py", 385, "source_group, join_group", "join_group, source_group",
        [f"tests/test_executor.py::TestExecutor::{n}" for n in (
            "test_correlated_exists_over_scalar_aggregate", "test_distinct_order_by_column_from_different_table",
            "test_execute_callable", "test_execute_subqueries", "test_execute_tables", "test_execute_tpcds",
            "test_outer_joins_preserve_unmatched_rows", "test_subqueries", "test_in_any_subquery_without_a_from")],
    ),
}


@pytest.fixture(scope="module")
def env():
    from maps.load import load
    import diffparse
    import rules

    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    return load(str(ROOT / "maps")), tests, diffparse, rules


@pytest.mark.parametrize("name", sorted(CASES))
def test_probe_selection_contains_every_real_failure(env, name):
    maps, tests, diffparse, rules = env
    rel, ln, old_s, new_s, failing = CASES[name]
    old = (PIN / rel).read_text("utf-8")
    lines = old.split("\n")
    assert old_s in lines[ln - 1], (rel, ln, lines[ln - 1])
    lines[ln - 1] = lines[ln - 1].replace(old_s, new_s)
    patch = make_patch(rel, old, "\n".join(lines))
    got, _ = rules.select(diffparse.parse(patch), maps, tests, src_root=PIN)
    ids = {t["number"]: t["id"] for t in tests}
    lost = [ids[n] for n in map(lambda i: next(t["number"] for t in tests if t["id"] == i), failing) if n not in got]
    assert not lost, lost


def test_enum_member_value_flows_to_every_dialect_that_names_it(env):
    """Stress-style trap: UPPERCASE is read only through class bodies (NORMALIZATION_STRATEGY =
    NormalizationStrategy.UPPERCASE) executed at import, never by a line a test runs."""
    maps, tests, diffparse, rules = env
    rel = "sqlglot/dialects/dialect.py"
    old = (PIN / rel).read_text("utf-8")
    new = old.replace("    UPPERCASE = auto()", "    UPPERCASE = None", 1)
    assert new != old
    got, _ = rules.select(diffparse.parse(make_patch(rel, old, new)), maps, tests, src_root=PIN)
    snow = [t["number"] for t in tests if "test_snowflake" in t["id"]]
    assert snow and set(snow) <= got


def test_enum_member_value_edit_selects_readers_of_the_member(env):
    maps, tests, diffparse, rules = env
    patch = (Path(__file__).resolve().parent / "fixtures" / "enum_member.patch").read_text()
    got, _ = rules.select(diffparse.parse(patch), maps, tests, src_root=PIN)
    ids = {t["number"]: t["id"] for t in tests}
    for want in ("test_postgres.py::TestPostgres::test_date_interval_timestamp",
                 "test_postgres.py::TestPostgres::test_extract_date_parts",
                 "test_parser.py::TestParser::test_text_match_excludes_delimited_tokens"):
        n = [k for k, v in ids.items() if v.endswith(want)]
        assert n and n[0] in got, want


def test_fixture_comment_with_unicode_line_separator_is_not_a_noop(env):
    maps, tests, diffparse, rules = env
    patch = (Path(__file__).resolve().parent / "fixtures" / "identity_u2028.patch").read_text()
    got, _ = rules.select(diffparse.parse(patch), maps, tests, src_root=PIN)
    assert got


def test_syntax_error_in_a_lazily_loaded_dialect_module_selects_every_test_that_loads_it(env):
    """stress-3-0028: sqlglot/generators/exasol.py is imported only through the dialect registry
    (Dialect.classes, a dialect name string, iterating Dialects). Static imports and the preimported
    coverage runs do not see those tests; the cold import map (maps/lazyimp.bin) does."""
    maps, tests, diffparse, rules = env
    rel = "sqlglot/generators/exasol.py"
    old = (PIN / rel).read_text("utf-8")
    got, _ = rules.select(diffparse.parse(make_patch(rel, old, old + "def broken(:\n")), maps, tests, src_root=PIN)
    ids = {t["id"]: t["number"] for t in tests}
    for want in ("tests/dialects/test_dialect.py::TestDialect::test_enum",
                 "tests/dialects/test_dialect.py::TestDialect::test_regr_count",
                 "tests/dialects/test_druid.py::TestDruid::test_druid",
                 "tests/test_generator.py::TestGenerator::test_generator_memory",
                 "tests/test_time.py::TestTime::test_canonical_time_format_vocabulary"):
        assert ids[want] in got, want


def test_default_in_def_header_is_not_widened_when_the_body_never_ran_at_import(env):
    maps, tests, diffparse, rules = env
    rel = "sqlglot/optimizer/qualify.py"
    old = (PIN / rel).read_text("utf-8")
    new = old.replace("isolate_tables: bool = False,", "isolate_tables: bool = True,", 1)
    assert new != old
    got, _ = rules.select(diffparse.parse(make_patch(rel, old, new)), maps, tests, src_root=PIN)
    assert 0 < len(got) < 1000  # the def header always runs at import; only a body line that does widens


def test_decorator_on_init_subclass_selects_every_importer_of_expressions(env):
    """Expr.__init_subclass__ runs for every Expr class at import: stress-3-0049 fails 1600 tests."""
    maps, tests, diffparse, rules = env
    rel = "sqlglot/expressions/core.py"
    old = (PIN / rel).read_text("utf-8")
    new = old.replace("    @classmethod\n    def __init_subclass__", "    @staticmethod\n    def __init_subclass__", 1)
    assert new != old
    got, _ = rules.select(diffparse.parse(make_patch(rel, old, new)), maps, tests, src_root=PIN)
    assert len(got) >= 1600
