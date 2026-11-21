"""Shared rule tests for the T7/T8 selector lanes.

Contract under test (selector/rules.py):

    select(changes, maps, tests, src_root=None) -> (set[int], reason: str)

changes = diffparse.parse(patch_text); maps = maps.load.load(); tests = the "tests" list of
tests.json; src_root = the pinned sqlglot checkout (default: BR_SRC / ../sqlglot-pin). The set
holds test numbers. Unplaceable patches must widen (R9), never return an empty set by accident.
"""

import json
import sys
from pathlib import Path

import pytest

from conftest import KIT, PIN, ROOT, make_patch, needs_kit, needs_pin

sys.path.insert(0, str(ROOT))

pytestmark = [needs_pin, needs_kit]

SAMPLE = KIT / "sample"


@pytest.fixture(scope="module")
def maps():
    from maps.load import load

    return load(str(ROOT / "maps"))


@pytest.fixture(scope="module")
def tests():
    return json.loads((SAMPLE / "tests.json").read_text("utf-8"))["tests"]


@pytest.fixture(scope="module")
def answers():
    return json.loads((SAMPLE / "answers.json").read_text("utf-8"))["commits"]


@pytest.fixture(scope="module")
def rules():
    import rules as r

    return r


def run(rules, maps, tests, patch_text):
    import diffparse

    return rules.select(diffparse.parse(patch_text), maps, tests, src_root=PIN)


def run_sample(rules, maps, tests, cid):
    return run(rules, maps, tests, (SAMPLE / "commits" / f"{cid}.patch").read_bytes().decode("utf-8"))


def num(tests, suffix):
    hits = [t["number"] for t in tests if t["id"].endswith(suffix)]
    assert len(hits) == 1, (suffix, hits)
    return hits[0]


def edit(rules, maps, tests, rel, fn):
    """Patch built from the pinned text of `rel` with fn(lines) -> lines."""
    old = (PIN / rel).read_text("utf-8")
    lines = old.split("\n")
    new = "\n".join(fn(list(lines)))
    assert new != old
    return run(rules, maps, tests, make_patch(rel, old, new))


def find(lines, needle, nth=0):
    hits = [i for i, l in enumerate(lines) if needle in l]
    return hits[nth]


# ---- sample patches --------------------------------------------------------------------------

@pytest.mark.parametrize("cid", ["9u5k", "bap8"])
def test_r4_comment_only_is_empty(rules, maps, tests, cid):
    got, _ = run_sample(rules, maps, tests, cid)
    assert got == set()


def test_r7_class_constant_includes_test_simplify(rules, maps, tests):
    got, _ = run_sample(rules, maps, tests, "535z")
    assert num(tests, "TestOptimizer::test_simplify") in got


@pytest.mark.parametrize("cid", ["4d63", "ktz3", "gbav"])
def test_r7_import_time_lines_select_something(rules, maps, tests, cid):
    got, _ = run_sample(rules, maps, tests, cid)
    assert got


def test_r2_fixture_selects_its_test(rules, maps, tests):
    got, _ = run_sample(rules, maps, tests, "2vdj")
    assert num(tests, "TestOptimizer::test_pushdown_predicates") in got
    assert len(got) < 40


def test_r6_function_body_selects_all_eight(rules, maps, tests, answers):
    got, _ = run_sample(rules, maps, tests, "2jrq")
    assert set(answers["2jrq"]["failing"]) <= got


def test_sample_zero_misses(rules, maps, tests, answers):
    for cid, a in answers.items():
        got, _ = run_sample(rules, maps, tests, cid)
        assert set(a["failing"]) <= got, (cid, sorted(set(a["failing"]) - got))


# ---- hand-made traps -------------------------------------------------------------------------

def test_comment_in_typing_selects_test_typing(rules, maps, tests):
    n = num(tests, "TestTyping::test_no_redundant_expression_metadata")
    got, _ = edit(rules, maps, tests, "sqlglot/typing/snowflake.py", lambda l: l[:3] + ["# a note"] + l[3:])
    assert n in got


def test_hash_inside_multiline_string_is_not_empty(rules, maps, tests):
    def fn(l):
        i = find(l, "def eliminate_join_marks")
        j = find(l[i:], "1. You cannot specify") + i
        return l[: j + 1] + ["# inside the docstring"] + l[j + 1 :]

    got, _ = edit(rules, maps, tests, "sqlglot/transforms.py", fn)
    assert got


def test_docstring_edit_is_not_empty(rules, maps, tests):
    def fn(l):
        i = find(l, "1. You cannot specify")
        l[i] = l[i].replace("You cannot", "You can not")
        return l

    got, _ = edit(rules, maps, tests, "sqlglot/transforms.py", fn)
    assert got


def test_sql_comment_only_fixture_edit_is_empty(rules, maps, tests):
    got, _ = edit(rules, maps, tests, "tests/fixtures/optimizer/pushdown_predicates.sql",
                  lambda l: l[:10] + ["-- only a comment"] + l[10:])
    assert got == set()


def test_hash_metadata_fixture_edit_is_not_empty(rules, maps, tests):
    n = num(tests, "TestOptimizer::test_pushdown_predicates")
    got, _ = edit(rules, maps, tests, "tests/fixtures/optimizer/pushdown_predicates.sql",
                  lambda l: l[:10] + ["# title: only metadata"] + l[10:])
    assert n in got


def test_syntax_error_widens_to_importers(rules, maps, tests):
    got, _ = edit(rules, maps, tests, "sqlglot/transforms.py",
                  lambda l: l[:5] + ["def broken(:"] + l[5:])
    assert len(got) > 1000


def test_test_function_body_edit_selects_that_test(rules, maps, tests):
    n = num(tests, "TestTime::test_format_time")

    def fn(l):
        i = find(l, 'format_time("", {})')
        l[i] = l[i].replace('format_time("", {})', 'format_time("x", {})')
        return l

    got, _ = edit(rules, maps, tests, "tests/test_time.py", fn)
    assert n in got


def test_readme_edit_is_empty(rules, maps, tests):
    got, _ = edit(rules, maps, tests, "README.md", lambda l: l[:3] + ["Extra line."] + l[3:])
    assert got == set()


def test_unapplicable_patch_widens(rules, maps, tests):
    old = (PIN / "sqlglot/transforms.py").read_text("utf-8").split("\n")
    wrong = "\n".join(old[:10] + ["this line is not in the file"] + old[11:])
    patch = make_patch("sqlglot/transforms.py", wrong, wrong.replace("import", "imports", 1))
    got, _ = run(rules, maps, tests, patch)
    assert len(got) > 100


def test_file_delete_widens(rules, maps, tests):
    old = (PIN / "sqlglot/transforms.py").read_text("utf-8")
    patch = "diff --git a/sqlglot/transforms.py b/sqlglot/transforms.py\ndeleted file mode 100644\n" \
            "index 1111111..0000000\n--- a/sqlglot/transforms.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-" \
            + old.split("\n")[0] + "\n"
    got, _ = run(rules, maps, tests, patch)
    assert len(got) > 100
