import json
import subprocess
import sys
from pathlib import Path

import eval as ev
from conftest import ROOT, needs_kit, needs_pin

KIT = ROOT.parent / "kits" / "rwx" / "x"
SAMPLE = KIT / "sample"
BASELINE_OUT = ROOT.parent / "baseline" / "output.jsonl"


def kit_score_ms(output: Path) -> int:
    """The number the kit's own score.py prints, in ms."""
    r = subprocess.run(
        [sys.executable, str(KIT / "score.py"), str(SAMPLE), str(SAMPLE / "answers.json"), str(output)],
        capture_output=True, text=True, check=True,
    )
    line = [l for l in r.stdout.splitlines() if l.startswith("Simulated:")][0]
    sec = line.split()[1]
    whole, frac = sec.split(".")
    return int(whole) * 1000 + int(frac)


def load():
    return ev.load_set("sample", sample_dir=SAMPLE)


@needs_kit
def test_kit_baseline_scores_exactly_what_score_py_prints():
    ds = load()
    res = ev.score(BASELINE_OUT.read_bytes(), ds)
    assert res["ok"]
    assert res["simulated_ms"] == kit_score_ms(BASELINE_OUT) == 1_210_470
    assert res["misses"] == []


@needs_kit
def test_dropping_one_failing_test_reports_that_miss(tmp_path):
    ds = load()
    lines = [json.loads(l) for l in BASELINE_OUT.read_text().splitlines()]
    for row in lines:
        if row["commit"] == "2jrq":
            row["workers"] = [[n for n in w if n != 1104] for w in row["workers"]]
    out = tmp_path / "o.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in lines))
    res = ev.score(out.read_bytes(), ds)
    assert res["ok"]
    assert res["misses"] == [("2jrq", [1104])]
    assert res["simulated_ms"] == kit_score_ms(out)


@needs_kit
def test_rejected_output_is_reported_not_raised():
    ds = load()
    res = ev.score(b"not json\n", ds)
    assert not res["ok"] and res["code"] == "not_json"


@needs_kit
def test_floor_is_optimal_packing_of_the_true_failing_sets():
    ds = load()
    floor = ev.floor_ms(ds)
    # Every failing commit costs at least 2 s; 18 of 30 sample commits break something.
    assert 90_000 < sum(floor.values()) < 100_000
    assert floor["2haz"] == 0  # breaks nothing


@needs_kit
def test_report_has_per_kind_table_and_floor():
    ds = load()
    res = ev.score(BASELINE_OUT.read_bytes(), ds)
    text = ev.render(ds, res, stderr="", cpu_s=None)
    for kind in ("code", "fixture", "comment", "multi"):
        assert kind in text
    assert "floor" in text.lower()
    assert "1210.470" in text


@needs_kit
@needs_pin
def test_native_run_of_the_selector_matches_its_own_score(tmp_path):
    ds = load()
    stdout, stderr, cpu = ev.run_native(ds, ROOT / "selector" / "main.py", src=ROOT.parent / "sqlglot-pin")
    res = ev.score(stdout, ds)
    assert res["ok"] and res["misses"] == []
    out = tmp_path / "o.jsonl"
    out.write_bytes(stdout)
    assert res["simulated_ms"] == kit_score_ms(out)
