"""tools/wm_all_build.py: OR over harnesses, and the --old fallback keeps the new data."""
import json
import marshal
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "selector"))
import weakmut as W  # noqa: E402

SITE_A = "sqlglot/x.py:1:0:1:5:C"
SITE_B = "sqlglot/x.py:2:0:2:5:C"


def _res(d: Path, seeds: int, by_test: dict, ident="t::a"):
    dirs = []
    for s in range(seeds):
        r = d / f"{d.name}{s}"
        r.mkdir(parents=True)
        for n, mask in by_test.items():
            (r / f"{n}.mar").write_bytes(marshal.dumps({"id": f"t::{n}", "mask": mask}))
        dirs.append(str(r))
    return dirs


def _build(tmp_path, a, b=(), c=(), old=None, tests=(1, 2)):
    tj = tmp_path / "tests.json"
    tj.write_text(json.dumps({"tests": [{"number": n, "id": f"t::{n}"} for n in tests]}))
    out = tmp_path / "w.bin"
    src = tmp_path / "src"
    (src / "sqlglot").mkdir(parents=True, exist_ok=True)
    (src / "sqlglot" / "m.py").write_text("x = 1\n")
    cmd = [sys.executable, str(ROOT / "tools" / "wm_all_build.py"), "--src", str(src), "--tests", str(tj),
           "--res", *a, "--min-seeds", "1", "--out", str(out)]
    if b:
        cmd += ["--res-b", *b]
    if c:
        cmd += ["--res-c", *c]
    if old:
        cmd += ["--old", old]
    subprocess.run(cmd, check=True, capture_output=True)
    return marshal.loads(zlib.decompress(out.read_bytes()))


def _groups(d, site):
    rel, l, c, el, ec, kind = site.rsplit(":", 5)
    return dict((m, bits) for m, bits in d["sites"][rel][(int(l), int(c), int(el), int(ec), kind)])


def test_masks_are_ored_over_the_harnesses(tmp_path):
    ea = W.EVAL_BIT
    a = _res(tmp_path / "a", 2, {1: {SITE_A: ea}, 2: {SITE_A: ea}})
    c = _res(tmp_path / "c", 2, {1: {SITE_A: ea | 1 << 4}, 2: {SITE_A: ea}})
    d = _build(tmp_path, a, c=c)
    g = _groups(d, SITE_A)
    assert g[ea | 1 << 4] == 1 << 1 and g[ea] == 1 << 2


def test_a_test_missing_from_a_harness_is_not_recorded(tmp_path):
    a = _res(tmp_path / "a", 1, {1: {SITE_A: W.EVAL_BIT}, 2: {SITE_A: W.EVAL_BIT}})
    c = _res(tmp_path / "c", 1, {1: {SITE_A: W.EVAL_BIT}})  # test 2 failed or was skipped in harness C
    d = _build(tmp_path, a, c=c)
    assert d["recorded"] == 1 << 1


def test_old_entry_is_added_to_new_data_not_substituted(tmp_path):
    ea = W.EVAL_BIT
    a = _res(tmp_path / "a", 1, {1: {SITE_A: ea}, 2: {SITE_A: ea}})
    c = _res(tmp_path / "c", 1, {2: {SITE_A: ea}})  # test 1 has no harness C result
    src = tmp_path / "src"
    (src / "sqlglot").mkdir(parents=True)
    (src / "sqlglot" / "m.py").write_text("x = 1\n")
    sys.path.insert(0, str(ROOT / "tools"))
    from weakmut_build import tree_hash

    old = {"v": 2, "rec": W.RECORDER_VERSION, "src": tree_hash(str(src.resolve())),
           "tests": {1: {"id": "t::1", "sites": {"sqlglot/x.py": {(2, 0, 2, 5, "C"): ea | 1 << 3}}}}}
    oldp = tmp_path / "old.bin"
    oldp.write_bytes(zlib.compress(marshal.dumps(old)))
    d = _build(tmp_path, a, c=c, old=str(oldp))
    assert d["recorded"] == (1 << 1) | (1 << 2)
    assert _groups(d, SITE_A)[ea] & (1 << 1)  # the new harness A data of test 1 survived
    assert _groups(d, SITE_B)[ea | 1 << 3] == 1 << 1  # and the old entry was added
