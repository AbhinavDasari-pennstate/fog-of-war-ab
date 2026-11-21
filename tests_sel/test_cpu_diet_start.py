"""CPU diet, start-up: what the selector imports, and the records that replaced two dataclasses."""

import json
import subprocess
import sys
from pathlib import Path

import diffparse
import tpcds_scope

SEL = str(Path(__file__).resolve().parent.parent / "selector")


def run_py(code: str) -> str:
    r = subprocess.run([sys.executable, "-S", "-E", "-c", f"import sys; sys.path.insert(0, {SEL!r})\n{code}"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_hunk_and_filechange_keep_what_the_dataclasses_gave():
    a = diffparse.Hunk(1, 2, 3, 4)
    b = diffparse.Hunk(1, 2, 3, 4)
    assert a == b and a.lines == [] and a.no_newline is False
    assert a.lines is not b.lines  # the default list is not shared
    b.lines.append((" ", "x"))
    assert a != b
    assert repr(diffparse.Hunk(1, 2, 3, 4, [("+", "y")], True)) == (
        "Hunk(old_start=1, old_count=2, new_start=3, new_count=4, lines=[('+', 'y')], no_newline=True)")
    fc = diffparse.FileChange("a.py", "b.py", "renamed")
    assert fc == diffparse.FileChange("a.py", "b.py", "renamed", []) and fc.hunks == []
    assert repr(fc) == "FileChange(path_old='a.py', path_new='b.py', status='renamed', hunks=[])"
    assert fc != a and (fc == a) is False
    try:
        hash(fc)
    except TypeError:
        pass
    else:
        raise AssertionError("a dataclass with eq=True is unhashable")


def test_scope_keeps_its_fields_and_mask():
    s = tpcds_scope.Scope("replay", {0: "E", 3: "P"}, "why")
    assert (s.kind, s.plan, s.why, s.mask) == ("replay", {0: "E", 3: "P"}, "why", 1 + 8)
    assert tpcds_scope.Scope("keep", 0, "w") == tpcds_scope.Scope("keep", 0, "w")
    assert tpcds_scope.Scope("keep").plan is None and tpcds_scope.Scope("keep").why == ""
    assert repr(tpcds_scope.Scope("keep", 0, "w")) == "Scope(kind='keep', plan=0, why='w')"


def test_patch_modules_import_no_dataclasses():
    out = run_py("import diffparse, tpcds_scope, pack, pyscope, weakmut, attrrule, rules\n"
                 "print('dataclasses' in sys.modules, 'inspect' in sys.modules)")
    assert out == "False False"


def test_a_batch_with_no_replay_imports_no_subprocess_and_stops_the_collector(tmp_path):
    data = tmp_path / "data"
    (data / "commits").mkdir(parents=True)
    (data / "tests.json").write_text(json.dumps({"tests": []}))
    out = run_py(f"import main, gc\nfrom pathlib import Path\nmain.main(Path({str(data)!r}), Path({str(tmp_path)!r}))\n"
                 "print('subprocess' in sys.modules, 'concurrent.futures' in sys.modules, gc.isenabled())")
    assert out == "False False False"
