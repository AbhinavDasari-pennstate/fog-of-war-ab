"""Order-state hole: tests/test_dialect_entry_points.py clears Dialect._classes, so tests after it run the lazy
re-registration loop in Dialect._try_load (dialect.py 248 to 257). Two real mutants there fail 134 and 221 tests when
the whole suite runs in one process (labels: a full -n 0 run of each mutant). The selector must keep every one of them."""

import json
import sys
from pathlib import Path

import pytest

import diffparse
import rules
from conftest import KIT, PIN, ROOT, needs_kit, needs_pin

pytestmark = [needs_pin, needs_kit]

FIX = Path(__file__).resolve().parent / "fixtures" / "dialect_state"
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def maps():
    from maps.load import load

    return load(str(ROOT / "maps"))


@pytest.fixture(scope="module")
def tests():
    return json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]


@pytest.mark.parametrize("name", ["d254", "d248"])
def test_state_clearing_mutants_keep_every_failing_test(maps, tests, name):
    failing = set(json.loads((FIX / "failing.json").read_text())[name])
    got, why = rules.select(diffparse.parse((FIX / f"{name}.patch").read_text()), maps, tests, src_root=PIN)
    assert not failing - set(got), f"{name}: {len(failing - set(got))} of {len(failing)} failing tests dropped ({why[:200]})"


def test_lambda_edit_keeps_the_tests_selected_before_the_sequential_pass(maps, tests):
    """synth-dev-1-0096 edits a lambda body in a table that runs at import: the sequential coverage pass credits the
    import-time lines to the first importer, so the soft lambda branch must ignore importtime and unstable lines."""
    soft = Path(__file__).resolve().parent / "fixtures" / "soft_lambda"
    before = set(json.loads((soft / "dev0096_ba1e74e.json").read_text()))
    got, why = rules.select(diffparse.parse((soft / "dev0096.patch").read_text()), maps, tests, src_root=PIN)
    assert not before - set(got), f"{len(before - set(got))} of {len(before)} tests lost ({why[:200]})"
