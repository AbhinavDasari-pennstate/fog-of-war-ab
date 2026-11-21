"""Regression probes for the per-query TPC-DS replay: real mutants that break test_execute_tpcds
(tools/tpcds_mutants.py proves each one fails the real test) must keep test 1106 selected, and
patches that cannot reach any query must drop it. Needs the pinned sqlglot checkout next to the repo."""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "selector"))
sys.path.insert(0, str(ROOT / "tools"))
PIN = Path(os.environ.get("BR_SRC", str(ROOT.parent / "sqlglot-pin")))
SAMPLE = Path(os.environ.get("BR_SAMPLE", str(ROOT.parent / "kits" / "rwx" / "x" / "sample")))
GIANT = "tests/test_executor.py::TestExecutor::test_execute_tpcds"

pytestmark = pytest.mark.skipif(not (PIN / "sqlglot").exists() or not (SAMPLE / "tests.json").exists(),
                                reason="pinned sqlglot or sample missing")

import main as sel  # noqa: E402
import tpcds_mutants as M  # noqa: E402


def _run(tmp_path, patches: dict, capsys):
    (tmp_path / "commits").mkdir()
    for name, text in patches.items():
        (tmp_path / "commits" / f"{name}.patch").write_text(text, encoding="utf-8")
    (tmp_path / "tests.json").write_text((SAMPLE / "tests.json").read_text(encoding="utf-8"), encoding="utf-8")
    sel.main(tmp_path, PIN)
    tests = json.loads((tmp_path / "tests.json").read_text())["tests"]
    num = next(t["number"] for t in tests if t["id"] == GIANT)
    out = {}
    for line in capsys.readouterr().out.splitlines():
        row = json.loads(line)
        out[row["commit"]] = num in {n for w in row["workers"] for n in w}
    return out


@pytest.mark.parametrize("name", [n for n, v in M.MUTANTS.items() if v[3]])
def test_real_mutant_keeps_giant(name, tmp_path, capsys):
    """Each mutant fails the real test (tools/tpcds_mutants.py real <name>), so the giant must stay."""
    got = _run(tmp_path, {name: M.patch_text(str(PIN), name)}, capsys)
    assert got[name] is True


def test_unreachable_function_change_drops_giant(tmp_path, capsys):
    """A one-line change in a function no TPC-DS query reaches needs no replay and drops the giant."""
    f = "sqlglot/parsers/athena.py"
    text = (PIN / f).read_text(encoding="utf-8")
    old = "raw_tokens[1:], sql)"
    assert text.count(old) >= 1
    import difflib

    new = text.replace(old, "raw_tokens[0:], sql)", 1)
    diff = "".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True), f"a/{f}", f"b/{f}"))
    got = _run(tmp_path, {"c": f"diff --git a/{f} b/{f}\n" + diff}, capsys)
    assert got["c"] is False


def test_changed_query_text_keeps_giant(tmp_path, capsys):
    """A fixture edit that changes the SQL of a query (not just its expected optimized text) stays."""
    import difflib

    f = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
    text = (PIN / f).read_text(encoding="utf-8")
    new = text.replace("SELECT ", "SELECT  ", 1)
    diff = "".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True), f"a/{f}", f"b/{f}"))
    got = _run(tmp_path, {"c": f"diff --git a/{f} b/{f}\n" + diff}, capsys)
    # the first SELECT of the file is inside the first query's SQL or its expected text; either way
    # the verdict must not be a silent drop of a changed query: only an unchanged (meta, sql) list drops
    import tpcds_scope

    changed = tpcds_scope._pairs(str(PIN)) != tpcds_scope._pairs(str(PIN), new)
    assert got["c"] is changed or got["c"] is True
    if changed:
        assert got["c"] is True


def _replay_verdict(name, tmp_path):
    """(plan size, replay said same) for a mutant, running scope and the replay child directly."""
    import diffparse
    import tpcds_scope

    tpcds_scope.IMPORT_REPLAY = True  # the import-time path is off by default; these probes exercise it

    tp = tpcds_scope.TpcdsMap.load(sel.MAPS_ROOT / "tpcds.z")
    changes = diffparse.parse(M.patch_text(str(PIN), name))
    sc = tpcds_scope.scope([(c, True) for c in changes], tp, PIN)
    assert sc.kind == "replay"
    patched = {}
    for c in changes:
        old = (PIN / c.path_new).read_bytes().decode("utf-8")
        patched[c.path_new] = diffparse.apply(old, c)
    same, why = sel._run_replay(sc.plan, patched, PIN, 120, 1)
    return len(sc.plan), same, why


