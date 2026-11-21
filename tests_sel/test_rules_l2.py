"""L2: class-level attribute edits select only the recorded readers, and keep today's rule where the
attribute is derived at class creation (metaclass, __init_subclass__, enums) or was never read."""

import json
import sys
from pathlib import Path

import pytest

from conftest import KIT, PIN, ROOT, make_patch, needs_kit, needs_pin

sys.path.insert(0, str(ROOT))
pytestmark = [needs_pin, needs_kit]

TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


@pytest.fixture(scope="module")
def env():
    from maps.load import load
    import diffparse
    import rules

    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    maps = load(str(ROOT / "maps"))
    if not (ROOT / "maps" / "attrreads.bin").exists():
        pytest.skip("no attribute-read map")
    return maps, tests, diffparse, rules


def select(env, rel, old_s, new_s):
    maps, tests, diffparse, rules = env
    old = (PIN / rel).read_text("utf-8")
    assert old.count(old_s) == 1, (rel, old_s)
    got, why = rules.select(diffparse.parse(make_patch(rel, old, old.replace(old_s, new_s))), maps, tests, src_root=PIN)
    num = {t["id"]: t["number"] for t in tests}
    return got, why, num


def test_plain_constant_selects_its_readers_and_not_tpcds(env):
    got, why, num = select(env, "sqlglot/dialects/mysql.py", "    SAFE_DIVISION = True\n", "    SAFE_DIVISION = False\n")
    assert "L2" in why
    assert num["tests/dialects/test_mysql.py::TestMySQL::test_safe_div"] in got
    assert num[TPCDS] not in got
    assert len(got) < 60


@pytest.mark.parametrize("rel,old_s,new_s", [
    # Generator = None: the dialect metaclass reads it with klass.__dict__.get at class creation
    ("sqlglot/dialects/duckdb.py", "    Generator = DuckDBGenerator\n", "    Generator = None\n"),
    # enum member read by value
    ("sqlglot/dialects/dialect.py", "    UPPERCASE = auto()\n", "    UPPERCASE = None\n"),
])
def test_derived_and_enum_edits_keep_today_s_rule(env, rel, old_s, new_s):
    got, why, num = select(env, rel, old_s, new_s)
    assert "L2" not in why
    assert num[TPCDS] in got
    assert len(got) > 400


def test_tokenizer_keywords_entry_keeps_every_user_of_the_class(env):
    # KEYWORDS is read by Tokenizer.__init_subclass__ (trie, comments) while the class is created
    maps, *_ = env
    got, why, num = select(env, "sqlglot/dialects/mysql.py", '            "BLOB": TokenType.BLOB,\n', '            "BLOB": TokenType.VAR,\n')
    assert "L2" not in why
    assert len(got) >= bin(maps.names_class["MySQL.Tokenizer"]).count("1")


def test_derived_table_marks_class_creation_reads():
    import marshal
    import zlib

    d = marshal.loads(zlib.decompress((ROOT / "maps" / "attrreads.bin").read_bytes()))
    assert "Generator" in d["derived"]["sqlglot.dialects.duckdb:DuckDB"]
    assert "KEYWORDS" in d["derived"]["sqlglot.dialects.mysql:MySQL.Tokenizer"]
    assert "sqlglot.dialects.dialect:NormalizationStrategy" in d["enums"]


@pytest.mark.parametrize("rel,old_s,new_s", [
    # reviewer m1: STRING_TYPE_MAPPING flows into TYPE_MAPPING as a bare name inside the class body
    ("sqlglot/generators/exasol.py", '        exp.DType.TEXT: "LONG VARCHAR",\n', '        exp.DType.TEXT: "VARCHAR",\n'),
    # m19 and m21: NAME_TO_PROPERTY is inverted by a comprehension in the class body
    ("sqlglot/expressions/properties.py", '        "PARTITIONED_BY": PartitionedByProperty,\n', '        "PARTITION_BY": PartitionedByProperty,\n'),
    ("sqlglot/expressions/properties.py", '        "LOCK": LockProperty,\n', '        "LOCKS": LockProperty,\n'),
])
def test_bare_name_derivations_keep_today_s_rule(env, rel, old_s, new_s):
    got, why, num = select(env, rel, old_s, new_s)
    assert "L2" not in why
    assert num["tests/dialects/test_exasol.py::TestExasol::test_type_mappings"] in got or "properties" in rel
    assert len(got) > 20


def test_unhashable_set_member_value_keeps_today_s_rule(env):
    got, why, num = select(env, "sqlglot/dialects/mysql.py", "    SAFE_DIVISION = True\n", "    SAFE_DIVISION = {[]}\n")
    assert "L2" not in why
