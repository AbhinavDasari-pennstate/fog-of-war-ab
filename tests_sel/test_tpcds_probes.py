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
    """T25: the edit sits in a lambda of the Python generator's table. The recorded calls (maps/tpcds_lambdas.json)
    name the queries that call it, so the replay runs those and still sees the broken result."""
    n, same, why = _replay_verdict("classlevel", tmp_path)
    assert 0 < n <= 99 and not same, why


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


def test_default_of_a_method_no_query_calls_drops_giant(tmp_path, capsys):
    """T25: a literal default is read only by a call that runs the body; AthenaParser.parse is never called here."""
    got = _run(tmp_path, {"c": M.patch_text(str(PIN), "default_unused")}, capsys)
    assert got["c"] is False


def test_default_of_a_function_every_query_calls_keeps_giant(tmp_path, capsys):
    got = _run(tmp_path, {"c": M.patch_text(str(PIN), "default_literal")}, capsys)
    assert got["c"] is True


def test_lambda_no_query_calls_drops_giant(tmp_path, capsys):
    """T25: a lambda in the Hive typing table, called by no query and not at import: nothing to replay."""
    f = "sqlglot/typing/hive.py"
    text = (PIN / f).read_text(encoding="utf-8")
    old = 'exp.If: {"annotator": lambda self, e: self._annotate_by_args(e, "true", "false", promote=True)},'
    assert text.count(old) == 1
    import difflib

    new = text.replace(old, old.replace("promote=True", "promote=False"))
    diff = "".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True), f"a/{f}", f"b/{f}"))
    got = _run(tmp_path, {"c": f"diff --git a/{f} b/{f}\n" + diff}, capsys)
    assert got["c"] is False


def test_lambda_called_at_import_is_an_import_time_edit():
    """T25: a lambda the parser module calls while it is imported (maps/tpcds_lambdas.json "called") is never narrowed."""
    import json

    import tpcds_scope

    tp = tpcds_scope.TpcdsMap.load(sel.MAPS_ROOT / "tpcds.z")
    called, by_lambda = tp.lambdas()
    assert called and any(k.startswith("sqlglot/parser.py:") for k in called)
    assert any(k.startswith("sqlglot/generators/python.py:") for k in by_lambda)


def _distributive_swap():
    """synth-holdout-2-0165: an AND/OR swap in distributive_law. Two queries reach it; the optimized tree of both
    changes, the executor result does not."""
    import difflib

    import diffparse
    import tpcds_scope

    f = "sqlglot/optimizer/normalize.py"
    text = (PIN / f).read_text(encoding="utf-8")
    old = "to_func = exp.and_ if to_exp == exp.And else exp.or_"
    assert text.count(old) == 1
    new = text.replace(old, "to_func = exp.and_ if to_exp != exp.And else exp.or_")
    diff = "".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True), f"a/{f}", f"b/{f}"))
    changes = diffparse.parse(f"diff --git a/{f} b/{f}\n" + diff)
    tp = tpcds_scope.TpcdsMap.load(sel.MAPS_ROOT / "tpcds.z")
    sc = tpcds_scope.scope([(c, True) for c in changes], tp, PIN)
    assert sc.kind == "replay" and sc.plan and set(sc.plan.values()) == {"O"}, sc.why
    return sc.plan, {f: new}


def test_later_seeds_reuse_the_executor_verdict_of_a_tree_an_earlier_seed_passed():
    """T26: seed 0 runs the executor on each changed optimized tree; later seeds stop after optimize() when the tree
    digest is one that run passed. The verdicts must be those of five independent replays, and the memo must hold
    the trees (otherwise the saving is not real)."""
    plan, patched = _distributive_swap()
    seeds = sel.replay_seeds(3)
    plain = [sel._run_replay(plan, patched, PIN, 120, sd)[0] for sd in seeds]
    memo, chained = None, []
    for sd in seeds:
        ok, why, memo = sel._run_replay_ex(plan, patched, PIN, 120, sd, memo)
        chained.append(ok)
    assert plain == chained == [True] * len(seeds), (plain, chained)
    assert memo and all(memo.values()), memo


def test_memo_never_hides_a_failure_that_only_some_seeds_show(tmp_path, capsys):
    """The seed-flaky mutant fails under seed 0 and some others. Carrying the memo along must not turn a failing
    seed into a pass: a tree the earlier seeds never saw runs in full."""
    import diffparse
    import tpcds_scope

    tp = tpcds_scope.TpcdsMap.load(sel.MAPS_ROOT / "tpcds.z")
    changes = diffparse.parse(M.patch_text(str(PIN), "seed_flaky"))
    sc = tpcds_scope.scope([(c, True) for c in changes], tp, PIN)
    patched = {c.path_new: diffparse.apply((PIN / c.path_new).read_bytes().decode("utf-8"), c) for c in changes}
    plain = {sd: sel._run_replay(sc.plan, patched, PIN, 120, sd)[0] for sd in sel.replay_seeds(2)}
    memo, chained = None, {}
    for sd in sel.replay_seeds(2):
        ok, _, memo = sel._run_replay_ex(sc.plan, patched, PIN, 120, sd, memo)
        chained[sd] = ok
    assert chained == plain, (plain, chained)
    assert not all(chained.values())


def test_executor_stage_never_uses_the_memo():
    """A plan that changes executor lines (stage E) or execute() before optimize (stage B) must run the executor
    on every seed: the memo is only for unchanged executor code."""
    import tpcds_replay as R

    for st in ("E", "B", "OE", "BO", "ET", "BOET"):
        assert R.exec_unchanged(st) is False, st
    for st in ("P", "O", "T", "OT", "PO"):
        assert R.exec_unchanged(st) is True, st


def test_executor_mutant_chain_matches_independent_replays():
    """An executor-file mutant gets the same verdict on every seed whether or not a memo is carried."""
    import diffparse
    import tpcds_scope

    tp = tpcds_scope.TpcdsMap.load(sel.MAPS_ROOT / "tpcds.z")
    changes = diffparse.parse(M.patch_text(str(PIN), "executor"))
    sc = tpcds_scope.scope([(c, True) for c in changes], tp, PIN)
    patched = {c.path_new: diffparse.apply((PIN / c.path_new).read_bytes().decode("utf-8"), c) for c in changes}
    seeds = sel.replay_seeds(1)[:3]
    plain = [sel._run_replay(sc.plan, patched, PIN, 120, sd)[0] for sd in seeds]
    memo, chained = {}, []
    for sd in seeds:
        ok, _, memo = sel._run_replay_ex(sc.plan, patched, PIN, 120, sd, memo)
        chained.append(ok)
    assert plain == chained, (plain, chained)


def test_set_operations_and_array_unique_agg_are_seed_sensitive():
    """The executor orders UNION DISTINCT rows and ARRAYUNIQUEAGG results by string hashes (executor/python.py
    set_operation, executor/env.py): a tree holding either is never reused across seeds."""
    import tpcds_core as core

    core.setup_path(str(PIN))
    from sqlglot import parse_one

    assert core.seed_sensitive(parse_one("SELECT a FROM t UNION SELECT a FROM u"))
    assert core.seed_sensitive(parse_one("SELECT a FROM t EXCEPT SELECT a FROM u"))
    assert core.seed_sensitive(parse_one("SELECT ARRAY_UNIQUE_AGG(a) FROM t"))
    assert not core.seed_sensitive(parse_one("SELECT a, SUM(b) FROM t GROUP BY a"))
