"""T3: the committed maps find what the kit's own sample says breaks.

(a) name map: the TSQL class plus the name WEEK_OFFSET reach the test 535z breaks
(b) fixture reads: pushdown_predicates.sql reaches the test 2vdj breaks
(c) line map: the statement at eliminate_joins.py line 71 reaches every test 2jrq breaks
(d) fixture reads inside setUpClass count for every test in the class (tpc-ds, tpc-h)
(e) source readers: sqlglot/typing/*.py reach the test_typing tests
(f) the maps load in under 60 ms and total under 5 MB
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from maps.load import load  # noqa: E402

KIT = Path(os.environ.get("BR_KIT_SAMPLE", str(ROOT.parent / "kits" / "rwx" / "x" / "sample")))

needs_kit = pytest.mark.skipif(not (KIT / "answers.json").exists(), reason="kit sample not present")


@pytest.fixture(scope="module")
def maps():
    return load(str(ROOT / "maps"))


@pytest.fixture(scope="module")
def answers():
    return json.loads((KIT / "answers.json").read_text())["commits"]


def numbers(mask: int) -> set[int]:
    return {n for n in range(mask.bit_length()) if (mask >> n) & 1}


def members_under(maps, prefix: str) -> set[int]:
    return {n for n, t in enumerate(maps.ids) if t and t.startswith(prefix)}


@needs_kit
def test_ids_match_kit(maps):
    tests = json.loads((KIT / "tests.json").read_text())["tests"]
    assert len(tests) == 1620
    for t in tests:
        assert maps.ids[t["number"]] == t["id"]


@needs_kit
def test_a_names_reach_535z(maps, answers):
    want = set(answers["535z"]["failing"])
    assert want, "sample answer for 535z changed"
    got = numbers(maps.names_class["TSQL"] | maps.names_attr["WEEK_OFFSET"])
    assert want <= got


@needs_kit
def test_b_fixture_reads_reach_2vdj(maps, answers):
    want = set(answers["2vdj"]["failing"])
    got = numbers(maps.reads["tests/fixtures/optimizer/pushdown_predicates.sql"])
    assert want <= got


@needs_kit
def test_c_statement_reaches_2jrq(maps, answers):
    want = set(answers["2jrq"]["failing"])
    assert len(want) == 8
    f = "sqlglot/optimizer/eliminate_joins.py"
    stmt = maps.stmt(f, 71)
    assert stmt is not None and stmt[0] <= 71 <= stmt[1]
    got = numbers(maps.stmt_mask(f, 71))
    assert want <= got, sorted(want - got)


def test_d_setupclass_reads_cover_whole_class(maps):
    executor = members_under(maps, "tests/test_executor.py::TestExecutor::")
    assert len(executor) > 10
    for path in (
        "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql",
        "tests/fixtures/optimizer/tpc-h/tpc-h.sql",
    ):
        got = numbers(maps.reads[path])
        assert executor <= got, (path, sorted(executor - got)[:5])
    # and the optimizer tests that load tpc-ds / tpc-h directly
    got = numbers(maps.reads["tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"])
    assert got & members_under(maps, "tests/test_optimizer.py::TestOptimizer::test_tpcds")


def test_e_typing_sources_reach_test_typing(maps):
    typing_tests = members_under(maps, "tests/test_typing.py::")
    assert typing_tests
    for f in ("sqlglot/typing/bigquery.py", "sqlglot/typing/tsql.py"):
        assert typing_tests <= numbers(maps.srcreaders[f])
    # the dynamic audit saw the read too
    t = maps.number("tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata")
    assert t is not None and (maps.srcreaders["sqlglot/typing/duckdb.py"] >> t) & 1
    assert maps.srcreaders["sqlglot/dialects/__init__.py"]
    assert members_under(maps, "tests/test_dialect_imports.py::") <= numbers(maps.srcreaders["sqlglot/dialects/__init__.py"])


def test_f_load_time_and_size(tmp_path):
    best = 1e9
    for _ in range(5):
        t0 = time.perf_counter()
        load(str(ROOT / "maps"))
        best = min(best, time.perf_counter() - t0)
    assert best < 0.060, f"load took {best * 1000:.1f} ms"
    total = sum(p.stat().st_size for p in (ROOT / "maps").iterdir() if p.is_file())
    assert total < 5 * 1024 * 1024, total


def test_files_are_world_readable():
    for p in (ROOT / "maps").iterdir():
        if p.is_file():
            assert (p.stat().st_mode & 0o777) == 0o644, p


def test_every_test_has_a_set(maps):
    nums = [n for n, t in enumerate(maps.ids) if t]
    assert len(nums) == 1620
    seen = 0
    for m in maps.sets:
        seen |= m
    missing = [n for n in nums if not (seen >> n) & 1]
    assert not missing, missing[:10]
    # tests whose own run touched no sqlglot line are explained in NOTES.txt
    notes = (ROOT / "maps" / "NOTES.txt").read_text()
    assert "tests with empty coverage" in notes