def test_class_constant_edit_replays_every_query_and_passes_when_harmless(tmp_path):
    """T19: an import-time edit is replayed over every query on a freshly imported patched tree."""
    n, same, why = _replay_verdict("constant_harmless", tmp_path)
    assert n == 99 and same, why


def test_class_table_edit_that_breaks_queries_is_caught_by_the_replay(tmp_path):
    n, same, why = _replay_verdict("classlevel", tmp_path)
    assert n == 99 and not same, why


def test_optimizer_only_noop_edit_replays_stage_o_only_and_passes(tmp_path):
    """A2: a change reached only inside optimize() runs the optimizer, finds the tree pristine and skips the executor."""
    import diffparse
    import tpcds_scope

    tp = tpcds_scope.TpcdsMap.load(sel.MAPS_ROOT / "tpcds.z")
    changes = diffparse.parse(M.patch_text(str(PIN), "optimizer_noop"))
    sc = tpcds_scope.scope([(c, True) for c in changes], tp, PIN)
    assert sc.kind == "replay" and sc.plan and set(sc.plan.values()) == {"O"}, sc.plan
    n, same, why = _replay_verdict("optimizer_noop", tmp_path)
    assert same, why


def test_executor_only_edit_plan_has_stage_e(tmp_path):
    """A change in an executor-only line must plan stage E (the executor runs to the end)."""
    import diffparse
    import tpcds_scope

    tp = tpcds_scope.TpcdsMap.load(sel.MAPS_ROOT / "tpcds.z")
    for name, stage in (("executor", "E"), ("ensure_tables", "B"), ("find_tables", "B")):
        changes = diffparse.parse(M.patch_text(str(PIN), name))
        sc = tpcds_scope.scope([(c, True) for c in changes], tp, PIN)
        assert sc.kind == "replay" and sc.plan, name
        assert all(stage in v for v in sc.plan.values()), (name, set(sc.plan.values()))


def test_seed_flaky_mutant_is_caught_by_the_seed_set_and_kept(tmp_path, capsys):
    """synth-dev-1-0140: passes under seed 2 (its own crc seed) and fails under most others. The replay seed set
    must include seeds that fail, and the selector keeps the giant."""
    import diffparse
    import tpcds_scope

    tp = tpcds_scope.TpcdsMap.load(sel.MAPS_ROOT / "tpcds.z")
    changes = diffparse.parse(M.patch_text(str(PIN), "seed_flaky"))
    sc = tpcds_scope.scope([(c, True) for c in changes], tp, PIN)
    assert sc.kind == "replay" and sc.plan
    patched = {}
    for c in changes:
        patched[c.path_new] = diffparse.apply((PIN / c.path_new).read_bytes().decode("utf-8"), c)
    verdicts = {sd: sel._run_replay(sc.plan, patched, PIN, 120, sd)[0] for sd in sel.replay_seeds(2)}
    assert not all(verdicts.values()), verdicts
    got = _run(tmp_path, {"flaky": M.patch_text(str(PIN), "seed_flaky")}, capsys)
    assert got["flaky"] is True


def test_replay_child_honours_the_hash_seed(tmp_path):
    """-E would make the child ignore PYTHONHASHSEED. The seed_flaky mutant fails under seed 0 in the real
    test, so the seed-0 replay must say 'different' every time, and two runs under one seed must agree."""
    import diffparse
    import tpcds_scope

    tp = tpcds_scope.TpcdsMap.load(sel.MAPS_ROOT / "tpcds.z")
    changes = diffparse.parse(M.patch_text(str(PIN), "seed_flaky"))
    sc = tpcds_scope.scope([(c, True) for c in changes], tp, PIN)
    patched = {c.path_new: diffparse.apply((PIN / c.path_new).read_bytes().decode("utf-8"), c) for c in changes}
    verdicts = [sel._run_replay(sc.plan, patched, PIN, 120, 0)[0] for _ in range(4)]
    assert verdicts == [False] * 4, verdicts
