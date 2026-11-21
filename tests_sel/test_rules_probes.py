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
