"""maps/impfn.bin (tools/impfn.py): the committed function map must cover what maps/impexec.bin and importtime.bin say, and be
usable only by the interpreter that recorded it."""

import marshal
import sys
import zlib
from pathlib import Path

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))
from maps.load import load  # noqa: E402

MAPS = Path(ROOT) / "maps"


def _doc():
    return marshal.loads(zlib.decompress((MAPS / "impfn.bin").read_bytes()))


def test_recorded_with_the_collection_pass_and_three_seeds():
    d = _doc()
    assert d["v"] == 1 and d["collect"] is True and len(d["seeds"]) >= 3
    assert d["stats"]["collected"] >= 1600  # pytest --collect-only saw the whole suite
    assert d["py"].startswith("3.13.18 "), d["py"]  # the scorer image's interpreter


def test_every_range_of_impexec_is_in_impfn_with_at_least_its_modules():
    old = load(str(MAPS)).impexec[2]
    new = {f: {(a, b): set(ms) for a, b, ms in rows} for f, rows in _doc()["range"].items()}
    lacking = [(f, a, b) for f, rows in old.items() for a, b, ms in rows if not set(ms) <= new.get(f, {}).get((a, b), set())]
    assert not lacking, lacking[:5]


def test_functions_with_import_time_lines_nearly_all_have_a_range():
    # a function whose lines ran at import but that the recorder never saw under a module body keeps the wide answer; many of them would mean
    # the recorder missed a phase (the build had 13 of 383)
    m = load(str(MAPS))
    ranges = _doc()["range"]
    seen, missing = set(), 0
    for f, il in m.importtime.items():
        rec = m.stmts.get(f)
        if not f.startswith("sqlglot/") or rec is None:
            continue
        starts, ends, sids, scopes = rec
        for i, (s, e) in enumerate(zip(starts, ends)):
            kind, d0, d1, _cls = scopes[sids[i]]
            if kind != "function" or (f, d0, d1) in seen or not any(ln in il for ln in range(s, e + 1)):
                continue
            seen.add((f, d0, d1))
            if not any(a <= e and s <= b for a, b, _ in ranges.get(f, ())):
                missing += 1
    assert len(seen) > 300
    assert missing <= 25, (missing, len(seen))


@pytest.mark.skipif(not sys.version.startswith("3.13.18 "), reason="the map is recorded by the scorer's 3.13.18 and refused elsewhere")
def test_loader_uses_it_on_the_recorded_interpreter():
    got = load(str(MAPS)).impfn
    assert got is not None and "sqlglot/__init__.py" in got[1]


@pytest.mark.skipif(sys.version.startswith("3.13.18 "), reason="needs another interpreter")
def test_loader_refuses_it_on_another_interpreter():
    assert load(str(MAPS)).impfn is None
